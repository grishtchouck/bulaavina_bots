"""HTTP server that receives Prodamus payment notifications."""
import html
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiohttp import web

from . import prodamus
from .config import Config
from .db import DB
from .handlers_user import show
from .scenario import Scenario

log = logging.getLogger(__name__)


# set by make_app(); used by the admin /paid command to process a missed payment by hand
_mark_paid_manually = None


async def mark_paid_manually(order_id: str) -> str:
    return await _mark_paid_manually(order_id)


def make_app(bot: Bot, db: DB, sc: Scenario, cfg: Config) -> web.Application:
    async def prodamus_webhook(request: web.Request):
        form = await request.post()
        flat = {k: v for k, v in form.items() if isinstance(v, str)}
        data = prodamus.unflatten(flat)
        log.info("Prodamus: notification order_num=%s status=%s",
                 data.get("order_num"), data.get("payment_status"))

        if not prodamus.verify(data, cfg.prodamus_secret, request.headers.get("Sign", "")):
            log.warning("Prodamus: bad signature, payload=%s", flat)
            return web.Response(status=400, text="bad signature")

        # our order id comes back as order_num (order_id is Prodamus' internal id)
        order = None
        for key in ("order_num", "order_id"):
            if data.get(key):
                order = await db.get_payment(str(data[key]))
                if order:
                    break
        if not order:
            log.error("Prodamus: order not found, payload=%s", flat)
            return web.Response(text="success")  # 200, чтобы Prodamus не ретраил бесконечно

        if data.get("payment_status") != "success":
            log.info("Prodamus: order %s status=%s", order["order_id"], data.get("payment_status"))
            return web.Response(text="success")

        first_time = await db.mark_paid(order["order_id"], json.dumps(data, ensure_ascii=False))
        if first_time:
            log.info("Prodamus: order %s paid", order["order_id"])
            await on_paid(order)
        return web.Response(text="success")

    async def manual(order_id: str) -> str:
        order = await db.get_payment(order_id)
        if not order:
            return "not_found"
        if not await db.mark_paid(order_id, json.dumps({"manual": True})):
            return "already"
        log.info("order %s marked paid manually", order_id)
        await on_paid(order, manual=True)
        return "ok"

    global _mark_paid_manually
    _mark_paid_manually = manual

    async def on_paid(order, manual: bool = False):
        uid, tariff_id = order["user_id"], order["tariff"]
        tariff = sc.tariffs.get(tariff_id, {})
        await db.add_categories(uid, tariff.get("grant", []))
        if not tariff.get("admin_only"):
            await db.log_event(uid, "paid")
        user = await db.get_user(uid)
        try:
            await show(bot, db, sc, uid, uid, user["first_name"] if user else "",
                       tariff.get("success_screen") or sc.start_screen)
        except Exception:
            log.exception("could not notify user %s about payment", uid)
        text = await payment_notice(order, tariff, user)
        if manual:
            text += "\n\n✋ <i>проведено вручную командой /paid</i>"
        for admin in cfg.admin_ids:
            try:
                await bot.send_message(admin, text, disable_web_page_preview=True)
            except Exception:
                log.exception("could not send payment notice to admin %s", admin)

    async def payment_notice(order, tariff, user) -> str:
        rub = lambda x: f"{x:,.0f}".replace(",", " ")
        uid = order["user_id"]
        first = html.escape((user["first_name"] if user else "") or "без имени")
        who = f'<a href="tg://user?id={uid}">{first}</a>'
        if user and user["username"]:
            who += f" (@{html.escape(user['username'])})"
        test = bool(tariff.get("admin_only"))
        msk = timezone(timedelta(hours=3))
        day_start = datetime.now(msk).replace(hour=0, minute=0, second=0, microsecond=0)
        tests = [k for k, t in sc.tariffs.items() if t.get("admin_only")]
        n_day, s_day = await db.paid_summary(int(day_start.timestamp()), tests)
        n_all, s_all = await db.paid_summary(0, tests)
        lines = [
            "🧪 <b>Тестовая оплата</b>" if test else "💰 <b>Новая оплата!</b>",
            "",
            f"Курс: {html.escape(tariff.get('label') or tariff.get('title', order['tariff']))}",
            f"Сумма: {rub(order['amount'])} ₽",
            f"Покупатель: {who}",
            f"ID: <code>{uid}</code>",
        ]
        if user and user["start_param"]:
            lines.append(f"Источник: {html.escape(user['start_param'])}")
        lines += ["", f"Сегодня: {n_day} на {rub(s_day)} ₽ · всего: {n_all} на {rub(s_all)} ₽"]
        if test:
            lines.append("<i>тестовые оплаты в эти суммы не входят</i>")
        return "\n".join(lines)

    async def health(_):
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_post("/prodamus/webhook", prodamus_webhook)
    app.router.add_get("/health", health)
    return app
