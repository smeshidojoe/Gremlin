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
POLL = 30            # как часто спрашиваем адрес у соседнего контейнера, сек
# сколько проверок подряд должно провалиться, прежде чем считать адрес
# потерянным. Одиночный промах — это переподключение соседа или его рестарт,
# и снимать из-за него кнопку панели глупо: клиент Telegram кэширует её
# состояние, а через полминуты адрес тот же самый
MISS_LIMIT = 3
# первые проверки после старта: cloudflared регистрирует имя на trycloudflare
# секунд десять, бот поднимается быстрее — молчим, пока сосед просыпается
WARMUP = 2


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


async def _quick_hostname() -> str | None:
    """Спросить у соседнего cloudflared, какое имя ему выдали."""
    import aiohttp
    url = f"{config.TUNNEL_METRICS}/quicktunnel"
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url) as resp:
                if resp.status != 200:
                    logger.debug("сосед-туннель ответил %s", resp.status)
                    return None
                # content_type=None: cloudflared отдаёт json без заголовка,
                # и строгая проверка aiohttp иначе роняет разбор
                data = await resp.json(content_type=None)
                return (data.get("hostname") or "").strip() or None
    except Exception as e:
        # причину держим в debug: на старте и при переподключении соседа
        # это норма, а в обычном логе выглядело бы аварией
        logger.debug("сосед-туннель не ответил: %s: %s", type(e).__name__, e)
        return None


async def watcher(on_url) -> None:
    """Следить за адресом туннеля, который поднят отдельным контейнером.

    Свой cloudflared удобен тем, что ничего не нужно настраивать, но умирает
    вместе с ботом: каждая пересборка выдаёт новое имя, а клиенты Telegram
    держат кнопку панели в кэше и стучатся по старому — оттуда и ошибка 1033.
    Отдельный контейнер этим не страдает: бот пересобирается, туннель стоит.
    """
    last = "?"          # чтобы первая же проверка попала в лог, даже пустая
    misses = 0          # неудачные проверки подряд
    checks = 0
    while True:
        checks += 1
        host = await _quick_hostname()
        url = f"https://{host}" if host else None

        if url is None:
            misses += 1
            # пока сосед просыпается или моргнул — держим прежний адрес:
            # кнопка панели остаётся на месте, лишней тревоги в логе нет
            if checks <= WARMUP or misses < MISS_LIMIT:
                logger.debug("сосед-туннель молчит (%d-я проверка подряд)", misses)
                await asyncio.sleep(POLL)
                continue
        else:
            misses = 0

        if url != last:
            if url:
                logger.info("панель доступна по адресу %s", url)
            else:
                logger.warning("сосед-туннель (%s) не отвечает %d проверок подряд — "
                               "убираю кнопку панели", config.TUNNEL_METRICS, misses)
            last = url
            await on_url(url)
        await asyncio.sleep(POLL)


async def supervisor(on_url) -> None:
    """Держим туннель поднятым, пока жив бот. Отменяется вместе с задачей."""
    if config.TUNNEL_METRICS:
        await watcher(on_url)          # туннель не наш, только следим за адресом
        return
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
