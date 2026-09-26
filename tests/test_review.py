"""Очередь разбора: кнопка метит нужную часть случая, и случай уходит из очереди.

Ломается молча: «только текст», который заодно пометил профиль нормой, учит
сравнение профилей на спамере со взломанным видом. А случай, который после
разметки остался в очереди, человек будет размечать по второму кругу.
"""
import types

from gremlin import db
from gremlin.handlers import spam_bot
from gremlin.services import cases

from conftest import OWNER, make_user

PDATA = {"bio": "пиши в лс", "channel_title": "", "channel_desc": "",
         "channel_username": "", "photo_id": ""}


def click(data):
    shown = {}

    async def edit_text(text, reply_markup=None, **kw):
        shown["text"] = text

    async def answer(*a, **kw):
        pass

    return types.SimpleNamespace(
        data=data, message=types.SimpleNamespace(edit_text=edit_text),
        from_user=types.SimpleNamespace(id=OWNER), answer=answer), shown


async def labels(case_id):
    return {("prof" if p["origin"] == "profile" else "msg"): (p["label"], p["labeled_by"])
            for p in await db.case_parts(case_id)}


async def test_only_text_leaves_profile_undecided_and_case_leaves_queue(chat):
    older = await cases.record(None, chat, make_user(41), msg_label="unknown",
                               prof_label="unknown", origin="manual", feature="ban",
                               text="продам гараж недорого, пишите", pdata=PDATA)
    newer = await cases.record(None, chat, make_user(42), msg_label="spam",
                               prof_label="unknown", origin="auto", feature="стоп-слово",
                               text="заработок от 3000 в день, пиши плюс", pdata=PDATA)
    assert await db.review_next() == newer and await db.review_count() == 2

    cb, shown = click(f"r:msg:{newer}")
    await spam_bot.review_mark(cb)
    assert await labels(newer) == {"msg": ("spam", OWNER), "prof": ("unknown", None)}
    assert await db.review_next() == older          # размеченный ушёл из очереди
    assert "продам гараж" in shown["text"]          # и сразу показан следующий

    cb, shown = click(f"r:ok:{older}")
    await spam_bot.review_mark(cb)
    assert await labels(older) == {"msg": ("ok", OWNER), "prof": ("ok", OWNER)}
    assert await db.review_count() == 0 and "Очередь пуста" in shown["text"]
