"""Паузы после перезагрузки системы.

time.monotonic() считает время с запуска системы. Пока оно меньше самой паузы,
запись «никогда не срабатывало», записанная нулём, читается как «только что»:
паста молчала, реакции считались уже проверенными, триггеры не отвечали. Это
всплыло на живой машине после перезагрузки.
"""
import types

import pytest

from gremlin import config, db, utils
from gremlin.handlers import events, group
from gremlin.services import triggers, watch

from conftest import CHAT, FakeBot, Msg, make_chat, make_user

UID = 9800


@pytest.fixture
def just_booted(monkeypatch):
    """Система загрузилась пять секунд назад."""
    monkeypatch.setattr(group.time, "monotonic", lambda: 5.0)
    monkeypatch.setattr(events.time, "monotonic", lambda: 5.0)


def test_never_is_not_zero():
    assert utils.NEVER < -1e9


async def test_paste_answers_right_after_boot(chat, just_booted, monkeypatch):
    got = []

    async def send_answer(message, ans, **kw):
        got.append(ans["text"])

    monkeypatch.setattr(triggers, "send_answer", send_answer)
    monkeypatch.setattr(group, "_paste_fired", {})
    await db.ans_add("paste", chat, "не читал, но осуждаю")
    await db.set_setting(chat, "games_on", config.GAME_PASTE)
    await db.set_setting(chat, "paste_min", 300)
    await db.set_setting(chat, "paste_cd", 15)
    s = await db.get_settings(chat)
    await group.fire_paste(FakeBot(), Msg("буква " * 100), s)
    assert got == ["не читал, но осуждаю"]


async def test_trigger_answers_right_after_boot(chat, just_booted, monkeypatch):
    sent = []

    async def send(message, trig, **kw):
        sent.append(trig["phrase"])

    monkeypatch.setattr(triggers, "send", send)
    monkeypatch.setattr(group, "_trig_fired", {})
    rid = await db.trig_add(chat, "привет", None)
    await db.trig_set(rid, "cooldown", 30)
    await db.set_setting(chat, "trig_on", 1)
    s = await db.get_settings(chat)
    await group.fire_trigger(FakeBot(), Msg("привет всем"), s)
    assert sent == ["привет"]


async def test_reaction_checked_right_after_boot(chat, members, just_booted,
                                                 monkeypatch):
    calls = []

    async def fake_check(bot, chat_, user, settings, message=None, lvl=None,
                         event="message", nn_hit=False):
        calls.append(event)

    monkeypatch.setattr(watch, "check_user", fake_check)
    monkeypatch.setattr(events, "_reacted", {})
    await db.set_setting(chat, "watch_on", 1)
    await db.set_setting(chat, "watch_react", 1)
    update = types.SimpleNamespace(
        user=make_user(UID), chat=make_chat(chat),
        new_reaction=[types.SimpleNamespace(type="emoji", emoji="👍")])
    await events.reaction_put(update, FakeBot())
    assert calls == ["reaction"]
