"""Команды модерации через настоящий диспетчер aiogram.

Остальные тесты зовут обработчики напрямую и не видят того, что решает
диспетчер: какой роутер перехватит сообщение первым, пройдут ли фильтры, как
aiogram вызовет наш фильтр-функцию. Здесь апдейт идёт тем же путём, что в
боте: роутеры в порядке app.py, Telegram подменён сессией, которая записывает
запросы.
"""
import datetime as dt

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.types import (Chat, ChatMemberMember, ChatMemberOwner, Message,
                           Update, User)

from gremlin import db
from gremlin.handlers import (admin_menu, cards, events, fun, games, group,
                              user_menu)
from gremlin.services import adm_cache

from conftest import CHAT, OWNER

VICTIM = 5000


class Session(BaseSession):
    """Telegram, который на всё соглашается и запоминает, что у него просили."""

    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def stream_content(self, *a, **kw):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        self.calls.append((name, method))
        user = lambda uid: User(id=uid, is_bot=False, first_name=f"U{uid}")  # noqa: E731
        if name == "GetChatAdministrators":
            return [ChatMemberOwner(user=user(OWNER), is_anonymous=False)]
        if name == "GetChatMember":
            return ChatMemberMember(user=user(method.user_id))
        if name == "GetMe":
            return User(id=42, is_bot=True, first_name="Gremlin", username="gremlin_test_bot")
        if name == "SendMessage":
            return Message(message_id=900, date=dt.datetime.now(dt.timezone.utc),
                           chat=Chat(id=method.chat_id, type="supergroup"),
                           text=method.text)
        return True

    def names(self):
        return [n for n, _ in self.calls]


@pytest.fixture
def dispatcher():
    dp = Dispatcher()
    routers = (admin_menu.router, fun.router, games.router, user_menu.router,
               cards.router, events.router, group.router)
    dp.include_routers(*routers)
    yield dp
    for r in routers:                       # роутеры — синглтоны модулей
        r._parent_router = None
    adm_cache._admins.clear()


def _update(text, author=OWNER, reply_to=VICTIM):
    now = dt.datetime.now(dt.timezone.utc)
    chat = Chat(id=CHAT, type="supergroup", title="Чат")
    reply = Message(message_id=9, date=now, chat=chat, text="реклама",
                    from_user=User(id=reply_to, is_bot=False, first_name="Спамер"))
    msg = Message(message_id=10, date=now, chat=chat, text=text,
                  from_user=User(id=author, is_bot=False, first_name="Админ"),
                  reply_to_message=reply)
    return Update(update_id=1, message=msg)


async def _feed(dp, text, **kw):
    session = Session()
    bot = Bot("42:TEST", session=session)
    await dp.feed_update(bot, _update(text, **kw))
    return session


async def test_mute_command_mutes(chat, dispatcher):
    s = await _feed(dispatcher, "!mute 30m спам")
    assert "RestrictChatMember" in s.names(), s.names()
    restrict = next(m for n, m in s.calls if n == "RestrictChatMember")
    assert restrict.user_id == VICTIM
    assert (await db.active_punishments_count(CHAT)) == 1


@pytest.mark.parametrize("text", ["!Mute 6h", "!MUTE 6H", "!Мут 30м"])
async def test_command_ignores_letter_case(chat, dispatcher, text):
    """Телефон сам ставит заглавную первую букву: «!Mute 6h» бот пропускал молча."""
    s = await _feed(dispatcher, text)
    assert "RestrictChatMember" in s.names(), (text, s.names())


@pytest.mark.parametrize("text, method", [("!Ban", "BanChatMember"),
                                          ("!Kick", "BanChatMember"),
                                          ("!Dm", "DeleteMessage")])
async def test_other_commands_ignore_case(chat, dispatcher, text, method):
    s = await _feed(dispatcher, text)
    assert method in s.names(), (text, s.names())


async def test_russian_alias_still_works(chat, dispatcher):
    s = await _feed(dispatcher, "!мут 30м")
    assert "RestrictChatMember" in s.names(), s.names()


async def test_ban_and_kick_reach_telegram(chat, dispatcher):
    s = await _feed(dispatcher, "!ban спам")
    assert "BanChatMember" in s.names(), s.names()


async def test_switched_off_mute_is_ignored(chat, dispatcher):
    await db.set_setting(CHAT, "cmd_mute_on", 0)
    s = await _feed(dispatcher, "!mute 30m")
    assert "RestrictChatMember" not in s.names()
    assert (await db.active_punishments_count(CHAT)) == 0
