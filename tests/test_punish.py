"""Наказания: мут не-участнику, сроки, сетка, список активных."""
import time

import pytest

from gremlin import config, db, utils
from gremlin.handlers import user_menu as um
from gremlin.services import moderation, net

from conftest import OWNER, FakeBot, make_user

PEER = -1001000000002
WEEK = 7 * 24 * 60


async def test_mute_for_non_member_becomes_ban_with_same_term(chat, members):
    user = make_user(4242, "Олег", "chomkaaaa")
    members["outside"].add(user.id)
    bot = FakeBot()
    pid, err = await moderation.punish_ex(bot, chat, user, "mute", WEEK,
                                          "это не чат леши", OWNER)
    assert err is None
    row = await db.get_punishment(pid)
    assert row["kind"] == "ban"
    left = row["until_ts"] - int(time.time())
    assert WEEK * 60 - 60 < left <= WEEK * 60
    assert bot.banned[-1][2] is not None       # телеграму отдан срок


async def test_plain_ban_is_forever(chat, members):
    pid, _ = await moderation.punish_ex(FakeBot(), chat, make_user(5555), "ban",
                                        0, "спам", OWNER)
    assert (await db.get_punishment(pid))["until_ts"] is None


async def test_net_spreads_the_term_not_the_swapped_ban(chat, members,
                                                        monkeypatch):
    """Семидневный мут не должен расходиться по сетке вечным баном."""
    monkeypatch.setattr(config, "NET_DELAY", 0)
    await db.upsert_chat(PEER, "Соседний", None, OWNER, "supergroup")
    await db.get_settings(PEER)
    nid = await db.net_create(OWNER, "Сетка")
    await db.net_assign(chat, nid)
    await db.net_assign(PEER, nid)
    await db.net_set(nid, "sync_mask", config.NET_BAN | config.NET_MUTE)

    user = make_user(4242, "Олег", "chomkaaaa")
    members["outside"].add(user.id)
    bot = FakeBot()
    done, skipped, failed, swapped = await net.spread(
        bot, chat, user, "mute", WEEK, "это не чат леши", OWNER)
    assert (done, failed, swapped) == (1, 0, 1)
    peer = (await db.active_punishments(PEER))[0]
    assert peer["kind"] == "ban"
    assert peer["until_ts"] is not None
    # уже забанен там — повторный мут по сетке не заводит вторую запись
    again = await net.spread(bot, chat, user, "mute", WEEK, "ещё раз", OWNER)
    assert again[:2] == (0, 1)
    assert len(await db.active_punishments(PEER)) == 1


async def test_net_leaves_old_ban_alone(chat, members, monkeypatch):
    """Забаненного до бота сетка не перебанивает своим — и прощение в
    соседнем чате не выпускает его вместе с сетевым баном."""
    import types
    monkeypatch.setattr(config, "NET_DELAY", 0)
    await db.upsert_chat(PEER, "Соседний", None, OWNER, "supergroup")
    await db.get_settings(PEER)
    nid = await db.net_create(OWNER, "Сетка")
    await db.net_assign(chat, nid)
    await db.net_assign(PEER, nid)
    await db.net_set(nid, "sync_mask", config.NET_BAN | config.NET_LIFT)

    class OldBan(FakeBot):
        async def get_chat_member(self, cid, uid):
            return types.SimpleNamespace(status="kicked" if cid == PEER else "member")

    bot = OldBan()
    user = make_user(4343, "Спамер", "old_spammer")
    done, skipped, _, _ = await net.spread(bot, chat, user, "ban", 0, "стоп-слово", None)
    assert (done, skipped) == (0, 1)
    assert bot.banned == []
    assert await db.active_punishments(PEER) == []
    await net.lift(bot, chat, user.id)
    assert bot.unbanned == []


async def _net_with_peer(chat):
    await db.upsert_chat(PEER, "Соседний", None, OWNER, "supergroup")
    await db.get_settings(PEER)
    nid = await db.net_create(OWNER, "Сетка")
    await db.net_assign(chat, nid)
    await db.net_assign(PEER, nid)
    await db.net_set(nid, "sync_mask", config.NET_BAN | config.NET_MUTE)


async def test_autoban_spares_member_of_other_net_chat(chat, members, cards,
                                                       monkeypatch):
    """Комментатора, который сидит в соседнем чате сетки, автомат не банит:
    сообщение удалено, карточка с «Забанить» — решает админ."""
    from gremlin.services import adm_cache
    from conftest import Msg
    await _net_with_peer(chat)

    async def is_member(bot, cid, uid):
        return cid == PEER                  # здесь не участник, в соседнем свой

    monkeypatch.setattr(adm_cache, "is_member", is_member)
    bot = FakeBot()
    await moderation.violation(bot, Msg("казино тут", author=make_user(4545)),
                               config.BIT_WORDS, "стоп-слово", "ban", 0, "казино")
    assert bot.banned == []
    assert await db.active_punishments(chat) == []
    assert "Не забанен: состоит в «Соседний»" in cards[-1]["text"]


async def test_auto_spread_skips_where_member_manual_does_not(chat, members,
                                                              monkeypatch):
    import types
    monkeypatch.setattr(config, "NET_DELAY", 0)
    await _net_with_peer(chat)

    class Member(FakeBot):
        async def get_chat_member(self, cid, uid):
            return types.SimpleNamespace(status="member")

    bot = Member()
    user = make_user(4646, "Свой", "svoy")
    auto = await net.spread(bot, chat, user, "ban", 0, "стоп-слово", None)
    assert auto[:2] == (0, 1) and bot.banned == []
    manual = await net.spread(bot, chat, user, "ban", 0, "вручную", OWNER)
    assert manual[0] == 1 and bot.banned[-1][:2] == (PEER, user.id)


