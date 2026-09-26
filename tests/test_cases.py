"""Случаи: каждое решение оставляет в копилке разметку, и исход доходит до неё.

Всё здесь ломается молча. Снятое наказание, после которого профиль так и
лежит спамом, учит сравнение профилей на обычном человеке. Признак, который
выдаёт ответ («после бана он не в чате»), даёт модели сто процентов на
обучении и ноль в работе. А кнопка «Не трогать», которая не знает, про кого
она, — это разметка, выброшенная в корзину.
"""
import types

from gremlin import db
from gremlin.handlers import cards
from gremlin.services import cases

from conftest import OWNER, make_user

PDATA = {"bio": "Мой приватный канал 🔞👇", "channel_title": "", "channel_desc": "",
         "channel_username": "", "photo_id": "x"}


async def labels(case_id):
    cur = await db._db.execute(
        "SELECT origin, label, labeled_by FROM samples WHERE case_id = ? ORDER BY id",
        (case_id,))
    return [tuple(r) for r in await cur.fetchall()]


async def test_lift_makes_whole_case_norm(chat):
    """Бан правилом снят — нормой становится и сообщение, и профиль."""
    user = make_user(77, "Катя", "katya")
    pid = await db.add_punishment(chat, 77, "katya", "Катя", "ban", "стоп-слово", None, None)
    case = await cases.record(None, chat, user, msg_label="spam", prof_label="unknown",
                              origin="auto", feature="стоп-слово",
                              text="заработок без вложений", pid=pid, pdata=PDATA)
    assert await labels(case) == [("auto", "spam", None), ("profile", "unknown", None)]

    assert await cases.settle(await db.case_of_pid(pid), "ok", OWNER, chat)
    assert await labels(case) == [("card", "ok", OWNER), ("profile", "ok", OWNER)]
    # и оба доходят до моделей: сообщение — до текстовой, профиль — до сравнения
    assert any(r["case_id"] == case for r in await db.samples_pool("msg"))
    assert any(r["case_id"] == case for r in await db.samples_pool("prof"))


async def test_features_do_not_leak_the_ban(chat):
    """Признаки — как было до наказания, а не после него."""
    user = make_user(78)
    pid = await db.add_punishment(chat, 78, None, "Юзер78", "ban", "профиль", None, None,
                                  was_member=True)
    case = await cases.record(None, chat, user, msg_label="unknown", prof_label="spam",
                              origin="auto", feature="профиль", text="привет всем",
                              pid=pid, pdata=PDATA)
    cur = await db._db.execute("SELECT data FROM samples WHERE case_id = ?", (case,))
    import json
    for r in await cur.fetchall():
        feats = json.loads(r["data"])["features"]
        assert feats["pun"] == 0 and feats["member"] is True and feats["photo"] is True


def click(data, chat_id):
    async def edit_text(*a, **kw):
        raise RuntimeError("старая карточка")

    async def answer(*a, **kw):
        pass

    message = types.SimpleNamespace(chat=types.SimpleNamespace(id=chat_id), message_id=5,
                                    html_text="", edit_text=edit_text)
    return types.SimpleNamespace(data=data, message=message, bot=None,
                                 from_user=types.SimpleNamespace(id=OWNER), answer=answer)


async def test_leave_alone_marks_suspect_case_norm(chat):
    """«Не трогать» на карточке подозрения делает случай нормой, старая кнопка не падает."""
    user = make_user(79)
    case = await cases.record(None, chat, user, msg_label="unknown", prof_label="unknown",
                              origin="auto", feature="наблюдение",
                              text="гляньте мой канал", pdata=PDATA)
    await cards.card_watch_ok(click("k:wok", chat))
    assert {lab for _o, lab, _b in await labels(case)} == {"unknown"}

    await cards.card_watch_ok(click(f"k:wok:{chat}:79", chat))
    assert {lab for _o, lab, _b in await labels(case)} == {"ok"}


async def test_verdict_gets_its_case(chat):
    """Вердикт знает свой случай — у него признаки, у случая исход."""
    await db.verdict_add(chat, 80, 70, 70, "ban", "стоп-слово/ban", "{}", "[]", "текст")
    case = await cases.record(None, chat, make_user(80), msg_label="spam", prof_label=None,
                              origin="auto", feature="стоп-слово", text="текст спама")
    cur = await db._db.execute("SELECT case_id FROM verdicts WHERE user_id = 80")
    assert (await cur.fetchone())["case_id"] == case
