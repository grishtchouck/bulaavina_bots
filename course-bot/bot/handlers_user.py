import logging
import secrets
import time

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import prodamus
from .config import Config
from .db import DB
from .scenario import Rendered, Scenario

log = logging.getLogger(__name__)
router = Router(name="user")


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
        await bot.send_photo(chat_id, r.photo, caption=r.text, reply_markup=r.keyboard)
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
    if not tariff:
        await cq.answer("Тариф не найден", show_alert=True)
        return
    if not (cfg.prodamus_url and cfg.public_url):
        await cq.answer("Оплата пока не настроена", show_alert=True)
        log.error("PRODAMUS_URL / PUBLIC_URL are not set")
        return

    u = cq.from_user
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
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Перейти к оплате", url=link)]])
    await cq.answer()
    await cq.message.answer(
        f"<b>{tariff['title']}</b>\nСумма: {tariff['price']} ₽\n\n"
        "Нажмите кнопку ниже. После оплаты бот пришлёт подтверждение автоматически.",
        reply_markup=kb)


@router.message()
async def fallback(msg: Message, bot: Bot, db: DB, sc: Scenario):
    """Any other text from a user → show the start screen."""
    u = msg.from_user
    await db.upsert_user(u.id, u.username, u.first_name)
    await show(bot, db, sc, msg.chat.id, u.id, u.first_name, sc.start_screen)