async def test_creator_ban_passes_whitelist(chat, monkeypatch):
    """Ручной бан создателя чата доходит и до вайтлиста соседних чатов;
    бан обычного админа — нет. Создатель берётся из списка админов."""
    import types
    from gremlin.services import adm_cache
    monkeypatch.setattr(config, "NET_DELAY", 0)
    monkeypatch.setattr(adm_cache, "_admins", {})
    monkeypatch.setattr(adm_cache, "_members", {})
    await _net_with_peer(chat)
    user = make_user(4747, "В вайтлисте", "listed")
    await db.wl_set_scopes(PEER, user.id, None, None, {"all"})
    creator, admin = 111, 222

    class Owner(FakeBot):
        async def get_chat_administrators(self, cid):
            return [types.SimpleNamespace(status="creator", user=make_user(creator)),
                    types.SimpleNamespace(status="administrator", user=make_user(admin))]

        async def get_chat_member(self, cid, uid):
            return types.SimpleNamespace(status="member")

    bot = Owner()
    by_admin = await net.spread(bot, chat, user, "ban", 0, "вручную", admin)
    assert by_admin[:2] == (0, 1) and bot.banned == []
    by_creator = await net.spread(bot, chat, user, "ban", 0, "вручную", creator)
    assert by_creator[0] == 1 and bot.banned[-1][:2] == (PEER, user.id)
    assert await db.chat_creator(chat) == creator


async def test_card_lift_answers(chat, members, monkeypatch):
    """«Снять» на карточке падало на последней строке: нажатие без ответа."""
    from conftest import CB, Sent
    from gremlin.handlers import cards

    async def no_net(*a, **kw):
        pass

    monkeypatch.setattr(net, "lift_and_note", no_net)
    pid, _ = await moderation.punish_ex(FakeBot(), chat, make_user(4444), "ban",
                                        0, "спам", OWNER)
    cb = CB(f"k:lift:{pid}", message=Sent("⛔ Бан"))
    cb.bot = FakeBot()
    await cards.card_lift(cb, cb.bot)
    assert cb.alerts == ["Разбанен"]


def test_swapped_ban_is_shown_as_mute():
    """Мут не-участнику применён баном, но везде показывается мутом."""
    reason = "не будешь · мут не-участнику невозможен, заменён баном"
    assert utils.shown_kind("ban", reason) == "mute"
    assert utils.shown_kind("ban", "спам") == "ban"
    card = moderation.card_text("ban", "Чат", 1, "Вася", reason, "админ")
    assert "Мут" in card and "Бан" not in card
    assert "невозможен" not in card and utils.SWAP_NOTE in card
    assert "навсегда" in card


@pytest.mark.parametrize("raw,clean,swapped", [
    ("сетка · каментики: это не чат леши", "это не чат леши", False),
    ("сетка · каментики: это не чат леши · мут не-участнику невозможен, "
     "заменён баном на тот же срок", "это не чат леши", True),
    ("спам · мут не-участнику невозможен, заменён баном", "спам", True),
    ("стоп-слово: 🔞", "стоп-слово: 🔞", False),
    ("стоп-слово: сетка казино", "стоп-слово: сетка казино", False),
    (None, "—", False),
])
def test_short_reason(raw, clean, swapped):
    assert utils.short_reason(raw) == (clean, swapped)


def test_name_link():
    assert utils.name_link(1, "Олег", "chomkaaaa") \
        == '<a href="https://t.me/chomkaaaa">Олег</a>'
    assert utils.name_link(777, "Наталия") \
        == '<a href="tg://user?id=777">Наталия</a>'


async def test_active_list_shows_what_matters(chat):
    soon = int(time.time()) + 3600
    await db.add_punishment(
        chat, 21, "chomkaaaa", "Олег", "ban",
        "сетка · каментики: это не чат леши · мут не-участнику невозможен, "
        "заменён баном на тот же срок", soon, OWNER)
    text, _kb = await um.view_active(chat)
    assert '<a href="https://t.me/chomkaaaa">Олег</a>' in text
    assert "это не чат леши" in text
    assert "— мут до " in text and "мут→бан" not in text   # выдан мут
    assert "выдан " in text and "до " in text
    assert "сетка ·" not in text and "не-участнику" not in text


async def test_game_mute_adds_to_running_one(chat, members):
    """Рулетка дала 6 ч, битва — 1 ч: итог 7 ч, а не час."""
    user = make_user(4343)
    bot = FakeBot()
    first, _, _ = await moderation.game_punish(bot, chat, user, "mute", 360,
                                               "рулетка", None)
    pid, total, stricter = await moderation.game_punish(bot, chat, user, "mute",
                                                        60, "битва", None)
    assert stricter is None and 419 <= total <= 420
    left = (await db.get_punishment(pid))["until_ts"] - int(time.time())
    assert 419 * 60 < left <= 420 * 60
    assert not (await db.get_punishment(first))["active"]   # одна запись


async def test_game_mute_never_shortens_forever(chat, members):
    user = make_user(4444)
    bot = FakeBot()
    await moderation.punish_ex(bot, chat, user, "mute", 0, "навсегда", OWNER)
    pid, total, stricter = await moderation.game_punish(bot, chat, user, "mute",
                                                        60, "битва", None)
    assert (pid, total, stricter) == (None, None, "mute")
