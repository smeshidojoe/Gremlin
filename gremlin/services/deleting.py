"""Удаление сообщений, которое не сдаётся на первом «подождите».

Раньше каждое удаление было завернуто в «попробовать, а если ошибка — ничего».
Для уже удалённого или старого сообщения это верно. Но так же молча
проглатывался и ответ Telegram «слишком часто, подождите N секунд» — и во
время набега, когда удалять надо больше всего, спам оставался висеть в чате,
а в логах об этом не было ни строчки.

Здесь ошибки разделены:
  * уже удалено, старше 48 часов, нет прав — обычное дело, молчим;
  * «подождите» — ждём сколько сказано и повторяем. Пауза общая на чат: пока
    один обработчик ждёт, остальные в этом чате не долбят Telegram впустую и
    не продлевают себе запрет;
  * ждать слишком долго или повторы кончились — сдаёмся и пишем в «🐞 Ошибки»,
    чтобы было видно, что сообщение осталось в чате.
"""
import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable

from aiogram.exceptions import (TelegramBadRequest, TelegramForbiddenError,
                                TelegramNetworkError, TelegramRetryAfter)

from . import errorlog

logger = logging.getLogger("gremlin.deleting")

RETRIES = 3
# Дольше не ждём: сообщение всё это время висит у всех на глазах, а обработчик
# занят. Честнее сдаться и сказать об этом.
MAX_WAIT = 30
BATCH = 100              # столько id принимает deleteMessages за раз
COMPLAIN_EVERY = 60      # не чаще раза в минуту на чат, иначе набег забьёт журнал

_pause_until: dict[int, float] = {}
_rate_hit: dict[int, float] = {}        # чат -> когда последний раз упёрлись в лимит
_complained: dict[int, float] = {}

# вынесено ради тестов: подменять asyncio.sleep целиком опасно — подмена,
# которая сама зовёт asyncio.sleep, уходит в бесконечную рекурсию
_sleep = asyncio.sleep


async def _wait_turn(chat_id: int) -> None:
    left = _pause_until.get(chat_id, 0.0) - time.monotonic()
    if left > 0:
        # вразброс: иначе все, кто ждал, ударят в одну и ту же секунду
        await _sleep(left + random.uniform(0, 0.5))


def _complain(chat_id: int, why: str) -> None:
    logger.warning("удаление в %s не удалось: %s", chat_id, why)
    now = time.monotonic()
    if now - _complained.get(chat_id, -COMPLAIN_EVERY) < COMPLAIN_EVERY:
        return
    _complained[chat_id] = now
    errorlog.add(f"удаление в {chat_id}: {why} — сообщения остались в чате")


async def one(call: Callable[[], Awaitable], chat_id: int) -> bool:
    """Выполнить удаление с повторами. True — удалено."""
    for attempt in range(1, RETRIES + 1):
        await _wait_turn(chat_id)
        try:
            await call()
            return True
        except TelegramRetryAfter as e:
            wait = int(e.retry_after)
            _rate_hit[chat_id] = time.monotonic()
            _pause_until[chat_id] = max(_pause_until.get(chat_id, 0.0),
                                        time.monotonic() + wait)
            if wait > MAX_WAIT:
                _complain(chat_id, f"Telegram просит подождать {wait} с")
                return False
            logger.info("удаление в %s: Telegram просит подождать %s с (попытка %d)",
                        chat_id, wait, attempt)
        except TelegramNetworkError:
            logger.info("удаление в %s: сеть, повтор (попытка %d)", chat_id, attempt)
            await _sleep(1)
        except (TelegramBadRequest, TelegramForbiddenError) as e:
            # уже удалено, старше 48 часов, нет прав — для удаления обычное дело
            logger.debug("удаление в %s пропущено: %s", chat_id, e)
            return False
        except Exception:
            logger.warning("удаление в %s: неожиданная ошибка", chat_id, exc_info=True)
            return False
    _complain(chat_id, f"повторы кончились ({RETRIES})")
    return False


async def many(bot, chat_id: int, ids: list[int]) -> int:
    """Удалить пачку одним запросом на каждые 100 id. Возвращает, сколько ушло.

    Один запрос вместо десятка — это и быстрее, и бережёт лимит Telegram:
    уборка залпа флуда и сообщений забаненного — ровно те моменты, когда
    запросов больше всего.
    Если пачка не прошла не из-за лимита (например, в ней оказалось сообщение,
    которое удалить нельзя), добиваем по одному. Если из-за лимита — по одному
    не добиваем: десяток запросов вместо одного лимит только продлит.
    """
    ids = list(dict.fromkeys(i for i in ids if i))      # без повторов и нулей
    gone = 0
    for start in range(0, len(ids), BATCH):
        chunk = ids[start:start + BATCH]
        began = time.monotonic()
        if await one(lambda: bot.delete_messages(chat_id, chunk), chat_id):
            gone += len(chunk)
            continue
        if _rate_hit.get(chat_id, 0.0) >= began:
            break
        for mid in chunk:
            if await one(lambda: bot.delete_message(chat_id, mid), chat_id):
                gone += 1
    return gone
