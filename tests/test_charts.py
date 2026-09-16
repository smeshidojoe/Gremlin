"""Ряды для графиков панели.

Графики читают те же таблицы, что и всё остальное, поэтому ошибиться легко в
границах: сутки считаются местные, период отрезается по началу первых суток, а
пустые дни обязаны остаться в ряду нулями — иначе неделя молчания на картинке
превращается в короткий провал между соседними точками.
"""
import json
import time

import pytest

from gremlin import config, db, utils
from gremlin.services import moderation

from conftest import CB, CHAT, OWNER

USER = 9100


async def _event(chat_id, kind, ts):
    await db._db.execute(
        "INSERT INTO events (chat_id, ts, kind, text) VALUES (?, ?, ?, ?)",
        (chat_id, ts, kind, "x"))
    await db._db.commit()


async def _punish(chat_id, kind, reason, ts, by_id=None):
    pid = await db.add_punishment(chat_id, USER, None, None, kind, reason,
                                  None, by_id)
    await db._db.execute("UPDATE punishments SET created = ? WHERE id = ?",
                         (ts, pid))
    await db._db.commit()
    return pid


async def _msgs(chat_id, day, cnt):
    await db._db.execute(
        "INSERT INTO msg_stats (chat_id, user_id, day, cnt) VALUES (?, ?, ?, ?)",
        (chat_id, USER, day, cnt))
    await db._db.commit()


async def test_series_covers_every_day_including_empty(chat):
    today = utils.day_num()
    await _msgs(chat, today, 5)
    await _msgs(chat, today - 3, 2)

    d = await db.chart_series(chat, 7)
    assert len(d["series"]) == 7
    assert [r["msgs"] for r in d["series"]] == [0, 0, 0, 2, 0, 0, 5]
    assert d["series"][-1]["day"] == today
    # ряд идёт подряд, без дыр: соседние сутки отличаются ровно на день
    days = [r["day"] for r in d["series"]]
    assert days == list(range(today - 6, today + 1))


async def test_old_data_stays_outside_the_window(chat):
    today = utils.day_num()
    await _msgs(chat, today, 3)
    await _msgs(chat, today - 20, 100)
    week = await db.chart_series(chat, 7)
    month = await db.chart_series(chat, 30)
    assert sum(r["msgs"] for r in week["series"]) == 3
    assert sum(r["msgs"] for r in month["series"]) == 103


async def test_joins_and_leaves_land_on_their_day(chat):
    now = int(time.time())
    await _event(chat, "join", now)
    await _event(chat, "join", now)
    await _event(chat, "leave", now)
    await _event(chat, "join", now - 2 * 86400)
    await _event(chat, "other", now)           # чужой вид событий не считаем

    d = await db.chart_series(chat, 7)
    by_day = {r["day"]: r for r in d["series"]}
    today = utils.day_num(now)
    assert by_day[today]["joins"] == 2
    assert by_day[today]["leaves"] == 1
    assert by_day[utils.day_num(now - 2 * 86400)]["joins"] == 1


async def test_punishment_kinds_reasons_and_hours(chat):
    now = int(time.time())
    await _punish(chat, "mute", "стоп-слово: казино", now)
    await _punish(chat, "ban", "внешняя ссылка на t.me", now)
    await _punish(chat, "ban", "наблюдение: 90 очков", now)
    await _punish(chat, "mute", "мешает людям", now, by_id=OWNER)
    await _punish(chat, "ban", "старое", now - 40 * 86400)

    d = await db.chart_series(chat, 7)
    assert d["kinds"] == {"mute": 2, "ban": 2}
    assert sum(d["hours"]) == 4
    hour = (now + config.TZ_OFFSET * 3600) % 86400 // 3600
    assert d["hours"][hour] == 4

    # правила считает уже панель — здесь важно, что причины доехали целыми
    rules = {}
    for r in d["reasons"]:
        key = (moderation.forgive_scope(r["reason"]) or "other") if r["by_bot"] else "manual"
        rules[key] = rules.get(key, 0) + r["count"]
    assert rules == {"words": 1, "links": 1, "watch": 1, "manual": 1}


async def test_forgiven_by_rule(chat):
    await db.forgive_add(chat, USER, None, None, "words", "стоп-слово", OWNER)
    await db.forgive_add(chat, USER + 1, None, None, "words", "стоп-слово", OWNER)
    await db.forgive_add(chat, USER + 2, None, None, "links", "ссылка", OWNER)
    d = await db.chart_series(chat, 30)
    assert d["forgiven"] == {"words": 2, "links": 1}


async def test_empty_chat_gives_zeros_not_errors(chat):
    d = await db.chart_series(chat, 90)
    assert len(d["series"]) == 90
    assert not any(r["msgs"] or r["joins"] or r["leaves"] for r in d["series"])
    assert d["kinds"] == {} and d["forgiven"] == {} and d["reasons"] == []
    assert d["hours"] == [0] * 24


