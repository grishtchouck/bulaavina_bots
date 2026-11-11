"""Replies to forwarded questions: admin (or support chat member) replies → user gets the answer."""
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import Message

from .db import DB

log = logging.getLogger(__name__)


def make_router(admin_ids: set[int], support_chat_id: int | None) -> Router:
    r = Router(name="support")

    def in_support(msg: Message) -> bool:
        if support_chat_id and msg.chat.id == support_chat_id:
            return True
        return msg.chat.type == "private" and msg.from_user.id in admin_ids

    async def thread_reply(msg: Message, db: DB):
        if not msg.reply_to_message or not in_support(msg):
            return False
        uid = await db.thread_user(msg.chat.id, msg.reply_to_message.message_id)
        return {"thread_user": uid} if uid else False

    @r.message(thread_reply)
    async def answer_user(msg: Message, bot: Bot, db: DB, thread_user: int):
        try:
            sent = await bot.copy_message(thread_user, msg.chat.id, msg.message_id)
        except TelegramForbiddenError:
            await db.set_blocked(thread_user)
            return await msg.reply("❌ Не доставлено: человек заблокировал бота.")
        except Exception as e:
            log.exception("answer to %s failed", thread_user)
            return await msg.reply(f"❌ Не доставлено: {e}")
        # the user may reply again — keep the thread going from the answer too
        await db.log_event(thread_user, "answered")
        await msg.reply("✅ Ответ отправлен.")
        _ = sent

    return r
