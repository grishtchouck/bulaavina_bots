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

# local file path → Telegram file_id (after the first upload we resend by id, it's instant)
_file_ids: dict[str, str] = {}


ADMIN_COMMANDS = [
    ("admin", "админка"), ("stats", "статистика"), ("broadcast", "рассылка"),
    ("testpay", "тест оплаты на 10 ₽"),
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
    key = str(local)
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


@router.message()
async def fallback(msg: Message, bot: Bot, db: DB, sc: Scenario):
    """Any other text from a user → show the start screen."""
    u = msg.from_user
    await db.upsert_user(u.id, u.username, u.first_name)
    await show(bot, db, sc, msg.chat.id, u.id, u.first_name, sc.start_screen)
