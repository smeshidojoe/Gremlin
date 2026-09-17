"""Удаление с повторами: лимит Telegram не должен молча оставлять спам в чате.

Сон подменяем через deleting._sleep, а не asyncio.sleep: подмена asyncio.sleep,
которая сама зовёт asyncio.sleep, однажды ушла в бесконечную рекурсию и съела
всю память машины.
"""
import pytest
from aiogram.exceptions import (TelegramBadRequest, TelegramNetworkError,
                                TelegramRetryAfter)
from aiogram.methods import DeleteMessage

from gremlin.services import deleting, errorlog

CHAT = -1001
OTHER = -1002
METHOD = DeleteMessage(chat_id=CHAT, message_id=1)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(deleting, "_sleep", fake_sleep)
    monkeypatch.setattr(deleting, "_pause_until", {})
    monkeypatch.setattr(deleting, "_rate_hit", {})
    monkeypatch.setattr(deleting, "_complained", {})
    errorlog._errors.clear()
    return slept


def script(*outcomes):
    """Вызов, который по очереди падает переданными ошибками, потом удаётся."""
    calls = []

    async def call():
        calls.append(1)
        if len(calls) <= len(outcomes):
            raise outcomes[len(calls) - 1]
        return True
    return call, calls


def retry(seconds):
    return TelegramRetryAfter(method=METHOD, message="Too Many Requests",
                              retry_after=seconds)


async def test_waits_and_retries_after_flood_limit(fresh):
    call, calls = script(retry(3))
    assert await deleting.one(call, CHAT) is True
    assert len(calls) == 2
    assert fresh and fresh[0] >= 2.5            # ждали сколько просили
    assert not errorlog.recent()


async def test_gone_message_is_not_retried_and_not_reported():
    call, calls = script(TelegramBadRequest(method=METHOD,
                                            message="message to delete not found"))
    assert await deleting.one(call, CHAT) is False
    assert len(calls) == 1
    assert not errorlog.recent()


async def test_network_hiccup_is_retried():
    call, calls = script(TelegramNetworkError(method=METHOD, message="timeout"))
    assert await deleting.one(call, CHAT) is True
    assert len(calls) == 2


async def test_long_wait_gives_up_and_says_so():
    call, calls = script(retry(600))
    assert await deleting.one(call, CHAT) is False
    assert len(calls) == 1
    assert "600" in errorlog.recent()[-1]


async def test_endless_limit_gives_up_after_retries():
    call, calls = script(*[retry(1)] * 10)
    assert await deleting.one(call, CHAT) is False
    assert len(calls) == deleting.RETRIES
    assert "повторы кончились" in errorlog.recent()[-1]


async def test_pause_is_shared_within_chat_only(fresh):
    call, _ = script(retry(20))
    await deleting.one(call, CHAT)               # чат упёрся в лимит
    fresh.clear()
    ok, _ = script()
    await deleting.one(ok, OTHER)
    assert fresh == []                           # соседний чат не ждёт
    await deleting.one(ok, CHAT)
    assert fresh and fresh[0] > 15               # а этот ждёт общую паузу


async def test_complaints_are_throttled():
    for _ in range(5):
        call, _ = script(retry(600))
        await deleting.one(call, CHAT)
    assert len(errorlog.recent()) == 1


class Bot:
    def __init__(self, batch_error=None):
        self.batches, self.singles = [], []
        self.batch_error = batch_error

    async def delete_messages(self, chat_id, ids):
        self.batches.append(list(ids))
        if self.batch_error:
            raise self.batch_error
        return True

    async def delete_message(self, chat_id, mid):
        self.singles.append(mid)
        return True


async def test_many_goes_in_batches_of_100():
    bot = Bot()
    ids = list(range(1, 251)) + [5, 0]           # повтор и ноль отбрасываются
    assert await deleting.many(bot, CHAT, ids) == 250
    assert [len(b) for b in bot.batches] == [100, 100, 50]
    assert bot.singles == []


async def test_many_falls_back_to_singles_on_bad_batch():
    bot = Bot(TelegramBadRequest(method=METHOD, message="message can't be deleted"))
    assert await deleting.many(bot, CHAT, [1, 2, 3]) == 3
    assert bot.singles == [1, 2, 3]


async def test_many_does_not_hammer_when_rate_limited():
    bot = Bot(retry(600))
    assert await deleting.many(bot, CHAT, list(range(1, 201))) == 0
    assert len(bot.batches) == 1                 # вторую пачку не шлём
    assert bot.singles == []                     # и по одному не добиваем
