"""Стоп-слова: поиск, основы, отдельный список профилей и вес слова."""
import pytest

from gremlin import config, db
from gremlin.handlers import user_menu as um
from gremlin.services import filters as flt

from conftest import CB


@pytest.fixture(autouse=True)
def fresh_cache():
    flt._word_cache.clear()
    flt._word_weights.clear()


async def test_strict_word_matches_whole_word_only(chat):
    await db.words_add(chat, "оплата", "strict")
    flt.invalidate_words(chat)
    assert await flt.match_stopword(chat, "будет оплата сегодня") == "оплата"
    assert await flt.match_stopword(chat, "предоплатами") is None


async def test_stem_word_matches_any_ending(chat):
    await db.words_add(chat, "изнасилован", "stem")
    flt.invalidate_words(chat)
    assert await flt.match_stopword(chat, "темы про изнасилования") \
        == "изнасилования"


async def test_message_and_profile_lists_are_separate(chat):
    await db.words_add(chat, "малолет", "stem")
    await db.words_add(chat, "18+", "strict", "prof")
    flt.invalidate_words(chat)
    assert await flt.match_stopword(chat, "малолетка", "msg") == "малолетка"
    assert await flt.match_stopword(chat, "малолетка", "prof") is None
    assert await flt.match_stopword(chat, "мне 18+ пиши", "prof") == "18+"


async def test_new_word_is_strong_by_default(chat):
    await db.words_add(chat, "onlyfans", "strict")
    row = (await db.words_list(chat))[0]
    assert row["weight"] == config.UNI_W_STOPWORD


async def test_weight_is_saved_and_validated(chat):
    await db.words_add(chat, "оплата", "strict")
    rid = (await db.words_list(chat))[0]["id"]
    await db.words_set_weight(rid, 15)
    assert (await db.words_get(rid))["weight"] == 15
    with pytest.raises(ValueError):
        await db.words_set_weight(rid, 99)


async def test_weight_found_through_stem(chat):
    """«изнасилования» поймано основой — вес берётся у слова из списка."""
    await db.words_add(chat, "изнасилован", "stem")
    rid = (await db.words_list(chat))[0]["id"]
    await db.words_set_weight(rid, 30)
    flt.invalidate_words(chat)
    got = await flt.match_stopword(chat, "не переношу темы про изнасилования")
    assert await flt.stopword_weight(chat, got) == 30
    assert await flt.stopword_weight(chat, "чего-то нет") \
        == config.UNI_W_STOPWORD


async def test_changing_list_drops_cache(chat):
    await db.words_add(chat, "казино", "strict")
    flt.invalidate_words(chat)
    assert await flt.match_stopword(chat, "казино") == "казино"
    await db.words_clear(chat)
    flt.invalidate_words(chat)
    assert await flt.match_stopword(chat, "казино") is None


async def test_menu_cycles_weight(chat, monkeypatch):
    await db.words_add(chat, "onlyfans", "strict")
    rid = (await db.words_list(chat))[0]["id"]

    async def allow(cb, cid):
        return True

    monkeypatch.setattr(um, "_guard", allow)
    text, kb = await um.view_words(chat)
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"u:wdw:{chat}:0:{rid}" in data
    assert "Вес — сколько слово значит" in text

    order = list(config.WORD_WEIGHTS)
    start = (await db.words_get(rid))["weight"]
    cb = CB(f"u:wdw:{chat}:0:{rid}")
    await um.cb_word_weight(cb)
    after = (await db.words_get(rid))["weight"]
    assert after == order[(order.index(start) + 1) % len(order)]
    assert cb.alerts and "улика" in cb.alerts[0]


async def test_profile_words_page_has_weight_buttons(chat):
    await db.words_add(chat, "18+", "strict", "prof")
    text, kb = await um.view_prof_words(chat)
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert any(d.startswith(f"u:pww:{chat}:") for d in data)
