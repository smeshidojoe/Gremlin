"""Общая копилка и сборщик: что куда пишется и что доходит до модели.

Всё здесь ломается молча. Раньше набор сборщика отключался, как только у
чата набиралось двести своих примеров, — три тысячи примеров не работали
нигде, и ни одной ошибки в логах. Профиль, записанный спамом вместе со
спам-сообщением со взломанного аккаунта, учит модель на обычном человеке.
А выгрузка, после загрузки которой строка для модели выходит другой, тихо
портит набор при каждом переезде.
"""
import types

import numpy as np
import pytest

from gremlin import db
from gremlin.handlers import spam_bot
from gremlin.services import nn

from conftest import OWNER


@pytest.fixture
def fake_model(monkeypatch):
    """Модель, которая отличает «спам» от прочего по слову в тексте."""
    async def ensure():
        return True

    async def embed(texts):
        return np.array([[1.0, 0.0] if "реклама" in t else [0.0, 1.0] for t in texts],
                        dtype=np.float32)

    monkeypatch.setattr(nn, "ensure", ensure)
    monkeypatch.setattr(nn, "embed", embed)
    monkeypatch.setattr(nn, "_np", np)
    nn.invalidate()
    yield
    nn.invalidate()


async def test_collector_reaches_busy_chat(chat, fake_model):
    """У чата своих примеров больше двухсот — набор сборщика всё равно в работе."""
    for i in range(250):
        await db.sample_add(chat, 1000 + i, "random", "ok", f"обычная болтовня номер {i}")
    for i in range(5):
        await db.seed_add(f"реклама заработка номер {i} пиши", "spam", "msg")
    await db.seed_commit()

    _matrix, labels, _ids, _w = await nn.profile(chat)
    assert labels.count("spam") == 5
    got = await nn.check(chat, "реклама заработка, пиши в лс")
    assert got is not None and got["score"] > 50


def click(what: str, msg_id: int = 1):
    async def edit_text(*a, **kw):
        pass

    async def answer(*a, **kw):
        pass

    message = types.SimpleNamespace(message_id=msg_id, html_text="", edit_text=edit_text)
    return types.SimpleNamespace(data=f"s:{what}", message=message,
                                 from_user=types.SimpleNamespace(id=OWNER), answer=answer)


def case(uid=77):
    return {"uid": uid, "who": "", "asked": True, "seen": [],
            "text": "Доход от 3000 в день, пиши плюс",
            "msg": {"text": "Доход от 3000 в день, пиши плюс", "links": ["https://t.me/+x"]},
            "prof": "Алиса @alisa77 · обычная девушка, люблю котиков",
            "prof_data": {"name": "Алиса", "username": "alisa77",
                          "bio": "обычная девушка, люблю котиков"}}


async def labels_of(kind):
    return {r["text"]: r["label"] for r in await db.samples_pool(kind)}


async def test_only_text_skips_profile(database):
    spam_bot._pending[1] = case()
    await spam_bot.mark(click("msg"))
    assert await labels_of("msg") == {case()["text"]: "spam"}
    assert await labels_of("prof") == {}


async def test_only_profile_keeps_message_as_norm(database):
    spam_bot._pending[1] = case()
    await spam_bot.mark(click("prof"))
    assert await labels_of("msg") == {case()["text"]: "ok"}
    assert await labels_of("prof") == {case()["prof"]: "spam"}
    rows = await db.samples_pool("msg") + await db.samples_pool("prof")
    assert len({r["case_id"] for r in rows}) == 1 and rows[0]["case_id"]   # один случай
    assert {r["labeled_by"] for r in rows} == {OWNER}


async def test_export_import_keeps_model_text(database):
    """После выгрузки и загрузки модель видит ровно те же строки."""
    spam_bot._pending[1] = case()
    await spam_bot.mark(click("all"))
    await db.seed_add("Олеся @malloware · резидент", "ok", "prof")    # старая: без полей
    await db.seed_commit()
    before = (await labels_of("msg"), await labels_of("prof"))

    records = await db.pool_export()
    assert len(records) == 2                       # пара — одним случаем
    pair = next(r for r in records if "message" in r and "profile" in r)
    assert pair["message"]["links"] == ["https://t.me/+x"]
    assert pair["profile"]["username"] == "alisa77"

    await db.seed_clear()
    for rec in records:
        await spam_bot.load_case(rec, OWNER)
    await db.seed_commit()
    assert (await labels_of("msg"), await labels_of("prof")) == before
