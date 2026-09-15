"""Проверка статуса человека, права бота в чате и длинные кулдауны."""
import time
import types
from datetime import datetime, timezone

import pytest

from gremlin import config, db, schema, userbot, utils
from gremlin.handlers import user_menu as um
from gremlin.services import adm_cache, profile, resolve
from gremlin.services import status as st

from conftest import CHAT, OWNER, FakeBot

U = 9100
OTHER, THIRD, QUIET, FOREIGN = CHAT - 1, CHAT - 2, CHAT - 3, CHAT - 4


# ---------- подписи и пресеты ----------

@pytest.mark.parametrize("sec,label", [
    (5, "5 секунд"), (1, "1 секунда"), (90, "90 секунд"),
    (60, "1 минута"), (180, "3 минуты"), (2400, "40 минут"), (3600, "60 минут"),
])
def test_fmt_seconds(sec, label):
    assert utils.fmt_seconds(sec) == label


def test_long_presets():
    assert {2400, 3600} <= set(config.CMD_COOLDOWN_PRESETS)
    assert max(config.CAPTCHA_TIMEOUT_PRESETS) > 600
    assert max(config.ASR_MAX_SEC_PRESETS) > 600
    captcha = schema.SECTION_BY_KEY["captcha"].fields[1]
    assert schema.value_label(captcha, 3600) == "60 минут"


async def test_punishments_page_has_status_button(chat):
    _text, kb = await um.view_punishments(chat)
    assert any(btn.callback_data == f"u:ps:{chat}"
               for row in kb.inline_keyboard for btn in row)


# ---------- права бота ----------

class RightsBot(FakeBot):
    def __init__(self, member=None, fail=False):
        super().__init__()
        self.member, self.fail, self.calls = member, fail, 0

    async def get_chat_member(self, cid, uid):
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        return self.member


def admin(**rights):
    base = {k: True for k, _ in adm_cache.BOT_NEEDS}
    base.update(rights)
    return types.SimpleNamespace(status="administrator", **base)


@pytest.fixture(autouse=True)
def fresh_caches(monkeypatch):
    monkeypatch.setattr(adm_cache, "_bot_status", {})
    monkeypatch.setattr(profile, "_cache", {})


async def test_bot_rights_ok_and_cached():
    bot = RightsBot(admin())
    got = await adm_cache.bot_status(bot, CHAT)
    assert got["state"] == "ok" and "в порядке" in got["text"]
    await adm_cache.bot_status(bot, CHAT)
    assert bot.calls == 1
    adm_cache.invalidate_bot_status(CHAT)
    await adm_cache.bot_status(bot, CHAT)
    assert bot.calls == 2


async def test_bot_rights_missing():
    got = await adm_cache.bot_status(
        RightsBot(admin(can_invite_users=False, can_delete_messages=False)), CHAT)
    assert got["state"] == "missing"
    assert got["missing"] == ["удалять сообщения", "приглашать (ссылки и заявки)"]


@pytest.mark.parametrize("status,state", [("member", "not_admin"),
                                          ("left", "gone"), ("kicked", "gone")])
async def test_bot_not_admin(status, state):
    got = await adm_cache.bot_status(
        RightsBot(types.SimpleNamespace(status=status)), CHAT)
    assert got["state"] == state


async def test_bot_status_failure_not_cached():
    bot = RightsBot(fail=True)
    assert (await adm_cache.bot_status(bot, CHAT))["state"] == "unknown"
    await adm_cache.bot_status(bot, CHAT)
    assert bot.calls == 2


# ---------- кого проверять ----------

async def test_parse_id_and_link(monkeypatch):
    async def by_username(bot, uname):
        return {"katya": (U, "Катя"), "somechan": (-100500, "Канал")}.get(
            uname.lower(), (None, None))

    monkeypatch.setattr(resolve, "by_username", by_username)
    bot = FakeBot()
    assert await st.parse_target(bot, " 9100 ") == (U, "")
    for text in ("@katya", "https://t.me/katya", "t.me/katya/", "katya"):
        assert (await st.parse_target(bot, text))[0] == U, text
    uid, err = await st.parse_target(bot, "@nobody_here")
    assert uid is None and "@nobody_here" in err
    uid, err = await st.parse_target(bot, "@somechan")
    assert uid is None and "канал" in err.lower()
    assert (await st.parse_target(bot, "что это?"))[0] is None


async def test_parse_forward():
    bot = FakeBot()
    fwd = types.SimpleNamespace(forward_origin=types.SimpleNamespace(
        sender_user=types.SimpleNamespace(id=U)))
    assert await st.parse_target(bot, None, fwd) == (U, "")
    hidden = types.SimpleNamespace(forward_origin=types.SimpleNamespace(
        sender_user=None, sender_user_name="Скрытый"))
    uid, err = await st.parse_target(bot, None, hidden)
    assert uid is None and "скрыл" in err


# ---------- сама карточка ----------

def member(status, **kw):
    user = types.SimpleNamespace(id=U, full_name="Катя Новая", username="katya_new",
                                 is_premium=True)
    return types.SimpleNamespace(status=status, user=user, **kw)


