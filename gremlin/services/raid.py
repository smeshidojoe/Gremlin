"""Защита от набегов: когда в чат за минуту вливается толпа.

Капча и наблюдение смотрят на каждого по отдельности, а набег — это про
скорость: два десятка входов подряд. Разбирать их по одному поздно, поэтому на
время закрываем дверь: вошедшие получают мут (или кик, или капчу), а админы
разбираются по карточке в лог-чате, где перечислены все вошедшие.

Состояние живёт в памяти: набег — история на минуты, переживать перезапуск ей
незачем. Перезапуск просто снимает режим.
"""
import asyncio
import logging
import time
import types

from aiogram import Bot
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import config, db, runtime, utils

logger = logging.getLogger("gremlin.raid")

# chat_id -> {joins: [(когда, id, имя, ник)], until: до какого времени режим,
#             seen: кто вошёл за набег, cards: куда ушла карточка}
_state: dict[int, dict] = {}


def _rec(chat_id: int) -> dict:
    return _state.setdefault(chat_id, {"joins": [], "until": 0.0, "seen": [],
                                       "cards": []})


def active(chat_id: int) -> bool:
    """Идёт ли сейчас набег."""
    rec = _state.get(chat_id)
    return bool(rec and rec["until"] > time.time())


def joined(chat_id: int) -> list[tuple]:
    """Кто вошёл за время набега: (id, имя, ник)."""
    rec = _state.get(chat_id)
    return list(rec["seen"]) if rec else []


def stop(chat_id: int) -> int:
    """Снять режим досрочно. Вернуть, сколько человек успело войти."""
    rec = _state.get(chat_id)
    if rec is None:
        return 0
    rec["until"] = 0.0
    return len(rec["seen"])


async def note_join(bot: Bot, chat, user, s) -> str | None:
    """Учесть вход. Вернуть, что делать с вошедшим, или None.

    Пока набега нет, копим входы за окно. Как только их стало больше порога —
    включаем режим и с этого момента отвечаем на каждый вход выбранным
    действием, пока режим не кончится.
    """
    if not s.raid_on or user.is_bot:
        return None
    now = time.time()
    rec = _rec(chat.id)
    who = (user.id, user.full_name, user.username)
    if rec["until"] > now:
        rec["seen"].append(who)
        return s.raid_action

    rec["joins"] = [j for j in rec["joins"] if now - j[0] <= s.raid_window]
    rec["joins"].append((now, *who))
    if len(rec["joins"]) < s.raid_joins:
        return None

    rec["until"] = now + s.raid_hold * 60
    rec["seen"] = [(uid, name, uname) for _ts, uid, name, uname in rec["joins"]]
    rec["joins"] = []
    await _card_start(bot, chat, s, rec)
    await db.add_event(chat.id, "raid",
                       f"набег: {len(rec['seen'])} входов за {s.raid_window} сек")
    runtime.spawn(_watch_end(bot, chat))
    return s.raid_action


async def apply(bot: Bot, chat, user, s, action: str) -> bool:
    """Сделать с вошедшим то, что выбрано в настройках. True — сделали."""
    from . import moderation
    if action == "kick":
        pid, _err = await moderation.kick(bot, chat.id, user, "набег на чат", None)
        return pid is not None
    if action == "captcha":
        from ..handlers import group
        return await group.ask_captcha(bot, chat, user, s)
    # мут до конца режима, с запасом: снимут раньше кнопкой в списке наказаний
    pid = await moderation.apply_punishment(bot, chat.id, user, "mute",
                                            s.raid_hold, "набег на чат", None)
    return pid is not None


def _people(seen: list[tuple]) -> str:
    """Список вошедших цитатой: по нему админ и проверяет, кто это был."""
    if not seen:
        return ""
    rows = []
    for uid, name, uname in seen[:config.RAID_LIST_LIMIT]:
        who = utils.esc(name or str(uid))
        rows.append(f"{who}" + (f" @{utils.esc(uname)}" if uname else "") + f" · {uid}")
    more = len(seen) - len(rows)
    if more > 0:
        rows.append(f"…и ещё {more}")
    return ("\n\n👥 <b>Вошли:</b>\n<blockquote expandable>"
            + "\n".join(rows) + "</blockquote>")


async def _card_start(bot: Bot, chat, s, rec: dict) -> None:
    from . import moderation
    b = InlineKeyboardBuilder()
    b.row(_btn("🔓 Снять режим", f"k:raidoff:{chat.id}"),
          _btn("⛔ Забанить всех", f"k:raidban:{chat.id}"))
    text = (
        f"🛡 <b>Набег</b> · {utils.esc(chat.title)}\n"
        f"👥 Вошло: <b>{len(rec['seen'])}</b> за {s.raid_window} сек\n"
        f"⚙️ Пока идёт набег: "
        f"{config.RAID_ACTION_LABELS.get(s.raid_action, s.raid_action)}\n"
        f"⏳ Режим до {utils.fmt_ts(int(rec['until']))}"
        + _people(rec["seen"])
    )
    rec["cards"] = await moderation.send_card(bot, chat.id, config.BIT_RAID, text,
                                              markup=b.as_markup())


def _btn(text: str, data: str):
    from aiogram.types import InlineKeyboardButton
    return InlineKeyboardButton(text=text, callback_data=data)


async def _watch_end(bot: Bot, chat) -> None:
    """Дождаться конца режима и отчитаться, сколько всего вошло."""
    while True:
        rec = _state.get(chat.id)
        if rec is None:
            return
        left = rec["until"] - time.time()
        if left <= 0:
            break
        await asyncio.sleep(min(left, 30))
    rec = _state.pop(chat.id, None)
    if rec is None:
        return
    from . import moderation
    await moderation.send_card(
        bot, chat.id, config.BIT_RAID,
        f"🛡 <b>Набег закончился</b> · {utils.esc(chat.title)}\n"
        f"👥 Всего вошло: <b>{len(rec['seen'])}</b>" + _people(rec["seen"]))
    await db.add_event(chat.id, "raid", f"набег закончился: вошло {len(rec['seen'])}")


async def ban_all(bot: Bot, chat_id: int, by_id: int) -> int:
    """Забанить всех, кто вошёл за набег. Вернуть, скольких забанили."""
    from . import moderation
    done = 0
    people = joined(chat_id)[:config.RAID_BAN_LIMIT]
    for i, (uid, name, uname) in enumerate(people, 1):
        user = types.SimpleNamespace(id=uid, full_name=name, username=uname)
        try:
            if await moderation.apply_punishment(bot, chat_id, user, "ban", 0,
                                                 "набег на чат", by_id):
                done += 1
        except Exception:
            logger.warning("бан по набегу не прошёл: %s в %s", uid, chat_id,
                           exc_info=True)
        if i < len(people):
            await asyncio.sleep(config.RAID_BAN_DELAY)   # лимиты Telegram
    return done
