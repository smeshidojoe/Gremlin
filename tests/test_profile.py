"""Проверка профиля: участники, аватарка, порог откровенности."""
import pytest

from gremlin import config, db
from gremlin.services import filters as flt
from gremlin.services import nsfw, profile, watch

from conftest import FakeBot, Msg, make_chat, make_user

MEMBER, GUEST = 5001, 5002

# профиль, за который прилетел ложный бан: «18+» в описании своего же канала
GROLREX = {"bio": "Клоунада", "channel_title": "grolrex.rawr",
           "channel_desc": "18+ i quess.. Все прикольное в закрепе",
           "channel_username": "gro1rex", "photo_id": ""}


@pytest.fixture
async def prof_chat(chat, members, cards, monkeypatch):
    asked = []

    async def fetch(bot, uid):
        asked.append(uid)
        return dict(GROLREX)

    monkeypatch.setattr(profile, "fetch", fetch)
    members["outside"].add(GUEST)
    for key, val in (("watch_on", 1), ("prof_on", 1), ("prof_photo", 0),
                     ("prof_mode", "punish"), ("prof_punish", "ban")):
        await db.set_setting(chat, key, val)
    await db.words_add(chat, "18+", "strict", "prof")
    flt.invalidate_words(chat)
    return asked


async def test_member_checked_by_default(prof_chat, chat):
    s = await db.get_settings(chat)
    bot = FakeBot()
    await watch.check_user(bot, make_chat(), make_user(MEMBER), s,
                           Msg("привет"), None, nn_hit=True)
    assert prof_chat == [MEMBER]
    assert [u for _c, u, _t in bot.banned] == [MEMBER]


async def test_members_left_alone_when_switched_off(prof_chat, chat):
    await db.set_setting(chat, "prof_members", 0)
    s = await db.get_settings(chat)
    bot = FakeBot()
    await watch.check_user(bot, make_chat(), make_user(MEMBER), s,
                           Msg("привет"), None, nn_hit=True)
    assert prof_chat == [] and bot.banned == []


async def test_guest_still_checked_when_members_off(prof_chat, chat):
    await db.set_setting(chat, "prof_members", 0)
    s = await db.get_settings(chat)
    bot = FakeBot()
    await watch.check_user(bot, make_chat(), make_user(GUEST), s,
                           Msg("привет всем"), None)
    assert prof_chat == [GUEST]


async def test_newcomer_checked_on_join_always(prof_chat, chat):
    await db.set_setting(chat, "prof_members", 0)
    s = await db.get_settings(chat)
    await watch.check_user(FakeBot(), make_chat(), make_user(MEMBER), s,
                           None, None, event="join")
    assert prof_chat == [MEMBER]


async def test_avatar_gives_points_not_a_ban(chat, monkeypatch):
    await db.set_setting(chat, "prof_on", 1)
    await db.set_setting(chat, "prof_photo", 1)
    s = await db.get_settings(chat)

    async def photo_bytes(bot, data):
        return b"IMG"

    async def score(raw):
        return 99

    monkeypatch.setattr(profile, "photo_bytes", photo_bytes)
    monkeypatch.setattr(nsfw, "score", score)
    data = {"bio": "", "channel_title": "", "channel_desc": "",
            "channel_username": "", "photo_id": "F"}
    assert await watch.profile_check(FakeBot(), chat, make_user(10), s, data) \
        is None
    pts, why = await watch.photo_points(FakeBot(), chat, make_user(10), s, data)
    assert pts == s.prof_photo_score
    assert "откровенная аватарка (99%)" in why[0]


def test_low_nsfw_thresholds_removed():
    """На 85% модель записывала в наготу портрет в платье."""
    assert min(config.NSFW_PRESETS) >= 95


async def test_photo_threshold_default_is_97(chat):
    assert (await db.get_settings(chat)).prof_photo_min == 97