class StatusBot(FakeBot):
    def __init__(self, members):
        super().__init__()
        self.members = members

    async def get_chat_member(self, cid, uid):
        m = self.members[cid]
        if isinstance(m, Exception):
            raise m
        return m

    async def get_chat(self, cid):
        if cid == U:
            return types.SimpleNamespace(
                id=U, bio="пиши в лс, всё расскажу", photo=None,
                personal_chat=types.SimpleNamespace(id=-100777, title="Заработок",
                                                    username="money_chan"))
        return await super().get_chat(cid)


async def test_collect(chat, monkeypatch):
    soon = int(time.time()) + 3600
    forever = datetime(1970, 1, 1, tzinfo=timezone.utc)   # так aiogram отдаёт 0
    bot = StatusBot({
        CHAT: member("kicked", until_date=forever),
        OTHER: member("member"),
        THIRD: RuntimeError("chat not found"),
        QUIET: member("left"),
    })
    joined = int(time.time()) - 400 * 86400

    async def joined_at(cid, uid):
        return joined if cid == OTHER else None

    monkeypatch.setattr(userbot, "joined_at", joined_at)

    # новая запись гасит прежние активные в том же чате — активную кладём последней
    old = await db.add_punishment(CHAT, U, "katya", "Катя", "mute", "флуд",
                                  soon, OWNER)
    await db.deactivate_punishment(old)
    await db.add_punishment(CHAT, U, "katya", "Катя", "ban",
                            "наблюдение: профиль (80)", None, OWNER)
    await db.add_punishment(OTHER, U, "katya", "Катя", "ban",
                            "сетка · Первый: наблюдение", None, OWNER)
    await db.add_punishment(OTHER, U, "katya", "Катя", "mute", "стоп-слово",
                            soon, OWNER)
    await db.add_punishment(FOREIGN, U, "katya", "Катя", "ban", "чужой чат",
                            None, OWNER)
    day = utils.day_num()
    await db._db.execute(
        "INSERT INTO msg_stats (chat_id, user_id, day, cnt) VALUES (?, ?, ?, ?), (?, ?, ?, ?)",
        (OTHER, U, day - 10, 30, OTHER, U, day, 12))
    await db._db.commit()
    await db.add_event(CHAT, "watch", f"ban: Катя ({U}) — профиль (80)")
    await db.add_event(CHAT, "leave", f"Кто-то ({U}1)")          # чужой id с тем же началом
    await db.add_event(CHAT, "bot", f"лог-чат установлен: {U}")  # не про людей

    chats = [{"chat_id": CHAT, "title": "Первый"},
             {"chat_id": OTHER, "title": "Второй"},
             {"chat_id": THIRD, "title": "Третий"},
             {"chat_id": QUIET, "title": "Тихий"}]
    d = await st.collect(bot, U, chats, first=OTHER)

    assert (d["name"], d["username"], d["premium"]) == ("Катя Новая", "katya_new", True)
    assert d["about"] == ["📝 пиши в лс, всё расскажу",
                          "📣 канал: Заработок (@money_chan)"]
    # копия по сетке и чужой чат в счёт не идут
    assert {c["kind"]: c["n"] for c in d["counts"]} == {"mute": 2, "ban": 1}

    # Третий не ответил и в базе пуст, в Тихом человека нет и не было — не показываем
    by = {c["chat_id"]: c for c in d["chats"]}
    assert set(by) == {OTHER, CHAT}
    assert d["chats"][0]["chat_id"] == OTHER

    assert by[CHAT]["state"] == "⛔ забанен навсегда"
    assert any(x.startswith("🔨 в базе: бан навсегда") for x in by[CHAT]["lines"])
    assert not any("не видит" in x for x in by[CHAT]["lines"])

    assert by[OTHER]["state"] == "✅ состоит"
    other = "\n".join(by[OTHER]["lines"])
    assert "📅 в чате с" in other and "1 год 1 мес" in other
    assert "✉️ 42 сообщения" in other
    assert "⚠️ Telegram этого наказания не видит" in other   # мут в базе, в TG нет

    assert [e["kind"] if "kind" in e else e["label"] for e in d["events"]] == ["Наблюдение"]
    assert d["events"][0]["body"] == "бан: Катя — профиль (80)"

    text = st.render(d)
    assert "💬 <b>Второй</b>\n├ ✅ состоит" in text
    assert "🔇 Муты: <b>2</b> · ⛔ Баны: <b>1</b>" in text
    assert "<blockquote expandable>" in text and text.endswith("</blockquote>")


async def test_collect_unknown_person(chat):
    bot = StatusBot({CHAT: member("left")})
    bot.members[CHAT].user = None
    d = await st.collect(bot, U, [{"chat_id": CHAT, "title": "Первый"}])
    assert d["name"] == str(U) and d["counts"] == [] and d["chats"] == []
    text = st.render(d)
    assert "Не было." in text and "Ни в одном из ваших чатов" in text
    assert "blockquote" not in text


def test_render_trims_log_not_card():
    d = {"user_id": U, "name": "Катя", "username": None, "premium": False,
         "about": [], "facts": [], "counts": [],
         "chats": [{"title": "Чат", "state": "✅ состоит", "lines": ["x" * 3000]}],
         "events": [{"when": "01.01 10:00", "chat": "Чат", "icon": "•",
                     "label": "Событие", "body": "y" * 90}] * 30}
    text = st.render(d)
    assert len(text) <= st.TEXT_LIMIT
    assert text.count("<blockquote") == text.count("</blockquote>")
