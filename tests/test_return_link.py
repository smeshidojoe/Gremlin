"""Ссылка на возврат после разбана и сбои проверки членства."""
import logging
import types

import pytest

from gremlin import db
from gremlin.services import adm_cache, moderation

from conftest import CHAT, OWNER, FakeBot

VICTIM = 8800
CHANNEL = -1002000000001


class LinkBot(FakeBot):
    """Бот, у которого чат может быть обсуждением канала."""

    def __init__(self, linked=None, join_to_send=None):
        super().__init__()
        self.linked, self.join_to_send = linked, join_to_send
        self.links = []

    async def get_chat(self, cid):
        if cid == self.linked:
            return types.SimpleNamespace(id=cid, title="Канал", username=None,
                                         type="channel", linked_chat_id=CHAT)
        return types.SimpleNamespace(id=cid, title="Чат", username=None,
                                     type="supergroup", linked_chat_id=self.linked,
                                     join_to_send_messages=self.join_to_send)

    async def create_chat_invite_link(self, cid, **kw):
        self.links.append(cid)
        return types.SimpleNamespace(invite_link=f"https://t.me/+test{len(self.links)}")


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(adm_cache, "_linked", {})
    monkeypatch.setattr(adm_cache, "_comment_joins", {})
    monkeypatch.setattr(adm_cache, "_member_fail_logged", {})


async def banned(was_member=True):
    return await db.add_punishment(CHAT, VICTIM, None, "Катя", "ban",
                                   "наблюдение: профиль (80)", None, OWNER,
                                   was_member=was_member)


async def test_no_link_when_comment_makes_member(chat):
    """Обсуждение с «вступить, чтобы писать»: участник через комментарий."""
    bot = LinkBot(linked=CHANNEL, join_to_send=True)
    ok, _, link = await moderation.lift_punishment(bot, await banned())
    assert ok and link is None
    assert bot.unbanned == [(CHAT, VICTIM)]
    assert bot.links == []
    assert await db.kv_get(moderation.unban_link_key(CHAT, VICTIM)) is None


@pytest.mark.parametrize("linked,join_to_send", [
    (None, None),            # обычная группа
    (CHANNEL, False),        # обсуждение, писать можно без вступления
    (None, True),            # флаг без канала ничего не значит
])
async def test_link_for_real_members(chat, linked, join_to_send):
    bot = LinkBot(linked=linked, join_to_send=join_to_send)
    ok, _, link = await moderation.lift_punishment(bot, await banned())
    assert ok and link and link.startswith("https://t.me/+")
    assert bot.links == [CHAT]


async def test_no_link_for_non_member(chat):
    bot = LinkBot()
    _, _, link = await moderation.lift_punishment(bot, await banned(False))
    assert link is None and bot.links == []


async def test_member_check_failure_is_logged_once(monkeypatch, caplog):
    monkeypatch.setattr(adm_cache, "_members", {})

    class Broken(FakeBot):
        calls = 0

        async def get_chat_member(self, cid, uid):
            Broken.calls += 1
            raise RuntimeError("Bad Request: boom")

    bot = Broken()
    with caplog.at_level(logging.WARNING, logger="gremlin.adm_cache"):
        assert await adm_cache.is_member(bot, CHAT, 1) is True
        assert await adm_cache.is_member(bot, CHAT, 2) is True
    lines = [r for r in caplog.records if "getChatMember" in r.getMessage()]
    assert len(lines) == 1 and "boom" in lines[0].getMessage()
    assert Broken.calls == 2                 # сбой не кэшируется
    assert adm_cache.member_cached(CHAT, 1) is None
