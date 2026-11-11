import html
import logging
import secrets
import time

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import (BotCommand, BotCommandScopeChat, CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from . import prodamus
from .config import Config
from .db import DB
from .scenario import Rendered, Scenario

log = logging.getLogger(__name__)
router = Router(name="user")
router.message.filter(F.chat.type == "private")

# local file path → Telegram file_id (after the first upload we resend by id, it's instant)
_file_ids: dict[str, str] = {}


ADMIN_COMMANDS = [
    ("admin", "админка"), ("stats", "статистика"), ("broadcast", "рассылка"),
    ("testpay", "тест оплаты на 10 ₽"), ("success", "экран после оплаты"), ("funnel", "воронка"),
]


async def set_commands(bot: Bot, sc: Scenario, admin_ids: set[int] = frozenset()):
    cmds = [BotCommand(command="start", description=sc.start_description)]
    cmds += [BotCommand(command=k, description=(v or {}).get("description") or k)
             for k, v in sc.commands.items()]
    await bot.set_my_commands(cmds)
    # admins additionally see admin commands in their menu
    admin_cmds = cmds + [BotCommand(command=c, description=d) for c, d in ADMIN_COMMANDS]
    for aid in admin_ids:
        try:
            await bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=aid))
        except TelegramBadRequest:
            log.warning("admin %s has not started the bot yet — menu not set", aid)


async def _send_photo(bot: Bot, sc: Scenario, chat_id: int, photo: str, **kw):
    local = sc.media_path(photo)
    if not local:
        return await bot.send_photo(chat_id, photo, **kw)
    key = f"{local}:{local.stat().st_mtime_ns}"  # new file contents → new upload
    if key in _file_ids:
        try:
            return await bot.send_photo(chat_id, _file_ids[key], **kw)
        except TelegramBadRequest:
            _file_ids.pop(key, None)
    msg = await bot.send_photo(chat_id, FSInputFile(local), **kw)
    _file_ids[key] = msg.photo[-1].file_id
    return msg


async def show(bot: Bot, db: DB, sc: Scenario, chat_id: int, user_id: int, first_name: str,
               screen: str, edit: Message | None = None):
    cats = await db.categories(user_id)
    r: Rendered = sc.render(screen, cats, first_name)
    await db.log_event(user_id, r.name)  # for /funnel
    if r.add:
        await db.add_categories(user_id, r.add)
    if r.remove:
        await db.remove_categories(user_id, r.remove)

    # try to edit the previous text message in place (smoother navigation)
    if edit and not r.photo and edit.text is not None:
        try:
            await edit.edit_text(r.text, reply_markup=r.keyboard, disable_web_page_preview=True)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
    if r.photo:
        await _send_photo(bot, sc, chat_id, r.photo, caption=r.text, reply_markup=r.keyboard)
    else:
        await bot.send_message(chat_id, r.text, reply_markup=r.keyboard, disable_web_page_preview=True)


@router.message(CommandStart())
async def cmd_start(msg: Message, command: CommandObject, bot: Bot, db: DB, sc: Scenario):
    u = msg.from_user
    param = (command.args or "").strip() or None
    await db.upsert_user(u.id, u.username, u.first_name, param)
    if param and param in sc.deep_links:
        await db.add_categories(u.id, sc.deep_links[param])
    await show(bot, db, sc, msg.chat.id, u.id, u.first_name, sc.start_screen)


@router.message(F.text.regexp(r"^/([a-z0-9_]+)(@\w+)?$").as_("m"))
async def cmd_scenario(msg: Message, m, bot: Bot, db: DB, sc: Scenario):
    cmd = sc.commands.get(m.group(1))
    if not cmd:
        return await fallback(msg, bot, db, sc)
    u = msg.from_user
    await db.upsert_user(u.id, u.username, u.first_name)
    await show(bot, db, sc, msg.chat.id, u.id, u.first_name, cmd["screen"])


@router.callback_query(F.data.startswith("g:"))
async def cb_goto(cq: CallbackQuery, bot: Bot, db: DB, sc: Scenario):
    screen = cq.data[2:]
    await cq.answer()
    if screen not in sc.screens:
        screen = sc.start_screen
    u = cq.from_user
    await db.upsert_user(u.id, u.username, u.first_name)
    await show(bot, db, sc, cq.message.chat.id, u.id, u.first_name, screen, edit=cq.message)


