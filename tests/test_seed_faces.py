"""Профили в наборе: вид строки и сравнение с нормой.

Всё здесь ломается молча. Пример в наборе не того вида, что проверяемый
профиль, — сходство просто выходит ниже, и бан не случается. Норма, не
дошедшая до сравнения, — живого человека со «18+» в канале банят как спам.
"""
import numpy as np

from gremlin import config, db
from gremlin.handlers.spam_bot import parse_profile
from gremlin.services import nn, profile as prof_svc

from conftest import make_user


def test_form_matches_live_check():
    """Форма, заполненная вразнобой и с ником без «@», даёт ту же строку,
    что и живой профиль."""
    got, seen = parse_profile("Канал: Анюта18\nНик: anna\nИмя: Анна | 18+\n"
                              "О себе: пиши в лс\nОписание канала: \nеще строка")
    user = make_user(1, name="Анна | 18+", username="anna")
    live = prof_svc.face_text(user, {"bio": "пиши в лс", "channel_title": "Анюта18"})
    assert got == live + " · еще строка"
    assert "свободная строка" in seen


async def test_reshape_old_form_rows(database):
    old = "Имя: Helen Smith · Ник: @helen · О себе: Кончи со мной 🔞"
    await db.seed_add(old, "spam", "prof")
    await db.seed_add("Helen Smith @helen · Кончи со мной 🔞", "spam", "prof")  # пересылка
    await db.seed_add("Имя: Олеся · Ник: @malloware · О себе: резидент", "ok", "prof")
    await db.seed_add("🥴🥴🥴 · НЕподземелье Meev (18+)", "ok", "prof")
    await db.seed_commit()

    assert await db.reshape_seed_profiles() == (1, 1)
    texts = {r["text"] for r in await db.samples_seed_faces(99, "spam")}
    assert texts == {"Helen Smith @helen · Кончи со мной 🔞"}     # дубль ушёл
    texts = {r["text"] for r in await db.samples_seed_faces(99, "ok")}
    assert texts == {"Олеся @malloware · резидент", "🥴🥴🥴 · НЕподземелье Meev (18+)"}
    assert await db.reshape_seed_profiles() == (0, 0)             # второй раз молчит


async def test_reshape_keeps_conflicting_labels(database):
    """Один профиль и спамом, и нормой — противоречие человека, а не дубль:
    решать за него не нам, обе записи остаются."""
    await db.seed_add("Имя: Анна · О себе: пиши мне", "spam", "prof")
    await db.seed_add("Анна · пиши мне", "ok", "prof")
    await db.seed_commit()
    assert await db.reshape_seed_profiles() == (1, 0)


def test_face_hit_needs_to_beat_norm():
    top = config.PROFILE_SIM + 10
    assert nn.face_hit((top, None))
    assert nn.face_hit((top, top - 1))
    assert not nn.face_hit((top, top))              # на норму похож не меньше
    assert not nn.face_hit((config.PROFILE_SIM - 1, None))
    assert not nn.face_hit(None)


async def test_norm_reaches_comparison(chat, monkeypatch):
    """Нормальные профили из набора и правда участвуют в сравнении."""
    vec = {"spam": [1.0, 0.0], "ok": [0.0, 1.0]}

    async def ensure():
        return True

    async def embed(texts):
        return np.array([vec["ok" if "норм" in t else "spam"] for t in texts])

    monkeypatch.setattr(nn, "ensure", ensure)
    monkeypatch.setattr(nn, "embed", embed)
    nn.invalidate()
    for i in range(5):
        await db.seed_add(f"реклама номер {i} пиши в лс", "spam", "prof")
    await db.seed_add("просто нормальный человек", "ok", "prof")
    await db.seed_commit()

    assert await nn.face_score(chat, "ещё один нормальный") == (0, 100)
    assert await nn.face_score(chat, "реклама пиши") == (100, 0)
    nn.invalidate()
