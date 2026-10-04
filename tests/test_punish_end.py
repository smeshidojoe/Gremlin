"""Как закончилось наказание: панель показывает завершённые за сутки с
пометкой. Ошибки тут молчаливые — истёкшее, погашенное позже заодно, выглядело
бы снятым, а снятый руками мут капчи всё равно кончался бы киком."""
import time

from gremlin import db
from gremlin.handlers import group

from conftest import FakeBot, make_chat, make_user


async def test_end_marks(chat):
    now = int(time.time())
    old = await db.add_punishment(chat, 1, None, None, "mute", "игра", now - 60, None)
    await db.deactivate_user_punishments(chat, 1)    # срок уже вышел
    lifted = await db.add_punishment(chat, 2, None, None, "mute", "x", now + 600, None)
    await db.deactivate_punishment(lifted)
    first = await db.add_punishment(chat, 3, None, None, "mute", "x", now + 600, None)
    await db.add_punishment(chat, 3, None, None, "ban", "y", None, None)

    ends = {r["id"]: db.punishment_end(r, now)[0]
            for r in await db.recent_punishments(chat, now - 86400)}
    assert ends[old] == "expired"
    assert ends[lifted] == "lifted"
    assert ends[first] == "replaced"


async def test_captcha_mute_listed_and_lift_cancels_kick(chat, members, monkeypatch):
    bot = FakeBot()
    s = await db.get_settings(chat)
    monkeypatch.setattr(group.runtime, "spawn", lambda coro: coro.close())
    assert await group.ask_captcha(bot, make_chat(), make_user(7), s)
    p = await db.active_punishment_of(chat, 7, "mute")
    assert p is not None and p["reason"].startswith("капча")

    await db.deactivate_punishment(p["id"])          # админ снял из списка
    group._captcha_pending[(chat, 7)] = 1
    monkeypatch.setattr(group.asyncio, "sleep", _no_sleep)
    await group._captcha_timeout(bot, chat, 7, 0, 1, p["id"])
    assert bot.banned == []                          # кика нет


async def _no_sleep(*a, **kw):
    pass
