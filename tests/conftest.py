"""Общие заготовки для тестов Гремлина.

Запуск из корня репозитория:

    pip install pytest pytest-asyncio
    pytest tests

Что здесь важно и почему.

Окружение ставится ДО импорта gremlin: config читает переменные при импорте,
и без этого тесты полезли бы в настоящую базу и настоящие папки.

Каждый тест получает свою пустую базу во временной папке. Модуль db держит
одно соединение на процесс, поэтому фикстура закрывает его после теста — иначе
aiosqlite оставляет не-демонский поток, и pytest не может завершиться.

Все пути, которые по умолчанию смотрят в папку проекта (медиа, теневые логи,
бэкапы), уводятся во временную папку. Однажды тест без этого уже насорил в
репозиторий: в корне появилась папка verdict/ с логом выдуманного чата.
"""
import os
import sys
import tempfile
import types

import pytest

_SESSION_TMP = tempfile.mkdtemp(prefix="gremlin-tests-")
os.environ.update(
    BOT_TOKEN="1:test",
    ADMIN_IDS="424211817",
    USERBOT_ON="0",
    BACKUP_ON="0",
    WEB_ON="0",
    DB_PATH=os.path.join(_SESSION_TMP, "boot.sqlite3"),
    LOG_PATH=os.path.join(_SESSION_TMP, "bot.log"),
    MEDIA_DIR=os.path.join(_SESSION_TMP, "media"),
    TRIG_DIR=os.path.join(_SESSION_TMP, "triggers"),
    BACKUP_DIR=os.path.join(_SESSION_TMP, "backups"),
    STATS_DB=os.path.join(_SESSION_TMP, "stats.db"),
    NN_LOG_DIR=os.path.join(_SESSION_TMP, "shadow"),
    NN_LOG=os.path.join(_SESSION_TMP, "nn_shadow.log"),
    UNI_LOG_DIR=os.path.join(_SESSION_TMP, "verdict"),
    TG_SESSION=os.path.join(_SESSION_TMP, "session"),
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gremlin import config, db  # noqa: E402

OWNER = 424211817
CHAT = -1001000000001


@pytest.fixture
async def database(tmp_path, monkeypatch):
    """Чистая база на тест и все пишущие пути во временной папке."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.sqlite3"))
    for name in ("MEDIA_DIR", "TRIG_DIR", "BACKUP_DIR", "NN_LOG_DIR",
                 "UNI_LOG_DIR"):
        monkeypatch.setattr(config, name, str(tmp_path / name.lower()))
    db._db = None
    await db.init()
    try:
        yield db
    finally:
        await db.close()


@pytest.fixture
async def chat(database):
    """Зарегистрированный рабочий чат с настройками по умолчанию."""
    await db.upsert_chat(CHAT, "Чат", None, OWNER, "supergroup")
    await db.get_settings(CHAT)
    return CHAT


def make_user(uid, name=None, username=None, is_bot=False):
    name = name or f"Юзер{uid}"
    return types.SimpleNamespace(id=uid, first_name=name, last_name=None,
                                 full_name=name, username=username,
                                 is_bot=is_bot)


def make_chat(cid=CHAT, title="Чат", kind="supergroup"):
    return types.SimpleNamespace(id=cid, title=title, type=kind, username=None)


class Sent:
    """Сообщение, которое бот отправил: его можно править."""

    def __init__(self, text="", message_id=100):
        self.text = self.html_text = text
        self.message_id = message_id
        self.reply_markup = None

    async def edit_text(self, text, reply_markup=None, **kw):
        self.text = self.html_text = text
        self.reply_markup = reply_markup


class Msg:
    """Входящее сообщение из чата."""

    def __init__(self, text="", author=None, cid=CHAT, reply_from=None):
        self.text = text
        self.caption = None
        self.message_id = 10
        self.chat = make_chat(cid)
        self.from_user = author or make_user(5000)
        self.reply_markup = None
        self.edit_date = None
        self.date = None
        self.via_bot = None
        self.sender_chat = None
        self.forward_origin = None
        self.reply_to_message = (types.SimpleNamespace(from_user=reply_from)
                                 if reply_from else None)
        self.replies = []
        self.deleted = False

    async def reply(self, text, **kw):
        self.replies.append(text)
        return Sent(text)

    async def answer(self, text, **kw):
        self.replies.append(text)
        return Sent(text)

    async def delete(self):
        self.deleted = True


class FakeBot:
    """Бот без сети: запоминает, что его просили сделать."""

    id = 1

    def __init__(self, subscribed=None):
        self.banned, self.unbanned, self.muted = [], [], []
        self.approved, self.declined, self.sent = [], [], []

    async def me(self):
        return types.SimpleNamespace(id=self.id, username="GremlinTestBot")

    async def get_chat(self, cid):
        return types.SimpleNamespace(id=cid, title="Чат", username=None,
                                     type="supergroup", permissions=None,
                                     invite_link=None, linked_chat_id=None)

    async def ban_chat_member(self, cid, uid, until_date=None):
        self.banned.append((cid, uid, until_date))

    async def unban_chat_member(self, cid, uid, only_if_banned=False):
        self.unbanned.append((cid, uid))

    async def restrict_chat_member(self, cid, uid, permissions=None,
                                   until_date=None):
        self.muted.append((cid, uid, until_date))

    async def approve_chat_join_request(self, cid, uid):
        self.approved.append(uid)

    async def decline_chat_join_request(self, cid, uid):
        self.declined.append(uid)

    async def send_message(self, cid, text, **kw):
        self.sent.append((cid, text))
        return Sent(text)

    async def delete_message(self, cid, mid):
        pass


class CB:
    """Нажатие кнопки."""

    def __init__(self, data, uid=OWNER, message=None):
        self.data = data
        self.from_user = make_user(uid, "Админ", "admin")
        self.message = message or Sent()
        self.message.chat = make_chat()
        self.bot = None
        self.alerts = []

    async def answer(self, text=None, show_alert=False):
        self.alerts.append(text or "")


@pytest.fixture
def cards(monkeypatch):
    """Перехват карточек в лог-чат: возвращает список отправленных текстов."""
    from gremlin.services import moderation
    got = []

    async def fake_send_card(bot, chat_id, bit, text, *a, **kw):
        got.append({"bit": bit, "text": text, "markup": kw.get("markup")})
        return []

    monkeypatch.setattr(moderation, "send_card", fake_send_card)
    return got


@pytest.fixture
def members(monkeypatch):
    """Кто состоит в чате: по умолчанию все, список исключений правится в тесте."""
    from gremlin.services import adm_cache
    state = {"outside": set(), "admins": set()}

    async def is_member(bot, cid, uid):
        return uid not in state["outside"]

    async def chat_admin_ids(bot, cid):
        return set(state["admins"])

    monkeypatch.setattr(adm_cache, "is_member", is_member)
    monkeypatch.setattr(adm_cache, "chat_admin_ids", chat_admin_ids)
    monkeypatch.setattr(adm_cache, "_members", {})
    return state
