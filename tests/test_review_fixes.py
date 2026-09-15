"""Исправления по код-ревью: права, пересылки, капча, фоновые задачи."""
import asyncio
import json
import time
import types

import pytest
from aiohttp import web

from gremlin import db, runtime, utils
from gremlin.handlers import cards, events, group
from gremlin.services import adm_cache, profile
from gremlin.services import status as st
from gremlin.web import api

from conftest import CB, CHAT, OWNER, FakeBot, Sent

MINE, THEIRS = CHAT - 10, CHAT - 11
ME, THEM, STRANGER = 5551, 5552, 5553
CHANNEL = -1002000000077


@pytest.fixture
async def two_chats(database):
    for cid, owner in ((MINE, ME), (THEIRS, THEM)):
        await db.upsert_chat(cid, f"Чат {owner}", None, owner, "supergroup")
        await db.get_settings(cid)


class Req(dict):
    """Запрос панели без сети: подпись уже проверена, дальше — только права."""

    def __init__(self, cid, rid=None, uid=ME, body=None):
        super().__init__(user={"id": uid})
        self.match_info = {"cid": str(cid)}
        if rid is not None:
            self.match_info["rid"] = str(rid)
        self.app = {"bot": FakeBot()}
        self.query = {}
        self._body = body or {}

    async def json(self):
        return self._body


# ---------- 6. фоновые задачи ----------

async def test_spawn_keeps_reference_until_done():
    gate = asyncio.Event()

    async def job():
        await gate.wait()

    task = runtime.spawn(job())
    assert task in runtime._tasks
    gate.set()
    await task
    await asyncio.sleep(0)
    assert task not in runtime._tasks


# ---------- 1, 4, 11. панель: чужие записи ----------

async def test_foreign_trigger_untouchable(two_chats):
    theirs = await db.trig_add(THEIRS, "чужая фраза", None)
    mine = await db.trig_add(MINE, "своя фраза", None)
    for handler in (api.api_trig, api.api_trig_edit, api.api_trig_del):
        with pytest.raises(web.HTTPNotFound):
            await handler(Req(MINE, theirs, body={"phrase": "взлом"}))
    assert (await db.trig_get(theirs))["phrase"] == "чужая фраза"
    await api.api_trig_del(Req(MINE, mine))
    assert await db.trig_get(mine) is None


async def test_foreign_counter_untouchable(two_chats):
    await db.cmd_add(THEIRS, "!чужой", "ответ", 30)
    theirs = (await db.cmd_find(THEIRS, "!чужой"))["id"]
    with pytest.raises(web.HTTPNotFound):
        await api.api_cmd_edit(Req(MINE, theirs, body={"reset": 1}))
    with pytest.raises(web.HTTPNotFound):
        await api.api_cmd_del(Req(MINE, theirs))
    assert await db.cmd_get(theirs) is not None


async def test_foreign_link_whitelist_untouchable(two_chats):
    await db.link_wl_add(THEIRS, CHANNEL, "chan", "Канал")
    rid = (await db.link_wl_list(THEIRS))[0]["id"]
    await api.api_linkwl_del(Req(MINE, rid))
    assert len(await db.link_wl_list(THEIRS)) == 1
    await api.api_linkwl_del(Req(THEIRS, rid, uid=THEM))
    assert await db.link_wl_list(THEIRS) == []


async def test_paste_and_sub_answers_deletable(two_chats):
    for owner in ("paste", "sub"):
        mine = await db.ans_add(owner, MINE, "моя заготовка")
        theirs = await db.ans_add(owner, THEIRS, "чужая заготовка")
        await api.api_answer_del(Req(MINE, mine))
        assert await db.ans_get(mine) is None
        with pytest.raises(web.HTTPForbidden):
            await api.api_answer_del(Req(MINE, theirs))
    assert await api._ans_owner_ok("paste", MINE, MINE)
    assert not await api._ans_owner_ok("paste", THEIRS, MINE)
    assert not await api._ans_owner_ok("чепуха", MINE, MINE)


async def test_spam_profile_bad_user_id_is_400(two_chats):
    with pytest.raises(web.HTTPBadRequest):
        await api.api_spam_profile(Req(MINE, body={"user_id": "abc"}))


# ---------- 2. кнопки карточек ----------

async def test_stranger_cannot_press_card(chat, members):
    bot = FakeBot()
    cb = CB(f"k:ban:{chat}:{THEM}", uid=STRANGER)
    await cards.card_ban(cb, bot)
    assert bot.banned == []
    assert cb.alerts == ["Эти кнопки — для админов чата."]


async def test_chat_admin_can_press_card(chat, members, monkeypatch):
    members["admins"].add(STRANGER)
    monkeypatch.setattr(cards, "_mark", lambda *a, **kw: asyncio.sleep(0))
    bot = FakeBot()
    cb = CB(f"k:ban:{chat}:{THEM}", uid=STRANGER)
    await cards.card_ban(cb, bot)
    assert [b[:2] for b in bot.banned] == [(chat, THEM)]


async def test_stranger_cannot_approve_request(chat, members):
    bot = FakeBot()
    cb = CB(f"sub:ok:{chat}:{THEM}", uid=STRANGER)
    await events.sub_take(cb, bot)
    assert bot.approved == []


