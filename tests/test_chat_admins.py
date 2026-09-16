"""Админы чата в боте: кому что открыто.

Владелец пускает своих админов в меню и панель по одному чату. Уровня два:
«наказания» и «настройки». Владельческое (лог-чат, сетки, перенос настроек,
удаление бота и сам список) не отдаётся никому — иначе доступ раздавался бы
по кругу, и чужой чат оказался бы у человека, которому его никто не доверял.
"""
import types

import pytest

from gremlin import db
from gremlin.handlers import user_menu as um
from gremlin.web import auth

from conftest import CB, CHAT, OWNER, FakeBot, Msg, make_user

PUNISHER = 7001          # уровень «наказания»
SETTER = 7002            # уровень «настройки»
STRANGER = 7003          # никто


def buttons(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


@pytest.fixture
async def with_admins(chat):
    await db.chat_admin_add(chat, PUNISHER, "punish", "pun", "Пун", OWNER)
    await db.chat_admin_add(chat, SETTER, "settings", "set", "Сет", OWNER)
    return chat


# ---------- права ----------

async def test_levels(with_admins):
    assert await db.chat_access(OWNER, CHAT) == "owner"
    assert await db.chat_access(PUNISHER, CHAT) == "punish"
    assert await db.chat_access(SETTER, CHAT) == "settings"
    assert await db.chat_access(STRANGER, CHAT) is None

    assert await db.may(PUNISHER, CHAT, "punish") is True
    assert await db.may(PUNISHER, CHAT, "settings") is False
    assert await db.may(SETTER, CHAT, "settings") is True
    assert await db.may(SETTER, CHAT, "owner") is False
    assert await db.may(OWNER, CHAT, "owner") is True
    assert await db.may(STRANGER, CHAT, "punish") is False


async def test_level_change_and_removal(with_admins):
    await db.chat_admin_add(CHAT, PUNISHER, "settings", None, None, OWNER)
    assert await db.chat_admin_level(CHAT, PUNISHER) == "settings"
    # имя при смене уровня не затираем: его брали из Telegram при добавлении
    row = (await db.chat_admin_list(CHAT))[0]
    assert row["name"] == "Пун"
    await db.chat_admin_remove(CHAT, PUNISHER)
    assert await db.chat_admin_level(CHAT, PUNISHER) is None
    assert await db.may(PUNISHER, CHAT, "punish") is False


async def test_admin_reaches_the_bot_and_sees_only_his_chat(with_admins):
    other = CHAT - 77
    await db.upsert_chat(other, "Чужой", None, OWNER + 1, "supergroup")
    await db.get_settings(other)
    # в общем списке доступа его нет — иначе он увидел бы все чужие чаты
    assert await db.access_allowed(PUNISHER, None) is True
    assert await db.access_allowed(STRANGER, None) is False
    mine = [c["chat_id"] for c in await db.chats_for(PUNISHER)]
    assert mine == [CHAT]


# ---------- меню ----------

async def test_guard_checks_the_level(with_admins):
    cb = CB(f"u:s:{CHAT}:words", uid=PUNISHER)
    assert await um._guard(cb, CHAT, "punish") is True
    assert await um._guard(cb, CHAT) is False          # настройки — не ему
    assert "только наказания" in cb.alerts[-1]
    assert await um._guard(cb, CHAT, "owner") is False

    cb = CB(f"u:s:{CHAT}:words", uid=SETTER)
    assert await um._guard(cb, CHAT) is True
    assert await um._guard(cb, CHAT, "owner") is False
    assert "владелец" in cb.alerts[-1]

    cb = CB(f"u:c:{CHAT}", uid=STRANGER)
    assert await um._guard(cb, CHAT, "punish") is False
    assert "не ваш чат" in cb.alerts[-1]


async def test_chat_card_by_level(with_admins):
    _text, kb = await um.view_chat(CHAT, OWNER)
    owner_btns = buttons(kb)
    assert f"u:ca:{CHAT}" in owner_btns                # список админов
    assert f"u:logsel:{CHAT}" in owner_btns
    assert f"u:s:{CHAT}:words" in owner_btns

    _text, kb = await um.view_chat(CHAT, SETTER)
    setter_btns = buttons(kb)
    assert f"u:s:{CHAT}:words" in setter_btns          # разделы открыты
    assert f"u:p:{CHAT}:0" in setter_btns
    assert f"u:ca:{CHAT}" not in setter_btns           # владельческое — нет
    assert f"u:logsel:{CHAT}" not in setter_btns
    assert f"a:leave:{CHAT}" not in setter_btns

    _text, kb = await um.view_chat(CHAT, PUNISHER)
    pun_btns = buttons(kb)
    assert f"u:p:{CHAT}:0" in pun_btns                 # наказания и статистика
    assert f"u:st:{CHAT}" in pun_btns
    assert not [d for d in pun_btns if d.startswith(f"u:s:{CHAT}")]
    assert f"a:clog:{CHAT}" not in pun_btns


async def test_punishments_page_hides_settings(with_admins):
    _text, kb = await um.view_punishments(CHAT, 0, full=False)
    data = buttons(kb)
    assert f"u:ps:{CHAT}" in data and f"u:mub:{CHAT}" in data
    assert f"u:s:{CHAT}:punish_cfg" not in data
    _text, kb = await um.view_punishments(CHAT, 0)
    assert f"u:s:{CHAT}:punish_cfg" in buttons(kb)


# ---------- добавление ----------

class FakeState:
    def __init__(self, **data):
        self.data = data
        self.state = None

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)

    async def set_state(self, st):
        self.state = st

    async def clear(self):
        self.state = None


class MenuBot(FakeBot):
    """Бот, умеющий править меню и отдавать участника чата."""

    def __init__(self, member=None):
        super().__init__()
        self.edits = []
        self._member = member

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
        self.edits.append(text)

    async def get_chat_member(self, cid, uid):
        return types.SimpleNamespace(
            user=self._member or make_user(uid, "Новый", "novy"))


async def _feed(chat, bot, text, author_id=OWNER):
    msg = Msg(text, author=make_user(author_id), cid=author_id)
    state = FakeState(cid=chat, msg_id=555, back=f"u:ca:{chat}")
    await um.chat_admin_input(msg, state, bot)
    return bot.edits[-1] if bot.edits else ""


async def test_only_chat_admins_can_be_added(chat, members):
    bot = MenuBot()
    members["admins"] = set()
    said = await _feed(chat, bot, "7005")
    assert "не админ чата" in said
    assert await db.chat_admin_level(chat, 7005) is None

    members["admins"] = {7005}
    said = await _feed(chat, bot, "7005")
    assert await db.chat_admin_level(chat, 7005) == "punish"
    assert "Админы в боте" in said


async def test_owner_cannot_add_himself(chat, members):
    bot = MenuBot()
    members["admins"] = {OWNER}
    said = await _feed(chat, bot, str(OWNER))
    assert "у вас и так все права" in said
    assert await db.chat_admin_level(chat, OWNER) is None


# ---------- панель ----------

async def test_web_owns_follows_the_same_levels(with_admins):
    assert await auth.owns(PUNISHER, CHAT, "punish") is True
    assert await auth.owns(PUNISHER, CHAT) is False
    assert await auth.owns(SETTER, CHAT) is True
    assert await auth.owns(SETTER, CHAT, "owner") is False
    assert await auth.owns(STRANGER, CHAT, "punish") is False