# ---------- маршрут панели ----------

class Req(dict):
    """Запрос к API без сервера: панели хватает пути, подписи и строки запроса."""

    def __init__(self, cid, uid=OWNER, **query):
        super().__init__(user={"id": uid})
        self.match_info = {"cid": str(cid)}
        self.query = {k: str(v) for k, v in query.items()}


async def _charts(cid, **query):
    from gremlin.web import api
    resp = await api.api_charts(Req(cid, **query))
    return json.loads(resp.text)


async def test_route_names_rules_and_guards_the_range(chat):
    now = int(time.time())
    await _punish(chat, "mute", "стоп-слово: казино", now)
    await _punish(chat, "mute", "капча не пройдена", now)
    await _punish(chat, "ban", "мешает людям", now, by_id=OWNER)
    await db.forgive_add(chat, USER, None, None, "words", "стоп-слово", OWNER)

    d = await _charts(chat, days=7)
    assert d["days"] == 7 and len(d["series"]) == 7
    assert d["totals"]["punished"] == 3
    rules = {r["label"]: r["count"] for r in d["rules"]}
    assert rules == {"стоп-слова": 1, "прочее": 1, "вручную": 1}
    assert [k["label"] for k in d["kinds"]][0] == "мут"       # чаще всего
    assert d["forgiven"] == [{"key": "words", "label": "стоп-слова", "count": 1}]
    assert len(d["hours"]) == 24

    # период берём только из своего списка: 1000 суток никто не просил
    assert (await _charts(chat, days=1000))["days"] == 30
    assert (await _charts(chat, days="всё"))["days"] == 30
    assert (await _charts(chat))["days"] == 30


async def test_route_refuses_a_stranger(chat):
    from aiohttp import web
    from gremlin.web import api
    with pytest.raises(web.HTTPForbidden) as err:
        await api.api_charts(Req(chat, uid=4242))
    assert "not your chat" in err.value.text


# ---------- расширенная сводка ----------

async def test_chat_stats_has_week_over_week(chat):
    today = utils.day_num()
    await _msgs(chat, today, 10)
    await _msgs(chat, today - 1, 4)
    await _msgs(chat, today - 10, 100)          # прошлая неделя
    await db._db.execute(
        "INSERT INTO msg_stats (chat_id, user_id, day, cnt) VALUES (?, ?, ?, ?)",
        (chat, USER + 1, today, 3))
    await db._db.commit()

    st = await db.chat_stats(chat)
    assert st["d1"] == 13 and st["y1"] == 4
    assert st["d7"] == 17 and st["p7"] == 100   # окно прошлой недели отдельно
    assert st["d30"] == 117
    assert st["people7"] == 2
    assert st["since"] == utils.day_ts(today - 10)


async def test_chat_stats_on_empty_chat(chat):
    st = await db.chat_stats(chat)
    assert st["y1"] == 0 and st["p7"] == 0 and st["people7"] == 0
    assert st["since"] is None and st["pun30"] == 0


async def test_menu_stats_page_renders(chat):
    from gremlin.handlers import user_menu as um
    await _msgs(chat, utils.day_num(), 5)
    await _msgs(chat, utils.day_num() - 10, 20)
    cb = CB(f"u:st:{chat}")
    await um.cb_stats(cb)
    text = cb.message.text
    assert "Сообщений" in text and "Считаем с" in text
    assert "Писали за 7д" in text
    # прошлая неделя была живее — это видно строкой, а не голым числом
    assert "% к прошлой" in text


async def test_previous_period_for_comparison(chat):
    now = int(time.time())
    today = utils.day_num()
    await _msgs(chat, today, 10)            # в окне
    await _msgs(chat, today - 8, 40)        # предыдущая неделя
    await _msgs(chat, today - 40, 999)      # и это уже никого не касается
    await _event(chat, "join", now)
    await _event(chat, "join", now - 8 * 86400)
    await _event(chat, "leave", now - 8 * 86400)
    await _punish(chat, "ban", "стоп-слово: казино", now - 8 * 86400)

    d = await db.chart_series(chat, 7)
    assert sum(r["msgs"] for r in d["series"]) == 10
    assert d["prev"] == {"msgs": 40, "joins": 1, "leaves": 1, "punished": 1}


async def test_route_sends_previous_period_and_manual_count(chat):
    now = int(time.time())
    await _punish(chat, "mute", "мешает людям", now, by_id=OWNER)
    await _punish(chat, "mute", "стоп-слово: казино", now)
    d = await _charts(chat, days=7)
    assert d["totals"]["manual"] == 1
    assert set(d["prev"]) == {"msgs", "joins", "leaves", "punished"}