# ---------- 7. «Забанить» пишет профиль целиком ----------

async def test_card_ban_remembers_full_profile(chat, members, monkeypatch):
    monkeypatch.setattr(profile, "_cache", {})
    monkeypatch.setattr(cards, "_mark", lambda *a, **kw: asyncio.sleep(0))

    class Bot(FakeBot):
        async def get_chat_member(self, cid, uid):
            return types.SimpleNamespace(status="kicked", user=types.SimpleNamespace(
                id=uid, full_name="Анна 18+", username="anna_dm"))

        async def get_chat(self, cid):
            if cid == THEM:
                return types.SimpleNamespace(id=THEM, bio="пиши в лс", photo=None,
                                             personal_chat=None)
            return await super().get_chat(cid)

    await cards.card_ban(CB(f"k:ban:{chat}:{THEM}", uid=OWNER), Bot())
    faces = [r["text"] for r in await db.samples_of_origin(chat, "profile")]
    assert faces == ["Анна 18+ @anna_dm · пиши в лс"]


# ---------- 3. пересылки из своего ----------

async def test_own_forward(chat, monkeypatch):
    monkeypatch.setattr(adm_cache, "_linked", {})

    class Bot(FakeBot):
        async def get_chat(self, cid):
            if cid == chat:
                return types.SimpleNamespace(id=cid, type="supergroup",
                                             linked_chat_id=CHANNEL)
            return types.SimpleNamespace(id=cid, type="channel", username="linked",
                                         title="Канал", linked_chat_id=chat)

    bot = Bot()
    origin = lambda cid, uname=None: types.SimpleNamespace(id=cid, username=uname)
    assert await group._own_forward(bot, chat, origin(chat))
    assert await group._own_forward(bot, chat, origin(CHANNEL))
    assert not await group._own_forward(bot, chat, origin(-100300, "spam"))
    await db.link_wl_add(chat, None, "friends", "Друзья")
    assert await group._own_forward(bot, chat, origin(-100400, "Friends"))


# ---------- 5. капча переживает перезапуск ----------

async def test_captcha_resumed_and_overdue_kicked(chat, monkeypatch):
    real_sleep = asyncio.sleep

    async def fast_sleep(delay, *a, **kw):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(group, "_captcha_pending", {})
    await db.kv_set(group._CAPTCHA_KEY.format(chat, THEM),
                    json.dumps({"msg": 77, "until": int(time.time()) - 5}))
    await db.kv_set(f"captcha:{chat}:broken", "{}")

    bot = FakeBot()
    assert await group.resume_captcha(bot) == 1
    await asyncio.gather(*list(runtime._tasks))

    assert [b[:2] for b in bot.banned] == [(chat, THEM)]
    assert bot.unbanned == [(chat, THEM)]
    assert await db.kv_prefix("captcha:") == []
    assert group._captcha_pending == {}


# ---------- 13, 9. членство ----------

async def test_not_member_error_is_an_answer(monkeypatch):
    monkeypatch.setattr(adm_cache, "_members", {})

    class Bot(FakeBot):
        def __init__(self, error):
            super().__init__()
            self.error = error

        async def get_chat_member(self, cid, uid):
            raise RuntimeError(self.error)

    assert await adm_cache.is_member(Bot("Bad Request: user not found"), CHAT, 1) is False
    assert adm_cache.member_cached(CHAT, 1) is False
    assert await adm_cache.is_member(Bot("Timeout"), CHAT, 2) is True
    assert adm_cache.member_cached(CHAT, 2) is None


def test_note_member(monkeypatch):
    monkeypatch.setattr(adm_cache, "_members", {})
    adm_cache.note_member(CHAT, 3, False)
    assert adm_cache.member_cached(CHAT, 3) is False


# ---------- 10, 8. проверка статуса ----------

async def test_short_id_not_found_inside_other_numbers(chat):
    await db.add_event(chat, "leave", "Кто-то (1777)")
    assert await db.user_seen_chats(77, [chat]) == set()
    await db.add_event(chat, "leave", "Сам (77)")
    assert await db.user_seen_chats(77, [chat]) == {chat}


async def test_status_dates_only_from_viewer_chats(chat, monkeypatch):
    monkeypatch.setattr(profile, "_cache", {})
    other = CHAT - 20
    today = utils.day_num()
    await db._db.execute(
        "INSERT INTO users (user_id, username, first_name, first_seen, last_seen)"
        " VALUES (?, ?, ?, ?, ?)", (THEM, None, "Катя", 1, int(time.time())))
    await db._db.execute(
        "INSERT INTO msg_stats (chat_id, user_id, day, cnt) VALUES (?, ?, ?, 3), (?, ?, ?, 9)",
        (chat, THEM, today - 5, other, THEM, today))
    await db._db.commit()

    class Bot(FakeBot):
        async def get_chat_member(self, cid, uid):
            return types.SimpleNamespace(status="member", user=None)

    d = await st.collect(Bot(), THEM, [{"chat_id": chat, "title": "Мой"}])
    day = st._day_date(today - 5)
    # сообщение в чужом чате сегодня не должно попасть в «последнее»
    assert d["facts"] == [f"✉️ пишет в ваших чатах с {day} · последнее сообщение {day}"]
