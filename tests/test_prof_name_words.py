"""Стоп-слова профиля смотрят и на имя с ником, а не только на описание.

Молчаливое место: если проверка снова начнёт читать одно описание, рекламный
аккаунт вроде «Green VPN 🌿» с безобидным «о себе» просто пройдёт мимо, и
никакой ошибки в логах не будет.
"""
import pytest

from gremlin import db
from gremlin.services import filters as flt, watch

from conftest import make_user

DATA = {"bio": "тут про меня", "channel_title": "", "channel_desc": "",
        "photo_id": ""}


@pytest.fixture(autouse=True)
def fresh_words(monkeypatch):
    """Списки слов кэшируются на процесс, а база у каждого теста своя."""
    monkeypatch.setattr(flt, "_word_cache", {})
    monkeypatch.setattr(flt, "_word_weights", {})


async def settings_for(chat):
    s = await db.get_settings(chat)
    s.prof_on, s.prof_words, s.watch_nn = 1, 1, 0   # сравнение с базой — не здесь
    return s


async def test_stopword_in_name_is_found(chat):
    await db.words_add(chat, "vpn", "strict", "prof")
    user = make_user(9500, name="Green VPN 🌿", username="greenvpn_name_ts45w")
    got = await watch.profile_check(None, chat, user, await settings_for(chat), DATA)
    assert got is not None
    _data, finds = got
    assert any("vpn" in f.lower() for f in finds)


async def test_clean_profile_still_clean(chat):
    await db.words_add(chat, "vpn", "strict", "prof")
    user = make_user(9501, name="Катя", username="katya")
    assert await watch.profile_check(None, chat, user,
                                     await settings_for(chat), DATA) is None


async def test_stopword_in_bio_still_found(chat):
    await db.words_add(chat, "промокод", "stem", "prof")
    user = make_user(9502, name="Катя", username="katya")
    data = dict(DATA, bio="забирай промокоды в канале")
    got = await watch.profile_check(None, chat, user, await settings_for(chat), data)
    assert got is not None


async def test_promo_words_added_once(chat):
    await db.words_add(chat, "vpn", "strict", "prof")
    assert await db.add_promo_prof_words() == 2
    words = {r["word"] for r in await db.words_list(chat, "prof")}
    assert {"промокод", "скидк"} <= words
    assert await db.add_promo_prof_words() == 0      # второй запуск молчит


async def test_promo_words_skip_chat_without_list(chat):
    """Списка профильных слов нет — правка его не заводит: это включило бы
    проверку профиля там, где её не просили."""
    assert await db.add_promo_prof_words() == 0
    assert await db.words_list(chat, "prof") == []
