"""Soft reminder for people who opened the payment link but didn't pay."""
import asyncio
import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError

from .db import DB
from .handlers_user import show
from .scenario import Scenario

log = logging.getLogger(__name__)
CHECK_EVERY = 300  # seconds


async def send_due(bot: Bot, db: DB, sc: Scenario) -> int:
    rem = sc.reminder
    if not rem:
        return 0
    buyer_cats = {c for t in sc.tariffs.values() if not t.get("admin_only") for c in t.get("grant", [])}
    test_tariffs = [k for k, t in sc.tariffs.items() if t.get("admin_only")]
    due = await db.due_reminders(int(time.time()) - rem["after_minutes"] * 60, buyer_cats, test_tariffs)
    sent = 0
    for uid in due:
        await db.mark_reminded(uid)  # mark first: never send twice even if sending fails
        user = await db.get_user(uid)
        try:
            await show(bot, db, sc, uid, uid, user["first_name"] if user else "", rem["screen"])
            await db.log_event(uid, "reminder_sent")
            sent += 1
        except TelegramForbiddenError:
            await db.set_blocked(uid)
        except Exception:
            log.exception("reminder to %s failed", uid)
        await asyncio.sleep(0.1)
    if sent:
        log.info("reminders sent: %s", sent)
    return sent


async def reminder_loop(bot: Bot, db: DB, sc: Scenario):
    while True:
        try:
            await send_due(bot, db, sc)
        except Exception:
            log.exception("reminder loop error")
        await asyncio.sleep(CHECK_EVERY)
