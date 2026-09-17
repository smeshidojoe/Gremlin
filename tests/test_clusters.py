"""Копилка улик и кучки похожих.

Кучку складывает смысл текста, а не пометки: рядом с тремя рекламными
объявлениями в ней лежат десятки обычных сообщений. Раньше кнопка кучки
перезаписывала оценку у всех — одно нажатие делало обычный разговор «спамом».
"""
import time

import pytest

from gremlin import db
from gremlin.handlers import user_menu as um
from gremlin.services import nn

from conftest import CB, CHAT, OWNER, Sent

USER = 9400


def buttons(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


async def _add(origin, label, text):
    return await db.sample_add(CHAT, USER, origin, label, text)


@pytest.fixture
def cache(monkeypatch):
    """Готовая разбивка вместо модели: кучки считаются нейросетью."""
    monkeypatch.setattr(nn, "_clusters", {})

    def put(buckets, scope="profile"):
        nn._clusters[CHAT] = (time.monotonic(), buckets, [], scope)
    return put


async def test_stats_keep_people_profiles_apart(chat):
    await _add("auto", "spam", "казино бонус")
    await _add("random", "ok", "привет всем")
    await _add("manual", "unknown", "ручной мут")
    await _add("profile", "spam", "Анна 18+ пиши в лс")
    await _add("profile", "spam", "Кристина в лс")
    await _add("profile", "ok", "Вася")
    st = await db.samples_stats(CHAT)
    assert (st["spam"], st["ok"], st["unknown"]) == (1, 1, 1)
    assert (st["faces_spam"], st["faces_ok"]) == (2, 1)
    assert st["profile"] == 2 and st["total"] == 6


async def test_cluster_button_labels_only_unmarked(chat, cache):
    spam = await _add("auto", "spam", "казино")
    ok = await _add("random", "ok", "правда, меня")
    raw = [await _add("manual", "unknown", f"ручной {i}") for i in range(3)]
    face = await _add("profile", "unknown", "профиль")
    cache([[spam, ok, *raw, face]], scope="unknown")

    assert await nn.label_cluster(CHAT, 0, "spam") == 3
    labels = {r["id"]: r["label"] for r in await db.samples_by_ids(CHAT, [spam, ok, *raw, face])}
    assert labels[ok] == "ok"                     # норма нормой и осталась
    assert labels[spam] == "spam"
    assert all(labels[i] == "spam" for i in raw)
    assert labels[face] == "unknown"              # профили людей — не сюда
    # повторное нажатие ничего не меняет
    cache([[spam, ok, *raw]], scope="unknown")
    assert await nn.label_cluster(CHAT, 0, "ok") == 0


async def test_single_sample_label_is_scoped(chat):
    sid = await _add("random", "ok", "обычное")
    face = await _add("profile", "spam", "профиль")
    assert await db.sample_set_label(CHAT, sid, "spam") is True
    assert (await db.samples_by_ids(CHAT, [sid]))[0]["label"] == "spam"
    assert await db.sample_set_label(CHAT - 1, sid, "ok") is False   # чужой чат
    assert await db.sample_set_label(CHAT, face, "ok") is False      # профиль


async def test_menu_buttons_depend_on_tab(chat, monkeypatch):
    groups = [{"size": 70, "sample": "правда", "words": ["правда"],
               "spam": 3, "ok": 67, "unknown": 0},
              {"size": 5, "sample": "ручной", "words": ["ручной"],
               "spam": 0, "ok": 0, "unknown": 5}]

    async def fake_clusters(cid, scope):
        return groups
    monkeypatch.setattr(nn, "clusters", fake_clusters)

    text, kb = await um.view_clusters(CHAT, "unknown")
    data = buttons(kb)
    # у первой кучки размечать нечего — кнопок нет; у второй — только её 5
    assert f"u:nnl:{CHAT}:0:spam:unknown" not in data
    assert f"u:nnl:{CHAT}:1:spam:unknown" in data
    assert "(4%)" in text                         # доля спама видна

    _text, kb = await um.view_clusters(CHAT, "profile")
    data = buttons(kb)
    assert not [d for d in data if d.startswith("u:nnl:")]   # оптом не размечается
    assert f"u:nni:{CHAT}:0:0" in data


async def test_menu_fix_one_sample(chat, cache):
    ids = [await _add("random", "ok", f"сообщение {i}") for i in range(7)]
    cache([ids])
    text, kb = await um.view_cluster_items(CHAT, 0, 0)
    assert "Кучка 1" in text and "7 шт" in text
    assert f"u:nns:{CHAT}:0:{ids[0]}:spam:0" in buttons(kb)
    assert f"u:nni:{CHAT}:0:1" in buttons(kb)     # вторая страница

    msg = Sent(text)
    cb = CB(f"u:nns:{CHAT}:0:{ids[0]}:spam:0", uid=OWNER, message=msg)
    await um.cb_cluster_item_label(cb)
    assert cb.alerts == ["Поправлено"]
    labels = [r["label"] for r in await db.samples_by_ids(CHAT, ids)]
    assert labels == ["spam"] + ["ok"] * 6
    assert "спамом помечено 1" in msg.text


async def test_stale_split_is_reported(chat, cache):
    cb = CB(f"u:nni:{CHAT}:3:0", uid=OWNER)
    await um.cb_cluster_items(cb)
    assert "устарела" in cb.alerts[-1]
