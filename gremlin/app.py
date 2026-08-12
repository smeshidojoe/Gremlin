"""Сборка и запуск бота."""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import ErrorEvent, MenuButtonCommands, MenuButtonWebApp, WebAppInfo

from . import config, db, userbot, web
from .handlers import admin_menu, cards, events, group, user_menu
from .middlewares import TrackingMiddleware
from .services import digest, errorlog

logger = logging.getLogger("gremlin")


class _DropPollingDisconnect(logging.Filter):
    """Гасит транзиентную ошибку обрыва long-poll при Ctrl+C — бот и так штатно встаёт."""
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return "Failed to fetch updates" not in msg


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(config.LOG_PATH, encoding="utf-8"),
        ],
    )
    logging.getLogger("aiogram.dispatcher").addFilter(_DropPollingDisconnect())


async def _on_error(event: ErrorEvent) -> None:
    errorlog.add(f"{type(event.exception).__name__}: {event.exception}")
    logger.exception("update error", exc_info=event.exception)


async def main() -> None:
    _setup_logging()
    if not config.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set (.env)")

    await db.init()
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    dp.message.middleware(TrackingMiddleware())
    dp.callback_query.middleware(TrackingMiddleware())
    dp.errors.register(_on_error)

    web_runner = None
    if config.WEBAPP_URL:
        web_runner = await web.start()  # mini app
        # официальная кнопка-меню (≡) в личке: открывает аппку
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="⚙️ Настройки", web_app=WebAppInfo(url=config.WEBAPP_URL))
        )
    else:
        logger.info("WEBAPP_URL не задан — mini app выключен")
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())

    # порядок важен: специфичные роутеры до группового catch-all
    dp.include_routers(
        admin_menu.router,
        user_menu.router,
        cards.router,
        events.router,
        group.router,
    )

    digest_task = asyncio.create_task(digest.scheduler(bot))
    try:
        ub = await userbot.start(bot)
    except Exception:
        ub = None
        logger.warning("юзербот не поднялся", exc_info=True)

    try:
        # бэклог забираем: счётчики должны досчитаться за время простоя.
        # Реагировать на старое не даёт group.stale() в каждом хендлере.
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            drop_pending_updates=False,
        )
    finally:
        digest_task.cancel()
        if ub is not None:
            await ub.disconnect()
        if web_runner is not None:
            await web_runner.cleanup()
        await db.close()
        await bot.session.close()
        logger.info("bot stopped")


def run() -> None:
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("shutdown by signal")
