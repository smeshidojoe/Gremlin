"""Наказания: мут не-участнику, сроки, сетка, список активных."""
import time

import pytest

from gremlin import config, db, utils
from gremlin.handlers import user_menu as um
from gremlin.services import moderation, net

from conftest import OWNER, FakeBot, make_user

PEER = -1001000000002
WEEK = 7 * 24 * 60


async def test_mute_for_non_member_becomes_ban_with_same_term(chat, members):
    user = make_user(4242, "Олег", "chomkaaaa")
    members["outside"].add(user.id)
    bot = FakeBot()
    pid, err = await moderation.punish_ex(bot, chat, user, "mute", WEEK,
                                          "это не чат леши", OWNER)
    assert err is None
    row = await db.get_punishment(pid)
    assert row["kind"] == "ban"
    left = row["until_ts"] - int(time.time())
    assert WEEK * 60 - 60 < left <= WEEK * 60
    assert bot.banned[-1][2] is not None       # телеграму отдан срок


async def test_plain_ban_is_forever(chat, members):
    pid, _ = await moderation.punish_ex(FakeBot(), chat, make_user(5555), "ban",
                                        0, "спам", OWNER)
    assert (await db.get_punishment(pid))["until_ts"] is None


async def test_net_spreads_the_term_not_the_swapped_ban(chat, members,
                                                        monkeypatch):
    """Семидневный мут не должен расходиться по сетке вечным баном."""
    monkeypatch.setattr(config, "NET_DELAY", 0)
    await db.upsert_chat(PEER, "Соседний", None, OWNER, "supergroup")
    await db.get_settings(PEER)
    nid = await db.net_create(OWNER, "Сетка")
    await db.net_assign(chat, nid)
    await db.net_assign(PEER, nid)
    await db.net_set(nid, "sync_mask", config.NET_BAN | config.NET_MUTE)

    user = make_user(4242, "Олег", "chomkaaaa")
    members["outside"].add(user.id)
    bot = FakeBot()
    done, skipped, failed = await net.spread(bot, chat, user, "mute", WEEK,
                                             "это не чат леши", OWNER)
    assert (done, failed) == (1, 0)
    peer = (await db.active_punishments(PEER))[0]
    assert peer["kind"] == "ban"
    assert peer["until_ts"] is not None


@pytest.mark.parametrize("raw,clean,swapped", [
    ("сетка · каментики: это не чат леши", "это не чат леши", False),
    ("сетка · каментики: это не чат леши · мут не-участнику невозможен, "
     "заменён баном на тот же срок", "это не чат леши", True),
    ("спам · мут не-участнику невозможен, заменён баном", "спам", True),
    ("стоп-слово: 🔞", "стоп-слово: 🔞", False),
    ("стоп-слово: сетка казино", "стоп-слово: сетка казино", False),
    (None, "—", False),
])
def test_short_reason(raw, clean, swapped):
    assert utils.short_reason(raw) == (clean, swapped)


def test_name_link():
    assert utils.name_link(1, "Олег", "chomkaaaa") \
        == '<a href="https://t.me/chomkaaaa">Олег</a>'
    assert utils.name_link(777, "Наталия") \
        == '<a href="tg://user?id=777">Наталия</a>'


async def test_active_list_shows_what_matters(chat):
    soon = int(time.time()) + 3600
    await db.add_punishment(
        chat, 21, "chomkaaaa", "Олег", "ban",
        "сетка · каментики: это не чат леши · мут не-участнику невозможен, "
        "заменён баном на тот же срок", soon, OWNER)
    text, _kb = await um.view_active(chat)
    assert '<a href="https://t.me/chomkaaaa">Олег</a>' in text
    assert "это не чат леши" in text
    assert "мут→бан" in text
    assert "выдан " in text and "до " in text
    assert "сетка ·" not in text and "не-участнику" not in text
