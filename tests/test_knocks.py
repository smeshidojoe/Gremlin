"""Кто стучался к боту: личка чужого человека и зов бота в чужой чат.

Место молчаливое: отказ человек видит, а хозяин бота — нет. Если запись
перестанет появляться, никто этого не заметит, поэтому проверяем её здесь.
"""
import datetime as dt

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import Chat, Message, User

from gremlin import db
from gremlin.middlewares import TrackingMiddleware

STRANGER = 7700


class Session(BaseSession):
    """Telegram, который проглатывает ответ «доступ закрыт»."""

    async def close(self):
        pass

    async def stream_content(self, *a, **kw):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        return Message(message_id=900, date=dt.datetime.now(dt.timezone.utc),
                       chat=Chat(id=STRANGER, type="private"), text="ok")


@pytest.fixture
async def bot():
    b = Bot(token="1:test", session=Session())
    yield b
    await b.session.close()


def dm(uid=STRANGER, username="stranger", name="Чужой"):
    return Message(
        message_id=1, date=dt.datetime.now(dt.timezone.utc),
        chat=Chat(id=uid, type="private"), text="/start",
        from_user=User(id=uid, is_bot=False, first_name=name, username=username),
    )


async def pass_through(message, bot):
    """Прогнать сообщение через мидлварь. Вернуть (дошло ли до обработчика)."""
    got = []

    async def handler(event, data):
        got.append(event)
        return "done"

    await TrackingMiddleware()(handler, message.as_(bot), {})
    return bool(got)


async def test_denied_dm_is_recorded(database, bot):
    assert await pass_through(dm(), bot) is False
    row = await db.knock_get(STRANGER)
    assert (row["dm_cnt"], row["add_cnt"], row["allowed"]) == (1, 0, 0)
    assert (row["username"], row["name"]) == ("stranger", "Чужой")
    # отказали — в общую таблицу людей такой не попадает
    assert await db.get_user(STRANGER) is None


async def test_second_dm_counts_not_duplicates(database, bot):
    await pass_through(dm(), bot)
    await pass_through(dm(), bot)
    rows = await db.knock_list()
    assert len(rows) == 1
    assert rows[0]["dm_cnt"] == 2
    assert rows[0]["first_ts"] <= rows[0]["last_ts"]


async def test_allowed_dm_marked_allowed(database, bot):
    await db.access_add(STRANGER, None, None)
    assert await pass_through(dm(), bot) is True
    row = await db.knock_get(STRANGER)
    assert (row["dm_cnt"], row["allowed"]) == (1, 1)


async def test_chat_invite_recorded(database):
    await db.knock_chat(STRANGER, "stranger", "Чужой", "Свой чат")
    await db.knock_chat(STRANGER, "stranger", "Чужой", "Другой чат")
    row = await db.knock_get(STRANGER)
    assert (row["add_cnt"], row["dm_cnt"]) == (2, 0)
    assert row["last_chat"] == "Другой чат"


async def test_knock_remove(database, bot):
    await pass_through(dm(), bot)
    await db.knock_remove(STRANGER)
    assert await db.knock_list() == []
