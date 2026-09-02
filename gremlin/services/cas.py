"""CAS — общий список спамеров Telegram.

Combot держит открытый список аккаунтов, которых ловили на спаме в чужих чатах.
Ценность у него ровно одна, но крупная: он отвечает **до первого сообщения**.
Человек только зашёл — все наши правила молчат, разбирать нечего, а список уже
знает, что его выгоняли из двухсот чатов.

Список чужой, ошибки в нём не наши, поэтому совпадение не банит: оно добавляет
очки в наблюдение, а решение остаётся за порогами чата.

Ответы кэшируем в базе: один и тот же человек ходит по нескольким чатам, а
запись живёт неделями. Сервис не ответил — молчим и считаем, что не знаем:
чужая недоступность не повод менять поведение бота.
"""
import asyncio
import logging
import time

import aiohttp

from .. import config, db

logger = logging.getLogger("gremlin.cas")

# когда сервис лёг, не долбим его каждым сообщением
_fail_until = 0.0
_session: aiohttp.ClientSession | None = None
# замок на каждого человека отдельно: общий выстраивал в очередь и запросы про
# разных людей, и при заходе толпы наблюдение ждало по таймауту на каждого
_locks: dict[int, asyncio.Lock] = {}


def _lock_for(user_id: int) -> asyncio.Lock:
    lock = _locks.get(user_id)
    if lock is None:
        if len(_locks) > 10000:
            # чистим только свободные: занятый замок сейчас кого-то держит
            for uid in [u for u, l in _locks.items() if not l.locked()]:
                del _locks[uid]
        lock = _locks[user_id] = asyncio.Lock()
    return lock


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=config.CAS_TIMEOUT))
    return _session


async def close() -> None:
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


async def _ask(user_id: int) -> bool | None:
    """Спросить сервис. None — не ответил, не разобрали, вообще не знаем."""
    global _fail_until
    if time.monotonic() < _fail_until:
        return None
    try:
        session = await _get_session()
        async with session.get(config.CAS_API, params={"user_id": user_id}) as r:
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}")
            # сервис отдаёт text/html, поэтому проверку типа отключаем
            data = await r.json(content_type=None)
    except Exception as e:
        _fail_until = time.monotonic() + config.CAS_FAIL_PAUSE
        logger.info("CAS не ответил (%s), пауза %d сек", e, config.CAS_FAIL_PAUSE)
        return None
    return bool(data.get("ok"))


async def listed(user_id: int) -> bool | None:
    """Есть ли человек в списке. None — узнать не удалось.

    Между «нет» и «не знаю» разница принципиальная: на «не знаю» бот не должен
    ни наказывать, ни успокаиваться, поэтому наверх идут три состояния, а не два.
    """
    if not user_id or user_id < 0:
        return None
    cached = await db.cas_get(user_id, config.CAS_TTL_LISTED, config.CAS_TTL_CLEAN)
    if cached is not None:
        return cached
    async with _lock_for(user_id):
        # пока ждали замок, ответ мог появиться от соседнего сообщения
        cached = await db.cas_get(user_id, config.CAS_TTL_LISTED, config.CAS_TTL_CLEAN)
        if cached is not None:
            return cached
        got = await _ask(user_id)
    if got is None:
        return None
    await db.cas_put(user_id, got)
    if got:
        logger.info("CAS: %s в списке спамеров", user_id)
    return got


async def points(user_id: int, s) -> tuple[int, list[str]]:
    """Очки наблюдения за совпадение и причина для карточки."""
    if not s.cas_on:
        return 0, []
    hit = await listed(user_id)
    if not hit:
        return 0, []
    return int(s.cas_score), ["в общем списке спамеров (CAS)"]


def status() -> str:
    """Строка для меню: работает сервис или сейчас в паузе после отказа."""
    left = _fail_until - time.monotonic()
    return f"не отвечает, пробуем через {int(left)} сек" if left > 0 else "доступен"


async def keeper() -> None:
    """Фоновая уборка кэша: протухшие ответы не нужны никому."""
    while True:
        await asyncio.sleep(24 * 3600)
        try:
            gone = await db.cas_prune(config.CAS_PRUNE)
            if gone:
                logger.info("кэш CAS подрезан: удалено %d", gone)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("уборка кэша CAS не удалась", exc_info=True)
