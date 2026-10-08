"""Вход в чат: в больших чатах служебного сообщения нет, и встречать надо по
chat_member. Ошибки тут молчаливые — новичка просто никто не встретил или
встретили дважды."""
import time
import types

import pytest

from gremlin import db
from gremlin.handlers import events, group
from gremlin.services import moderation

from conftest import OWNER, FakeBot, Msg, make_chat, make_user

NEW = 8201


@pytest.fixture
def greeting(chat, members, monkeypatch):
    monkeypatch.setattr(group, "_met", {})
    monkeypatch.setattr(group, "_last_welcome", {})

    async def nothing(*a, **kw):
        return None
    monkeypatch.setattr(events, "_sub_close", nothing)
    monkeypatch.setattr(moderation, "revoke_unban_link", nothing)


def member_update(user, actor):
    return types.SimpleNamespace(
        chat=make_chat(), from_user=actor, date=time.time(),
        via_join_request=True,
        old_chat_member=types.SimpleNamespace(status="left", user=user),
        new_chat_member=types.SimpleNamespace(status="member", user=user))


async def test_quiet_join_is_greeted_once(chat, greeting):
    await db.set_setting(chat, "welcome_on", 1)
    await db.set_setting(chat, "watch_on", 0)
    await db.ans_add("welcome", chat, "Привет, {name}!")
    bot = FakeBot()
    newbie = make_user(NEW)

    # заявку одобрил сам бот, служебного сообщения нет — встречаем всё равно
    await events.member_updated(member_update(newbie, make_user(bot.id)), bot)
    assert len(bot.sent) == 1 and f"tg://user?id={NEW}" in bot.sent[0][1]

    # в маленьком чате следом приходит и служебное — второго привета нет
    service = Msg(author=newbie)
    service.new_chat_members = [newbie]
    service.date = time.time()

    async def delete():
        pass
    service.delete = delete
    await group.on_join(service, bot)
    assert len(bot.sent) == 1


async def test_manual_forever_mute_stays_in_list(chat, greeting, monkeypatch):
    """Вечный мут руками в Telegram: aiogram отдаёт 1970 год, а не None."""
    from datetime import datetime, timezone
    from gremlin.services import net

    async def nothing(*a, **kw):
        return None
    monkeypatch.setattr(net, "spread_and_note", nothing)
    user = make_user(NEW)
    upd = types.SimpleNamespace(
        chat=make_chat(), from_user=make_user(OWNER), date=time.time(),
        via_join_request=False,
        old_chat_member=types.SimpleNamespace(status="member", user=user),
        new_chat_member=types.SimpleNamespace(
            status="restricted", user=user, is_member=True, can_send_messages=False,
            until_date=datetime(1970, 1, 1, tzinfo=timezone.utc)))
    await events.member_updated(upd, FakeBot())
    [row] = await db.active_punishments(chat)
    assert row["user_id"] == NEW and row["until_ts"] is None