@router.callback_query(F.data.startswith("p:"))
async def cb_pay(cq: CallbackQuery, bot: Bot, db: DB, sc: Scenario, cfg: Config):
    tid = cq.data[2:]
    tariff = sc.tariffs.get(tid)
    if not tariff or (tariff.get("admin_only") and cq.from_user.id not in cfg.admin_ids):
        await cq.answer("Тариф не найден", show_alert=True)
        return
    if not (cfg.prodamus_url and cfg.public_url):
        await cq.answer("Оплата пока не настроена", show_alert=True)
        log.error("PRODAMUS_URL / PUBLIC_URL are not set")
        return

    await cq.answer()
    await send_payment_link(cq.message, cq.from_user, tid, bot, db, sc, cfg)


async def send_payment_link(msg: Message, u, tid: str, bot: Bot, db: DB, sc: Scenario, cfg: Config):
    tariff = sc.tariffs[tid]
    await db.upsert_user(u.id, u.username, u.first_name)
    await db.log_event(u.id, "pay_click")
    order_id = f"{u.id}-{int(time.time())}-{secrets.token_hex(3)}"
    await db.create_payment(order_id, u.id, tid, float(tariff["price"]))
    me = await bot.me()
    link = prodamus.payment_link(
        base_url=cfg.prodamus_url,
        order_id=order_id,
        user_id=u.id,
        title=tariff["title"],
        price=tariff["price"],
        notify_url=f"{cfg.public_url}/prodamus/webhook",
        success_url=f"https://t.me/{me.username}",
        secret=cfg.prodamus_secret,
        sign_link=cfg.prodamus_sign_links,
        paid_content=tariff.get("paid_content", ""),
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Перейти к оплате", url=link)]])
    price_str = f"{tariff['price']:,}".replace(",", " ")
    await msg.answer(
        f"<b>{tariff.get('label') or tariff['title']}</b>\nСумма: {price_str} ₽\n\n"
        "Нажмите кнопку ниже. После оплаты бот пришлёт подтверждение автоматически.",
        reply_markup=kb)


@router.message(F.text.startswith("/"))
async def unknown_command(msg: Message, bot: Bot, db: DB, sc: Scenario):
    """Unknown /command → start screen."""
    u = msg.from_user
    await db.upsert_user(u.id, u.username, u.first_name)
    await show(bot, db, sc, msg.chat.id, u.id, u.first_name, sc.start_screen)


async def fallback(msg: Message, bot: Bot, db: DB, sc: Scenario):
    # kept for cmd_scenario (unknown /command); same as unknown_command
    await unknown_command(msg, bot, db, sc)


@router.message()
async def question(msg: Message, bot: Bot, db: DB, sc: Scenario, cfg: Config):
    """Any text/photo/voice from a user is a question: forward it to support, confirm to the user."""
    u = msg.from_user
    if u.id in cfg.admin_ids:  # admins writing to the bot are not customers' questions
        return await unknown_command(msg, bot, db, sc)
    await db.upsert_user(u.id, u.username, u.first_name)
    await db.log_event(u.id, "question")
    cats = await db.categories(u.id)
    buyer_cats = {c for t in sc.tariffs.values() if not t.get("admin_only") for c in t.get("grant", [])}
    who = f'<a href="tg://user?id={u.id}">{html.escape(u.first_name or "без имени")}</a>'
    if u.username:
        who += f" (@{html.escape(u.username)})"
    status = "уже купил(а) курс ✅" if cats & buyer_cats else "ещё не купил(а)"
    header = (f"✉️ <b>Вопрос</b> от {who}\nID: <code>{u.id}</code> · {status}\n"
              "<i>Ответь на это сообщение (свайп влево → «Ответить»), и ответ уйдёт человеку от имени бота.</i>")
    targets = [cfg.support_chat_id] if cfg.support_chat_id else sorted(cfg.admin_ids)
    delivered = False
    for chat in targets:
        try:
            h = await bot.send_message(chat, header, disable_web_page_preview=True)
            c = await bot.copy_message(chat, msg.chat.id, msg.message_id, reply_to_message_id=h.message_id)
            await db.save_thread(chat, h.message_id, u.id)
            await db.save_thread(chat, c.message_id, u.id)
            delivered = True
        except Exception:
            log.exception("could not forward question to %s", chat)
    if delivered:
        await msg.answer(sc.question_reply)
    else:
        await show(bot, db, sc, msg.chat.id, u.id, u.first_name, sc.start_screen)
