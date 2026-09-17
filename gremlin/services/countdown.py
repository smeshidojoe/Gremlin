"""Живой обратный отсчёт в сообщениях приколов.

Править сообщение каждую секунду нельзя: в группе боту дают порядка двадцати
сообщений и правок в минуту, а после превышения Telegram на десятки секунд
закрывает боту чат целиком — вместе с удалением спама и банами. Поэтому частота
зависит от того, сколько осталось: пока больше минуты — раз в 10 секунд, на
последней минуте — раз в 5, последние секунды — каждую.
"""
import asyncio
import logging
from typing import Awaitable, Callable

from aiogram.exceptions import TelegramRetryAfter

logger = logging.getLogger("gremlin.countdown")

COARSE = 10      # шаг, пока осталось больше минуты
MEDIUM = 5       # шаг на последней минуте
FINE = 5         # последние секунды, которые показываем каждую


def marks(seconds: int, fine: int = FINE) -> list[int]:
    """Какие значения показать по пути от seconds к нулю. Сам ноль не входит:
    на нуле игра рисует уже итог, а не таймер.

    Отметки круглые — 590, 580… 55, 50… — чтобы таймер не показывал 47 и 37.
    """
    out = []
    left = seconds
    while left > 0:
        if left > 60:
            left = max(60, (left - 1) // COARSE * COARSE)
        elif left > fine:
            left = max(fine, (left - 1) // MEDIUM * MEDIUM)
        else:
            left -= 1
        if left > 0:
            out.append(left)
    return out


def label(left: int) -> str:
    """Сколько осталось: «45 сек», «580 сек».

    Всегда секундами, без перевода в «9:40»: в сообщениях игр так привычнее.
    """
    return f"{left} сек"


async def run(seconds: int, draw: Callable[[int], Awaitable[object]],
              stop: Callable[[], bool] | None = None, fine: int = FINE) -> None:
    """Отсчитать seconds, перерисовывая сообщение через draw(сколько осталось).

    Время меряем от старта, а не суммой пауз: правка идёт сотни миллисекунд, и
    за десяток правок таймер иначе отстал бы на секунды, а игра затянулась.

    draw правит сообщение сам и ошибок не глотает. «Текст не изменился» или
    «сообщение удалили» тут не страшны — пропускаем. Лимит Telegram — повод
    замолчать до конца блокировки, а не ждать его и не долбить повторно.

    stop() — вернуть True, если отсчёт больше не нужен (дуэль уже приняли).
    fine — сколько последних секунд показывать посекундно.
    """
    loop = asyncio.get_running_loop()
    start = loop.time()
    quiet_until = 0.0
    for left in marks(seconds, fine):
        await asyncio.sleep(max(0.0, (seconds - left) - (loop.time() - start)))
        if stop is not None and stop():
            return
        if loop.time() < quiet_until:
            continue
        try:
            await draw(left)
        except TelegramRetryAfter as e:
            quiet_until = loop.time() + e.retry_after
            logger.info("отсчёт: Telegram просит подождать %s сек", e.retry_after)
        except Exception:
            logger.debug("отсчёт: правка не прошла", exc_info=True)
    await asyncio.sleep(max(0.0, seconds - (loop.time() - start)))
