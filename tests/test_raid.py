"""Защита от набегов: порог, режим, карточка и кнопки."""
import asyncio
import time
import types

import pytest

from gremlin import config, db
from gremlin.handlers import cards as cards_h
from gremlin.services import moderation, raid

from conftest import CB, CHAT, OWNER, FakeBot, make_chat, make_user

GUEST = 9500


@pytest.fixture
async def raiders(chat, members, cards, monkeypatch):
    """Набег настроен на три входа за минуту, карточки перехвачены."""
    monkeypatch.setattr(raid, "_state", {})
    for key, val in (("raid_on", 1), ("raid_joins", 3), ("raid_window", 60),
                     ("raid_hold", 10), ("raid_action", "mute")):
        await db.set_setting(chat, key, val)

    def spawn(coro):
        coro.close()            # конец режима в тестах не ждём

    monkeypatch.setattr(raid.runtime, "spawn", spawn)
    return cards


async def flood(chat_id, count, start=GUEST, s=None):
    """Впустить подряд count человек. Вернуть, что бот решил по каждому."""
    s = s or await db.get_settings(chat_id)
    out = []
    for i in range(count):
        out.append(await raid.note_join(FakeBot(), make_chat(chat_id),
                                        make_user(start + i, f"Гость{i}"), s))
    return out


async def test_quiet_joins_are_not_a_raid(raiders, chat):
    assert await flood(chat, 2) == [None, None]
    assert raid.active(chat) is False
    assert raiders == []


async def test_threshold_starts_mode_and_cards(raiders, chat):
    assert await flood(chat, 3) == [None, None, "mute"]
    assert raid.active(chat) is True
    assert len(raiders) == 1
    card = raiders[0]
    assert card["bit"] == config.BIT_RAID
    assert "Вошло: <b>3</b>" in card["text"]
    # в цитате — все вошедшие, по ним и проверяют, кто это был
    assert "Гость0" in card["text"] and "Гость2" in card["text"]
    buttons = [b.callback_data for row in card["markup"].inline_keyboard for b in row]
    assert buttons == [f"k:raidoff:{chat}", f"k:raidban:{chat}"]


async def test_old_joins_fall_out_of_window(raiders, chat):
    await flood(chat, 2)
    raid._state[chat]["joins"] = [(time.time() - 600, 1, "Старый", None)]
    assert await flood(chat, 1, start=9600) == [None]
    assert raid.active(chat) is False


async def test_everyone_during_raid_gets_the_action(raiders, chat):
    await flood(chat, 3)
    assert await flood(chat, 2, start=9700) == ["mute", "mute"]
    assert len(raid.joined(chat)) == 5
    assert len(raiders) == 1          # карточка одна на набег


async def test_action_applies(raiders, chat, monkeypatch):
    done = []

    async def apply_punishment(bot, chat_id, user, kind, minutes, reason, by_id,
                               wipe=True):
        done.append((user.id, kind, minutes, reason))
        return 5

    async def kick(bot, chat_id, user, reason, by_id):
        done.append((user.id, "kick", 0, reason))
        return 6, None

    monkeypatch.setattr(moderation, "apply_punishment", apply_punishment)
    monkeypatch.setattr(moderation, "kick", kick)
    s = await db.get_settings(chat)
    user = make_user(GUEST)
    assert await raid.apply(FakeBot(), make_chat(chat), user, s, "mute")
    assert await raid.apply(FakeBot(), make_chat(chat), user, s, "kick")
    assert done == [(GUEST, "mute", 10, "набег на чат"),
                    (GUEST, "kick", 0, "набег на чат")]


async def test_disabled_feature_ignores_flood(raiders, chat):
    await db.set_setting(chat, "raid_on", 0)
    assert await flood(chat, 5) == [None] * 5
    assert raiders == []


# ---------- кнопки карточки ----------

def press(data, uid=OWNER):
    from conftest import Sent
    msg = Sent("🛡 <b>Набег</b>")
    msg.chat = types.SimpleNamespace(id=-100999)
    cb = CB(data, uid=uid, message=msg)
    cb.bot = FakeBot()
    return cb, msg


async def test_stop_button_ends_mode(raiders, chat):
    await flood(chat, 3)
    cb, msg = press(f"k:raidoff:{chat}")
    await cards_h.card_raid_off(cb)
    assert raid.active(chat) is False
    assert "Режим снят" in msg.text


async def test_ban_all_button(raiders, chat, monkeypatch):
    banned = []

    async def apply_punishment(bot, chat_id, user, kind, minutes, reason, by_id,
                               wipe=True):
        banned.append(user.id)
        return 7

    real_sleep = asyncio.sleep          # до подмены: иначе вызовет сам себя

    async def fast_sleep(delay, *a, **kw):
        await real_sleep(0)

    monkeypatch.setattr(moderation, "apply_punishment", apply_punishment)
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    await flood(chat, 3)
    cb, msg = press(f"k:raidban:{chat}")
    await cards_h.card_raid_ban(cb, cb.bot)
    assert banned == [GUEST, GUEST + 1, GUEST + 2]
    assert "Забанено: 3" in msg.text


async def test_stranger_cannot_press(raiders, chat):
    await flood(chat, 3)
    cb, _ = press(f"k:raidoff:{chat}", uid=GUEST)
    await cards_h.card_raid_off(cb)
    assert raid.active(chat) is True
