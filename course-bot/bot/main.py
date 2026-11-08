import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiohttp import web

from . import config
from .db import DB
from .handlers_admin import make_router
from .handlers_user import router as user_router
from .scenario import Scenario
from .webhook import make_app


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config.load()
    sc = Scenario(cfg.scenario_path)
    db = DB(cfg.db_path)
    await db.connect()

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(db=db, sc=sc, cfg=cfg)
    dp.include_router(make_router(cfg.admin_ids))  # admin first
    dp.include_router(user_router)

    runner = web.AppRunner(make_app(bot, db, sc, cfg))
    await runner.setup()
    await web.TCPSite(runner, cfg.web_host, cfg.web_port).start()
    logging.info("Webhook server on %s:%s", cfg.web_host, cfg.web_port)

    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
