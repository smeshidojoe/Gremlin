"""Публичный адрес для панели без ручной возни.

Telegram открывает мини-приложение только по https, а свой домен с сертификатом
ради панели заводить не хочется. Поэтому бот сам поднимает быстрый туннель
Cloudflare: аккаунт, токен и домен для него не нужны, адрес выдаётся на лету.

Адрес живёт до перезапуска туннеля, поэтому кнопку меню ставим не из .env, а из
рантайма — каждый раз, когда туннель сообщил новый адрес. Если cloudflared
падает, поднимаем заново с нарастающей паузой: панель просто недоступна эти
несколько секунд, бот работает как обычно.
"""
import asyncio
import logging
import re

from .. import config

logger = logging.getLogger("gremlin.tunnel")

# cloudflared пишет адрес в лог одной строкой среди рамочек из плюсов
_URL = re.compile(rb"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")

RETRY_MIN = 5        # пауза перед перезапуском упавшего туннеля, сек
RETRY_MAX = 300
STARTUP_WAIT = 60    # сколько ждём адрес, прежде чем считать запуск неудачным


async def _run_once(on_url) -> None:
    """Один запуск cloudflared: читаем его вывод, пока процесс жив."""
    proc = await asyncio.create_subprocess_exec(
        config.CLOUDFLARED_BIN, "tunnel", "--no-autoupdate",
        "--url", f"http://127.0.0.1:{config.WEB_PORT}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,     # cloudflared пишет всё в stderr
    )
    logger.info("cloudflared запущен, pid %s", proc.pid)
    found = None
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            m = _URL.search(line)
            if m and m.group(0).decode() != found:
                found = m.group(0).decode()
                logger.info("панель доступна по адресу %s", found)
                await on_url(found)
            elif b"ERR" in line or b"error" in line:
                logger.debug("cloudflared: %s", line.decode(errors="replace").strip())
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except asyncio.TimeoutError:
                proc.kill()
    logger.warning("cloudflared завершился с кодом %s", proc.returncode)


async def supervisor(on_url) -> None:
    """Держим туннель поднятым, пока жив бот. Отменяется вместе с задачей."""
    delay = RETRY_MIN
    while True:
        try:
            await _run_once(on_url)
            delay = RETRY_MIN            # раз проработал — считаем попытку удачной
        except FileNotFoundError:
            logger.warning("cloudflared не найден (%s): панель без публичного "
                           "адреса. Задайте WEBAPP_URL вручную или поставьте "
                           "cloudflared в образ.", config.CLOUDFLARED_BIN)
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("туннель упал", exc_info=True)
        await on_url(None)               # адреса больше нет, кнопку убираем
        logger.info("перезапуск туннеля через %s сек", delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, RETRY_MAX)
