"""Вход по подписке: режимы для подписанных и неподписанных, защита от долбёжки."""
import types

import pytest

from gremlin import config, db
from gremlin.handlers import events
from gremlin.services import profile
from gremlin.services import subscribe as sub

from conftest import CB, CHAT, FakeBot, Sent, make_chat, make_user

CHAN = -1001000000099


def request(uid=777, user_chat_id=None):
    return types.SimpleNamespace(chat=make_chat(), from_user=make_user(uid, "Гость"),
                                 user_chat_id=user_chat_id)


@pytest.fixture
async def gate(chat, cards, monkeypatch):
    """Чат с включённой проверкой и управляемым ответом «подписан ли»."""
    state = {"subscribed": True}

    async def subscribed(bot, channel_id, user_id):
        return state["subscribed"]

    async def no_profile(bot, uid):
        return None

    monkeypatch.setattr(sub, "subscribed", subscribed)
    monkeypatch.setattr(profile, "fetch", no_profile)
    monkeypatch.setattr(sub, "_tries", {})
    monkeypatch.setattr(sub, "_pending", {})
    monkeypatch.setattr(sub, "_dm_sent", {})
    await db.set_setting(chat, "sub_on", 1)
    await db.set_setting(chat, "sub_chat_id", CHAN)
    return state


async def test_default_lets_subscribed_in(gate, cards):
    bot = FakeBot()
    await events._sub_join_request(request(11), bot)
    assert bot.approved == [11]
    assert "Впущен по подписке" in cards[-1]["text"]


async def test_skip_leaves_request_with_decision_card(gate, cards):
    await db.set_setting(CHAT, "sub_pass", "skip")
    bot = FakeBot()
    await events._sub_join_request(request(12), bot)
    assert (bot.approved, bot.declined) == ([], [])
    labels = [b.text for r in cards[-1]["markup"].inline_keyboard for b in r]
    assert labels == ["✅ Принять", "🚫 Отказать", "⛔ Забанить"]


async def test_decline_mode_rejects_even_subscribed(gate, cards):
    await db.set_setting(CHAT, "sub_pass", "decline")
    bot = FakeBot()
    await events._sub_join_request(request(16), bot)
    assert bot.declined == [16]
    assert "вход сейчас закрыт" in cards[-1]["text"]


async def test_unsubscribed_declined(gate, cards):
    gate["subscribed"] = False
    bot = FakeBot()
    await events._sub_join_request(request(13), bot)
    assert bot.declined == [13]
    assert "Заявка отклонена" in cards[-1]["text"]


async def test_repeated_requests_card_first_and_fifth(gate, cards):
    gate["subscribed"] = False
    bot = FakeBot()
    for _ in range(6):
        await events._sub_join_request(request(555), bot)
    assert len(bot.declined) == 6
    assert len(cards) == 2
    assert f"Заявка подряд {config.SUB_REPEAT_ALERT}-я" in cards[1]["text"]


async def test_dm_not_lost_after_switching_from_decline_to_hold(gate):
    """Счётчик карточек не должен съедать письмо: после долбёжки в режиме
    отказа первое же письмо в режиме ожидания обязано уйти."""
    gate["subscribed"] = False
    bot = FakeBot()
    for _ in range(3):
        await events._sub_join_request(request(900, user_chat_id=999), bot)
    await db.set_setting(CHAT, "sub_action", "hold")
    await db.set_setting(CHAT, "sub_dm", 1)
    await db.ans_add("sub", CHAT, "Подпишись на канал, {name}")
    bot2 = FakeBot()
    await events._sub_join_request(request(900, user_chat_id=999), bot2)
    assert [cid for cid, _t in bot2.sent] == [999]


async def test_button_mode_only_button_lets_in(gate):
    await db.set_setting(CHAT, "sub_pass", "button")
    bot = FakeBot()
    await events._sub_join_request(request(15), bot)
    assert bot.approved == []
    sub.remember(CHAT, 15)
    cb = CB(f"sub:chk:{CHAT}", uid=15)
    await events.sub_recheck(cb, bot)
    assert bot.approved == [15]


async def test_skip_mode_button_does_not_let_in(gate):
    await db.set_setting(CHAT, "sub_pass", "skip")
    sub.remember(CHAT, 14)
    bot = FakeBot()
    cb = CB(f"sub:chk:{CHAT}", uid=14)
    await events.sub_recheck(cb, bot)
    assert bot.approved == []
    assert "админы" in cb.alerts[0]


async def test_decision_card_buttons(gate, members):
    card = Sent("🙋 Заявка")
    bot = FakeBot()
    await events.sub_take(CB(f"sub:ok:{CHAT}:21", message=card), bot)
    assert bot.approved == [21] and "Впущен" in card.text

    card2 = Sent("🙋 Заявка")
    await events.sub_drop(CB(f"sub:no:{CHAT}:22", message=card2), bot)
    assert bot.declined == [22] and "Отказано" in card2.text

    members["outside"].add(23)
    card3 = Sent("🙋 Заявка")
    await events.sub_ban(CB(f"sub:ban:{CHAT}:23", message=card3), bot)
    assert 23 in [u for _c, u, _t in bot.banned]
    assert [r["reason"] for r in await db.active_punishments(CHAT)] \
        == ["заявка на вступление"]
