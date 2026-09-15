"""Список чатов: служебные каналы прячутся, само обсуждение — никогда."""
import types

from gremlin import db
from gremlin.services import adm_cache

from conftest import OWNER

GROUP, CHAN = -1001389201023, -1001329961905    # обсуждение <-> канал
KOM, GGWP = -1003740255930, -1003179413117
LOGC = -1004344956812

KINDS = {GROUP: "supergroup", CHAN: "channel", KOM: "supergroup"}
LINKS = {GROUP: CHAN, CHAN: GROUP, KOM: GGWP}


class LinkBot:
    async def get_chat(self, cid):
        return types.SimpleNamespace(id=cid, title=str(cid), username=None,
                                     type=KINDS.get(cid, "supergroup"),
                                     linked_chat_id=LINKS.get(cid))


async def seed_live_layout():
    for cid, title in ((GROUP, "овощехранилище"), (CHAN, "Легушенька"),
                       (KOM, "каментики"), (LOGC, "тетрадь")):
        await db.upsert_chat(cid, title, None, OWNER)
        await db.get_settings(cid)
    await db.set_linked(GROUP, CHAN, "Легушенька")
    await db.set_linked(CHAN, GROUP, "овощехранилище")
    await db.set_linked(KOM, GGWP, "ggwp")
    await db.set_setting(GROUP, "log_chat_id", LOGC)
    await db.set_setting(KOM, "log_chat_id", LOGC)


async def test_mutual_link_does_not_hide_discussion(database):
    """Привязка в Telegram взаимная: раньше вместе с каналом пропадало
    из панели и само обсуждение."""
    await seed_live_layout()
    got = {r["chat_id"] for r in await db.moderated_chats()}
    assert GROUP in got
    assert LOGC not in got


async def test_reconcile_marks_kinds_and_hides_channel(database):
    await seed_live_layout()
    bot = LinkBot()
    for cid in (GROUP, CHAN, KOM):
        adm_cache._linked.pop(cid, None)
        await adm_cache.refresh_linked(bot, cid)
    assert (await db.get_chat(CHAN))["kind"] == "channel"
    assert (await db.get_chat(GROUP))["kind"] == "supergroup"
    got = sorted(r["chat_id"] for r in await db.moderated_chats())
    assert got == sorted([KOM, GROUP])
    assert sorted(r["chat_id"] for r in await db.chats_for(OWNER)) \
        == sorted([KOM, GROUP])


async def test_subscription_channel_hidden(database):
    await seed_live_layout()
    other = -100777
    await db.upsert_chat(other, "Отдельный", None, OWNER, "channel")
    await db.get_settings(other)
    assert other in {r["chat_id"] for r in await db.moderated_chats()}
    await db.set_setting(KOM, "sub_chat_id", other)
    assert other not in {r["chat_id"] for r in await db.moderated_chats()}


async def test_serves_chat_recognises_service_channels(database):
    await seed_live_layout()
    await db.set_setting(KOM, "sub_chat_id", -100555)
    assert await db.serves_chat(-100555) == (True, OWNER)
    assert await db.serves_chat(LOGC) == (True, OWNER)
    assert (await db.serves_chat(CHAN))[0] is True
    assert await db.serves_chat(-100999) == (False, None)
