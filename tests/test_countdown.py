"""Общий обратный отсчёт приколов: частота правок и поведение при лимитах."""
import asyncio

import pytest
from aiogram.exceptions import TelegramRetryAfter

from gremlin import config
from gremlin.handlers import games
from gremlin.services import countdown


@pytest.fixture
def fast(monkeypatch):
    real = asyncio.sleep

    async def fast_sleep(delay, *a, **kw):
        await real(0)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)


def test_marks_minute():
    assert countdown.marks(60) == [55, 50, 45, 40, 35, 30, 25, 20, 15, 10, 5,
                                   4, 3, 2, 1]


def test_marks_long_timer_steps_by_ten_then_five():
    m = countdown.marks(600)
    assert m[:3] == [590, 580, 570]
    assert m[m.index(60):m.index(60) + 3] == [60, 55, 50]
    assert all(x % 10 == 0 for x in m if x >= 60)
    assert len(m) == 54 + 15


def test_marks_without_fine_tail_and_short():
    assert countdown.marks(20, fine=0) == [15, 10, 5]
    assert countdown.marks(3) == [2, 1]
    assert countdown.marks(0) == []


def test_edits_fit_telegram_limit():
    """Минута любого прикола — не больше 20 правок."""
    for seconds in (30, 60, 120, 300, 600):
        m = countdown.marks(seconds)
        for start in range(seconds):
            window = [x for x in m if start - 60 < seconds - x <= start]
            assert len(window) <= 20, (seconds, start)
    assert len(countdown.marks(config.BATTLE_TICK, fine=0)) * 60 // config.BATTLE_TICK < 20


@pytest.mark.parametrize("left,text", [(45, "45 сек"), (59, "59 сек"),
                                       (60, "60 сек"), (580, "580 сек")])
def test_label(left, text):
    assert countdown.label(left) == text


async def test_run_draws_every_mark(fast):
    seen = []

    async def draw(left):
        seen.append(left)

    await countdown.run(20, draw)
    assert seen == countdown.marks(20)


async def test_run_goes_quiet_after_rate_limit(fast):
    seen = []

    async def draw(left):
        seen.append(left)
        if len(seen) == 1:
            raise TelegramRetryAfter(method=None, message="Too Many Requests",
                                     retry_after=3600)

    await countdown.run(30, draw)
    assert seen == [25]          # после 429 до конца блокировки молчим


async def test_run_survives_edit_errors_and_stops(fast):
    seen = []

    async def draw(left):
        seen.append(left)
        raise RuntimeError("message is not modified")

    await countdown.run(10, draw, stop=lambda: len(seen) >= 3)
    assert seen == [5, 4, 3]


async def test_court_hides_how_people_vote():
    tail = games._court_tail(45, {1: True, 2: False, 3: True})
    assert tail == "⏳ Голосование: 45 сек\n🗳 Голосов: 3"
