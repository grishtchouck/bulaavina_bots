"""HTTP server that receives Prodamus payment notifications."""
import json
import logging

from aiogram import Bot
from aiohttp import web

from . import prodamus
from .config import Config
from .db import DB
from .handlers_user import show
from .scenario import Scenario

log = logging.getLogger(__name__)


def make_app(bot: Bot, db: DB, sc: Scenario, cfg: Config) -> web.Application:
    async def prodamus_webhook(request: web.Request):
        form = await request.post()
        flat = {k: v for k, v in form.items() if isinstance(v, str)}
        data = prodamus.unflatten(flat)

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
            await on_paid(order)
        return web.Response(text="success")

    async def on_paid(order):
        uid, tariff_id = order["user_id"], order["tariff"]
        tariff = sc.tariffs.get(tariff_id, {})
        await db.add_categories(uid, tariff.get("grant", []))
        user = await db.get_user(uid)
        try:
            await show(bot, db, sc, uid, uid, user["first_name"] if user else "",
                       tariff.get("success_screen") or sc.start_screen)
        except Exception:
            log.exception("could not notify user %s about payment", uid)
        name = f"@{user['username']}" if user and user["username"] else str(uid)
        for admin in cfg.admin_ids:
            try:
                await bot.send_message(admin, f"💰 Оплата: {name} — {tariff.get('label') or tariff.get('title', tariff_id)}, "
                                              f"{order['amount']:,.0f} ₽".replace(",", " "))
            except Exception:
                pass

    async def health(_):
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_post("/prodamus/webhook", prodamus_webhook)
    app.router.add_get("/health", health)
    return app
