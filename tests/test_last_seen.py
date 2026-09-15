"""«Последняя активность» человека: сообщения в чате должны её двигать."""
import types

from gremlin import db, utils
from gremlin.services import profile
from gremlin.services import status as st

from conftest import CHAT, FakeBot

U = 9400
DAY = 86400


async def test_chat_message_moves_last_seen(chat, monkeypatch):
    start = 1_780_000_000
    monkeypatch.setattr(db, "_now", lambda: start)
    await db.msg_inc(chat, U, "katya", "Катя")
    monkeypatch.setattr(db, "_now", lambda: start + 30 * DAY)
    await db.msg_inc(chat, U, None, None)
    row = await db.get_user(U)
    assert row["first_seen"] == start
    assert row["last_seen"] == start + 30 * DAY
    assert (row["username"], row["first_name"]) == ("katya", "Катя")


async def test_status_uses_latest_message_for_old_rows(chat, monkeypatch):
    """Старые записи с застывшим last_seen: дату берём из счётчика сообщений."""
    monkeypatch.setattr(profile, "_cache", {})
    first = 1_780_000_000
    await db._db.execute(
        "INSERT INTO users (user_id, username, first_name, first_seen, last_seen)"
        " VALUES (?, ?, ?, ?, ?)", (U, "katya", "Катя", first, first))
    today = utils.day_num()
    await db._db.execute(
        "INSERT INTO msg_stats (chat_id, user_id, day, cnt) VALUES (?, ?, ?, ?)",
        (chat, U, today, 5))
    await db._db.commit()

    class Bot(FakeBot):
        async def get_chat_member(self, cid, uid):
            return types.SimpleNamespace(status="member", user=None)

    d = await st.collect(Bot(), U, [{"chat_id": chat, "title": "Чат"}])
    fact = d["facts"][0]
    assert fact.startswith(f"👁 бот впервые заметил его {st._date(first)}")
    assert fact.endswith(f"последняя активность {st._day_date(today)}")
