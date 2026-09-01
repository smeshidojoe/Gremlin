"""Сборка и запуск бота."""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (ErrorEvent, MenuButtonCommands, MenuButtonWebApp,
                           WebAppInfo)

from . import config, db, runtime, userbot
from .handlers import admin_menu, cards, events, fun, games, group, user_menu
from .middlewares import TrackingMiddleware
from .services import backup, digest, errorlog, moderation, nn

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


async def _menu_button(bot: Bot, url: str | None) -> None:
    """Кнопка рядом с полем ввода: панель, если её адрес известен.

    Адрес приходит либо из .env, либо от туннеля — и во втором случае меняется
    при каждом его перезапуске, поэтому кнопку переставляем на лету.
    """
    runtime.set_webapp_url(url)
    try:
        if url:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
                text="Панель", web_app=WebAppInfo(url=url)))
        else:
            await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception:
        logger.warning("не выставить кнопку меню", exc_info=True)


async def main() -> None:
    _setup_logging()
    if not config.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set (.env)")

    await db.init()
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(
        parse_mode=ParseMode.HTML,
        # превью ссылок не нужно нигде: ни в меню, ни в карточках, ни в ответах
        link_preview_is_disabled=True,
    ))
    dp = Dispatcher()

    dp.message.middleware(TrackingMiddleware())
    dp.callback_query.middleware(TrackingMiddleware())
    dp.errors.register(_on_error)

    await _menu_button(bot, config.WEBAPP_URL or None)

    # наблюдению нужен наш юзернейм: обращение к боту — не спам-сигнал
    from .services import watch as watch_svc
    watch_svc.set_self((await bot.me()).username)

    # база была битой и её откатили на копию — про такое надо сказать вслух,
    # иначе пропажа настроек за последние часы обнаружится случайно и нескоро
    if db.restored_from:
        for admin_id in config.ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    "⚠️ <b>База была повреждена</b>\n"
                    f"Бот поднялся на суточной копии <code>{db.restored_from}</code>. "
                    "Всё, что произошло после неё, потеряно.\n"
                    "Битый файл лежит рядом с базой с пометкой <code>.malformed-…</code>.")
            except Exception:
                logger.warning("не сказать владельцу о восстановлении", exc_info=True)

    # порядок важен: специфичные роутеры до группового catch-all
    dp.include_routers(
        admin_menu.router,
        fun.router,
        games.router,
        user_menu.router,
        cards.router,
        events.router,
        group.router,
    )

    web_runner = None
    tunnel_task = None
    if config.WEB_ON:
        from .web import server as web_server
        try:
            web_runner = await web_server.start(bot)
        except Exception:
            logger.warning("панель не поднялась", exc_info=True)
        else:
            if not config.WEBAPP_URL and config.TUNNEL_ON:
                from .services import tunnel

                async def _got_url(url: str | None) -> None:
                    await _menu_button(bot, url)

                tunnel_task = asyncio.create_task(tunnel.supervisor(_got_url))

    # Сверяем список чатов с реальностью: за время простоя бота могли выгнать,
    # а обновление до него не дошло. Фоном — старт из-за этого ждать незачем.
    async def _reconcile() -> None:
        from .services import adm_cache
        gone = await adm_cache.reconcile_chats(bot)
        if not gone:
            return
        owners: dict[int, list[str]] = {}
        for cid, title in gone:
            await db.add_event(cid, "bot", "чат выключен: бота там больше нет")
            ch = await db.get_chat(cid)
            if ch and ch["owner_id"]:
                owners.setdefault(ch["owner_id"], []).append(f"{title} ({cid})")
        logger.info("выключены чаты, где бота нет: %s",
                    ", ".join(f"{t} ({c})" for c, t in gone))
        # молча убирать чат из списка нельзя: человек решит, что настройки
        # потерялись, и будет искать их
        for owner_id, names in owners.items():
            try:
                await bot.send_message(
                    owner_id,
                    "ℹ️ Убрал из списка чаты, где бота больше нет:\n"
                    + "\n".join(f"• {n}" for n in names)
                    + "\n\nНастройки сохранены: добавите бота обратно — "
                      "чат вернётся в список со всем, что было.")
            except Exception:
                logger.warning("не сказать владельцу %s о выключенных чатах",
                               owner_id, exc_info=True)

    reconcile_task = asyncio.create_task(_reconcile())
    digest_task = asyncio.create_task(digest.scheduler(bot))
    titles_task = asyncio.create_task(games.titles_scheduler(bot))
    backup_task = asyncio.create_task(backup.scheduler())
    sweeper_task = asyncio.create_task(moderation.card_sweeper(bot))
    nn_task = asyncio.create_task(nn.keeper())
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
        reconcile_task.cancel()
        digest_task.cancel()
        titles_task.cancel()
        backup_task.cancel()
        sweeper_task.cancel()
        nn_task.cancel()
        if tunnel_task is not None:
            tunnel_task.cancel()
        if web_runner is not None:
            from .web import server as web_server
            await web_server.stop(web_runner)
        if ub is not None:
            await ub.disconnect()
        await db.close()
        await bot.session.close()
        logger.info("bot stopped")


def run() -> None:
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("shutdown by signal")
