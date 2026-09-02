"""Сервер панели: живёт в том же процессе, что и бот.

Отдельный процесс тут ни к чему — панели нужна та же база и тот же объект Bot,
чтобы банить, писать в чаты и вообще делать всё то же, что меню. aiohttp уже
стоит как зависимость aiogram, так что новых пакетов не появляется.

Наружу порт выводит туннель (ngrok, cloudflared): Telegram открывает мини-
приложение только по https, а сертификат нам заводить незачем.
"""
import logging
import os
import re

from aiohttp import web

from .. import config
from . import api, auth

logger = logging.getLogger("gremlin.web")

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@web.middleware
async def _auth_mw(request: web.Request, handler):
    """Подпись Telegram на каждом запросе к API. Страница отдаётся всем:
    без initData она всё равно ничего не покажет."""
    if not request.path.startswith("/api/"):
        return await handler(request)

    user = auth.check(request.headers.get("X-Init-Data", ""))
    if user is None:
        return web.json_response({"error": "Нет подписи Telegram. Откройте панель "
                                           "кнопкой в боте."}, status=401)
    if not await auth.allowed(user):
        return web.json_response({"error": "Доступ к боту закрыт."}, status=403)
    request["user"] = user
    return await handler(request)


@web.middleware
async def _errors_mw(request: web.Request, handler):
    """Ошибки — в JSON: клиент показывает текст человеку, а не «500»."""
    try:
        return await handler(request)
    except web.HTTPException as e:
        if request.path.startswith("/api/"):
            return web.json_response({"error": e.text or e.reason}, status=e.status)
        raise
    except Exception:
        logger.exception("панель: запрос %s упал", request.path)
        return web.json_response({"error": "Внутренняя ошибка, смотрите логи."},
                                 status=500)


_ASSET_RE = re.compile(r"(/static/app\.(?:css|js))\?v=[^\"']*")


def _asset_version() -> str:
    """Метка версии для app.js и app.css — время правки самих файлов.

    Раньше в index.html стояло «?v=2» руками, и после правки панели браузер
    честно отдавал старый файл: метка-то не менялась. Забыть её обновить —
    вопрос времени, поэтому считаем сами.
    """
    stamps = []
    for name in ("app.js", "app.css"):
        try:
            stamps.append(int(os.path.getmtime(os.path.join(STATIC, name))))
        except OSError:
            pass
    return str(max(stamps)) if stamps else "0"


async def _index(request: web.Request) -> web.Response:
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        html = f.read()
    html = _ASSET_RE.sub(r"\1?v=" + _asset_version(), html)
    # WebView Telegram охотно кэширует страницу, и после обновления панели
    # у людей оставалась старая. Сама страница крошечная, перепроверять её
    # каждый раз дешевле, чем ловить потом «а у меня всё по-старому»
    return web.Response(text=html, content_type="text/html",
                        headers={"Cache-Control": "no-cache"})


def build(bot) -> web.Application:
    app = web.Application(middlewares=[_errors_mw, _auth_mw],
                          client_max_size=config.WEB_UPLOAD_MAX + 1024)
    app["bot"] = bot
    app.add_routes(api.routes)
    app.router.add_get("/", _index)
    app.router.add_static("/static/", STATIC, name="static")
    return app


async def start(bot):
    """Поднять сервер. Вернёт runner, который надо будет остановить."""
    runner = web.AppRunner(build(bot), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, config.WEB_HOST, config.WEB_PORT)
    await site.start()
    logger.info("панель слушает %s:%s, публичный адрес %s",
                config.WEB_HOST, config.WEB_PORT, config.WEBAPP_URL)
    return runner


async def stop(runner) -> None:
    if runner is not None:
        await runner.cleanup()
