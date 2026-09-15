"""Освобождение от наблюдения действует на всех путях, которые в него ведут.

Сообщение, вход в чат и реакция — три двери в одно наблюдение. Реакция
проверяла только полный игнор, и человека, прощённого кнопкой «больше не
трогать: наблюдение», через 25 минут забанило за поставленную реакцию.
"""
import types

import pytest

from gremlin import db
from gremlin.handlers import events
from gremlin.services import watch

from conftest import CHAT, FakeBot, make_chat, make_user

UID = 5345435504


def reaction(uid=UID, cid=CHAT):
    return types.SimpleNamespace(
        user=make_user(uid, "Катришкинс", "katr1shasss"),
        chat=make_chat(cid),
        new_reaction=[types.SimpleNamespace(type="emoji", emoji="👍")])


@pytest.fixture
def watched(monkeypatch, members):
    """Кого наблюдение взялось проверять."""
    calls = []

    async def fake_check(bot, chat, user, settings, message=None, lvl=None,
                         event="message", nn_hit=False):
        calls.append((user.id, event))

    monkeypatch.setattr(watch, "check_user", fake_check)
    monkeypatch.setattr(events, "_reacted", {})
    return calls


async def enable_reactions(cid):
    await db.set_setting(cid, "watch_on", 1)
    await db.set_setting(cid, "watch_react", 1)


async def test_reaction_goes_to_watch(chat, watched):
    await enable_reactions(chat)
    await events.reaction_put(reaction(), FakeBot())
    assert watched == [(UID, "reaction")]


async def test_forgiven_for_watch_is_not_checked_on_reaction(chat, watched):
    """Ровно тот случай из живого чата."""
    await enable_reactions(chat)
    await db.forgive_add(chat, UID, "katr1shasss", "Катришкинс", "watch",
                         "наблюдение: профиль как у забаненных (76%)", 1)
    await events.reaction_put(reaction(), FakeBot())
    assert watched == []


async def test_whitelisted_for_watch_is_not_checked_on_reaction(chat, watched):
    await enable_reactions(chat)
    await db.wl_set_scopes(chat, UID, "katr1shasss", "Катришкинс", {"watch"})
    await events.reaction_put(reaction(), FakeBot())
    assert watched == []


async def test_forgiven_for_other_rule_is_still_checked(chat, watched):
    """Прощение узкое: простили за стоп-слова — наблюдение работает."""
    await enable_reactions(chat)
    await db.forgive_add(chat, UID, None, "Катришкинс", "words", "стоп-слово", 1)
    await events.reaction_put(reaction(), FakeBot())
    assert watched == [(UID, "reaction")]


async def test_reaction_check_respects_switches(chat, watched):
    await db.set_setting(chat, "watch_on", 1)
    await db.set_setting(chat, "watch_react", 0)
    await events.reaction_put(reaction(), FakeBot())
    assert watched == []


async def test_removed_reaction_is_ignored(chat, watched):
    await enable_reactions(chat)
    upd = reaction()
    upd.new_reaction = []
    await events.reaction_put(upd, FakeBot())
    assert watched == []
