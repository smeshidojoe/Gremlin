"""Игры в чате: рулетка, дуэль, королевская битва, суд, титулы недели.

Каждая включается отдельно в меню чата и может быть открыта либо всем, либо
только админам. Наказания настоящие — выдаются через общий механизм, поэтому
снимаются обычной кнопкой в меню наказаний.

Итоговое сообщение любой партии само исчезает через десять минут: игра
разовая, в истории чата ей делать нечего.
"""
import asyncio
import logging
import random
import time

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import config, db, utils
from ..services import adm_cache, moderation

logger = logging.getLogger("gremlin.games")

router = Router()
router.message.filter(F.chat.type.in_({"group", "supergroup"}))

# кулдаун рулетки: (chat_id, user_id) -> когда крутил
_rus_fired: dict[tuple[int, int], float] = {}
# открытые дуэли и суды: (chat_id, message_id) -> состояние
_duels: dict[tuple[int, int], dict] = {}
_courts: dict[tuple[int, int], dict] = {}
# сбор бойцов: (chat_id, message_id) -> set(user_id)
_battles: dict[tuple[int, int], set] = {}

CLICK_ONLY_PLAYERS = "Это не твоя партия."


# ---------- общее ----------

async def prize(s, bit: int) -> tuple[str, int]:
    """Приз проигравшему в этой игре: (наказание, минуты)."""
    kind_field, min_field = config.GAME_FIELDS[bit]
    return getattr(s, kind_field), getattr(s, min_field)


def prize_label(kind: str, minutes: int) -> str:
    return "бан" if kind == "ban" else f"мут на {utils.fmt_minutes(minutes)}"


async def _allowed(bot: Bot, message: Message, bit: int) -> bool:
    """Игра включена, и этому человеку её звать можно."""
    s = await db.get_settings(message.chat.id)
    if not (s.games_on & bit):
        return False
    if s.games_adm & bit:
        return message.from_user.id in await adm_cache.chat_admin_ids(
            bot, message.chat.id)
    return True


async def _cleanup(bot: Bot, chat_id: int, msg_id: int,
                   delay: int = config.GAME_CLEANUP) -> None:
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception:
        pass


def _later(bot: Bot, chat_id: int, msg_id: int) -> None:
    asyncio.create_task(_cleanup(bot, chat_id, msg_id))


async def _who(user_id: int) -> str:
    row = await db.get_user(user_id)
    return utils.mention(user_id, row["first_name"] if row else None,
                         row["username"] if row else None)


async def _punish(bot: Bot, chat_id: int, user_id: int, kind: str, minutes: int,
                  reason: str) -> bool:
    from ..services import net
    user = await net.user_stub(user_id)
    pid = await moderation.apply_punishment(bot, chat_id, user, kind, minutes,
                                            reason, None)
    if pid is not None:
        await db.add_event(chat_id, "manual", f"игра: {reason} — {user_id}")
    return pid is not None


async def _can_target(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Мутить админов и самого бота нельзя — в играх тоже."""
    if user_id in await adm_cache.chat_admin_ids(bot, chat_id):
        return False
    return user_id != (await bot.me()).id


# ---------- русская рулетка ----------

_RUS_SAFE = (
    "щёлк. Пусто.",
    "щёлк. Барабан пожалел.",
    "щёлк. Осечка — живи пока.",
    "щёлк. Ничего. Даже обидно.",
)
_RUS_HIT = (
    "БАХ! Не повезло.",
    "БАХ! Барабан выбрал тебя.",
    "БАХ! Вот и поговорили.",
)


@router.message(F.text.regexp(r"^!(рулетка|roulette)(\s|$)"))
async def cmd_roulette(message: Message, bot: Bot) -> None:
    if not await _allowed(bot, message, config.GAME_RUS):
        return
    key = (message.chat.id, message.from_user.id)
    now = time.time()
    left = config.RUS_CD - (now - _rus_fired.get(key, 0))
    if left > 0:
        sent = await message.reply(
            f"🔫 Барабан ещё горячий. Возвращайся через "
            f"{utils.fmt_minutes(int(left // 60) or 1)}.")
        _later(bot, message.chat.id, sent.message_id)
        return
    _rus_fired[key] = now

    s = await db.get_settings(message.chat.id)
    kind, minutes = await prize(s, config.GAME_RUS)
    hit = random.randrange(config.RUS_CHANCE) == 0
    who = utils.mention(message.from_user.id, message.from_user.full_name,
                        message.from_user.username)
    sent = await message.reply("🔫 Крутим барабан…")
    await asyncio.sleep(2)
    if not hit:
        await sent.edit_text(f"🔫 {who}: {random.choice(_RUS_SAFE)}")
        _later(bot, message.chat.id, sent.message_id)
        return
    if not await _can_target(bot, message.chat.id, message.from_user.id):
        await sent.edit_text(f"🔫 {who}: {random.choice(_RUS_HIT)}\n"
                             f"<i>…но админов пуля не берёт.</i>")
        _later(bot, message.chat.id, sent.message_id)
        return
    ok = await _punish(bot, message.chat.id, message.from_user.id, kind, minutes,
                       "проиграл в русскую рулетку")
    tail = (f"{prize_label(kind, minutes).capitalize()}." if ok
            else "…но пистолет заклинило: у бота нет прав.")
    await sent.edit_text(f"🔫 {who}: {random.choice(_RUS_HIT)}\n{tail}")
    _later(bot, message.chat.id, sent.message_id)


# ---------- дуэль ----------

@router.message(F.text.regexp(r"^!(дуэль|duel)(\s|$)"))
async def cmd_duel(message: Message, bot: Bot) -> None:
    if not await _allowed(bot, message, config.GAME_DUEL):
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        sent = await message.reply("⚔️ Вызывать на дуэль надо ответом на сообщение.")
        _later(bot, message.chat.id, sent.message_id, 60)
        return
    foe = message.reply_to_message.from_user
    me = message.from_user
    if foe.id == me.id or foe.is_bot:
        sent = await message.reply("⚔️ С собой и с ботами не дерутся.")
        _later(bot, message.chat.id, sent.message_id, 60)
        return

    s = await db.get_settings(message.chat.id)
    kind, minutes = await prize(s, config.GAME_DUEL)
    b = InlineKeyboardBuilder()
    b.button(text="⚔️ Принять вызов", callback_data="g:duel")
    sent = await message.answer(
        f"⚔️ <b>Дуэль!</b>\n\n{utils.mention(me.id, me.full_name, me.username)} "
        f"вызывает {utils.mention(foe.id, foe.full_name, foe.username)}.\n"
        f"Проигравший получает {prize_label(kind, minutes)}.\n\n"
        f"⏳ На раздумья {config.DUEL_WAIT} сек.",
        reply_markup=b.as_markup(),
    )
    key = (message.chat.id, sent.message_id)
    _duels[key] = {"caller": me.id, "foe": foe.id}
    asyncio.create_task(_duel_timeout(bot, key, foe.id))


async def _duel_timeout(bot: Bot, key: tuple[int, int], foe: int) -> None:
    await asyncio.sleep(config.DUEL_WAIT)
    if _duels.pop(key, None) is None:
        return                       # дуэль уже состоялась
    chat_id, msg_id = key
    try:
        await bot.edit_message_text(
            f"⚔️ <b>Дуэль не состоялась</b>\n\n{await _who(foe)} струсил и не вышел "
            f"к барьеру.", chat_id=chat_id, message_id=msg_id, reply_markup=None)
    except Exception:
        pass
    _later(bot, chat_id, msg_id)


@router.callback_query(F.data == "g:duel")
async def cb_duel(cb: CallbackQuery, bot: Bot) -> None:
    key = (cb.message.chat.id, cb.message.message_id)
    duel = _duels.get(key)
    if duel is None:
        await cb.answer("Дуэль уже закончилась.", show_alert=True)
        return
    if cb.from_user.id != duel["foe"]:
        await cb.answer(CLICK_ONLY_PLAYERS, show_alert=True)
        return
    _duels.pop(key, None)
    await cb.answer("К барьеру!")

    loser = random.choice([duel["caller"], duel["foe"]])
    winner = duel["foe"] if loser == duel["caller"] else duel["caller"]
    try:
        await cb.message.edit_text("⚔️ <b>Дуэль!</b>\n\nСходятся у барьера…",
                                   reply_markup=None)
    except Exception:
        pass
    await asyncio.sleep(2)
    if not await _can_target(bot, key[0], loser):
        text = (f"⚔️ <b>Дуэль</b>\n\nПобедил {await _who(winner)}, но проигравший — "
                f"админ, и пуля прошла мимо.")
    else:
        s = await db.get_settings(key[0])
        kind, minutes = await prize(s, config.GAME_DUEL)
        ok = await _punish(bot, key[0], loser, kind, minutes, "проиграл дуэль")
        text = (f"⚔️ <b>Дуэль окончена</b>\n\n🏆 Победитель: {await _who(winner)}\n"
                f"💀 Проиграл: {await _who(loser)}"
                + (f" · {prize_label(kind, minutes)}" if ok
                   else " · но приз не вручить, у бота нет прав"))
    try:
        await cb.message.edit_text(text, reply_markup=None)
    except Exception:
        pass
    _later(bot, key[0], key[1])


# ---------- королевская битва ----------

_DEATHS = (
    "утонул в бочке с огурцами",
    "ушёл за хлебом и не вернулся",
    "съеден админом",
    "поскользнулся на банановой кожуре",
    "решил не участвовать и телепортировался домой",
    "пал жертвой опечатки",
    "заблудился в комментариях",
    "проиграл в камень-ножницы-бумагу самому себе",
    "случайно нажал «покинуть чат»",
    "уснул прямо на арене",
    "был затоптан стадом гусей",
    "исчез при загадочных обстоятельствах",
)


@router.message(F.text.regexp(r"^!(битва|battle)(\s|$)"))
async def cmd_battle(message: Message, bot: Bot) -> None:
    if not await _allowed(bot, message, config.GAME_BATTLE):
        return
    s = await db.get_settings(message.chat.id)
    kind, minutes = await prize(s, config.GAME_BATTLE)
    b = InlineKeyboardBuilder()
    b.button(text="🏝 Вписаться", callback_data="g:battle")
    sent = await message.answer(
        f"🏝 <b>Королевская битва!</b>\n\nВыживет один. Первый выбывший получает "
        f"{prize_label(kind, minutes)}, последний — славу.\n\n"
        f"⏳ Сбор: {config.BATTLE_JOIN} сек\n👥 Бойцов: 0",
        reply_markup=b.as_markup(),
    )
    key = (message.chat.id, sent.message_id)
    _battles[key] = set()
    asyncio.create_task(_battle_run(bot, key))


@router.callback_query(F.data == "g:battle")
async def cb_battle(cb: CallbackQuery, bot: Bot) -> None:
    key = (cb.message.chat.id, cb.message.message_id)
    fighters = _battles.get(key)
    if fighters is None:
        await cb.answer("Битва уже началась.", show_alert=True)
        return
    if cb.from_user.id in fighters:
        await cb.answer("Ты уже на арене.")
        return
    fighters.add(cb.from_user.id)
    await cb.answer("Ты на арене 🏝")


async def _battle_run(bot: Bot, key: tuple[int, int]) -> None:
    chat_id, msg_id = key
    s = await db.get_settings(chat_id)
    kind, minutes = await prize(s, config.GAME_BATTLE)
    head = ("🏝 <b>Королевская битва!</b>\n\nВыживет один. Первый выбывший получает "
            f"{prize_label(kind, minutes)}, последний — славу.")
    b = InlineKeyboardBuilder()
    b.button(text="🏝 Вписаться", callback_data="g:battle")
    left = config.BATTLE_JOIN
    while left > 0:
        await asyncio.sleep(min(10, left))
        left -= min(10, left)
        try:
            await bot.edit_message_text(
                f"{head}\n\n⏳ Сбор: {left} сек\n👥 Бойцов: {len(_battles.get(key, ()))}",
                chat_id=chat_id, message_id=msg_id, reply_markup=b.as_markup())
        except Exception:
            pass

    fighters = list(_battles.pop(key, set()))
    random.shuffle(fighters)
    if len(fighters) < 2:
        try:
            await bot.edit_message_text(
                "🏝 <b>Битва отменена</b>\n\nМеньше двух бойцов. Арена пустует.",
                chat_id=chat_id, message_id=msg_id, reply_markup=None)
        except Exception:
            pass
        _later(bot, chat_id, msg_id)
        return

    log = [f"🏝 <b>Королевская битва</b>\n\nНа арене {len(fighters)} "
           f"{utils.plural(len(fighters), 'боец', 'бойца', 'бойцов')}."]
    first_out = None
    while len(fighters) > 1:
        await asyncio.sleep(config.BATTLE_TICK)
        dead = fighters.pop()
        first_out = first_out or dead
        log.append(f"💀 {await _who(dead)} {random.choice(_DEATHS)}")
        try:
            await bot.edit_message_text("\n".join(log[-12:]), chat_id=chat_id,
                                        message_id=msg_id, reply_markup=None)
        except Exception:
            pass

    winner = fighters[0]
    tail = f"\n\n👑 Победитель: {await _who(winner)}"
    if first_out and await _can_target(bot, chat_id, first_out):
        ok = await _punish(bot, chat_id, first_out, kind, minutes,
                           "выбыл первым в королевской битве")
        if ok:
            tail += (f"\n💀 Первым пал {await _who(first_out)} — "
                     f"{prize_label(kind, minutes)}")
    try:
        await bot.edit_message_text("\n".join(log[-12:]) + tail, chat_id=chat_id,
                                    message_id=msg_id, reply_markup=None)
    except Exception:
        pass
    _later(bot, chat_id, msg_id)


# ---------- народный суд ----------

@router.message(F.text.regexp(r"^!(суд|court)(\s|$)"))
async def cmd_court(message: Message, bot: Bot) -> None:
    if not await _allowed(bot, message, config.GAME_COURT):
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        sent = await message.reply("⚖️ Судить надо ответом на сообщение обвиняемого.")
        _later(bot, message.chat.id, sent.message_id, 60)
        return
    accused = message.reply_to_message.from_user
    if accused.is_bot or not await _can_target(bot, message.chat.id, accused.id):
        sent = await message.reply("⚖️ Этот подсудимый неподсуден.")
        _later(bot, message.chat.id, sent.message_id, 60)
        return

    charge = " ".join((message.text or "").split()[1:]) or "без объяснения причин"
    b = InlineKeyboardBuilder()
    b.button(text="👎 Виновен", callback_data="g:court:1")
    b.button(text="👍 Невиновен", callback_data="g:court:0")
    b.adjust(2)
    sent = await message.answer(
        f"⚖️ <b>Народный суд</b>\n\n"
        f"Подсудимый: {utils.mention(accused.id, accused.full_name, accused.username)}\n"
        f"Обвинение: {utils.esc(charge)}\n\n"
        f"⏳ Голосование {config.COURT_VOTE} сек · нужно минимум "
        f"{config.COURT_MIN_VOTES} голосов\n👎 0 · 👍 0",
        reply_markup=b.as_markup(),
    )
    key = (message.chat.id, sent.message_id)
    _courts[key] = {"accused": accused.id, "charge": charge, "votes": {}}
    asyncio.create_task(_court_run(bot, key))


@router.callback_query(F.data.startswith("g:court:"))
async def cb_court(cb: CallbackQuery) -> None:
    key = (cb.message.chat.id, cb.message.message_id)
    court = _courts.get(key)
    if court is None:
        await cb.answer("Суд уже вынес решение.", show_alert=True)
        return
    if cb.from_user.id == court["accused"]:
        await cb.answer("Подсудимый не голосует.", show_alert=True)
        return
    court["votes"][cb.from_user.id] = cb.data.endswith("1")
    await cb.answer("Голос учтён")


async def _court_run(bot: Bot, key: tuple[int, int]) -> None:
    chat_id, msg_id = key
    await asyncio.sleep(config.COURT_VOTE)
    court = _courts.pop(key, None)
    if court is None:
        return
    votes = court["votes"]
    guilty = sum(1 for v in votes.values() if v)
    innocent = len(votes) - guilty
    who = await _who(court["accused"])
    head = (f"⚖️ <b>Народный суд</b>\n\nПодсудимый: {who}\n"
            f"Обвинение: {utils.esc(court['charge'])}\n\n"
            f"👎 {guilty} · 👍 {innocent}\n\n")

    if len(votes) < config.COURT_MIN_VOTES:
        text = head + "Кворум не собрался — дело закрыто."
    elif guilty > innocent:
        s = await db.get_settings(chat_id)
        kind, minutes = await prize(s, config.GAME_COURT)
        ok = await _punish(bot, chat_id, court["accused"], kind, minutes,
                           f"приговор чата: {court['charge']}")
        text = head + (f"🔨 <b>Виновен!</b> Приговор — {prize_label(kind, minutes)}."
                       if ok
                       else "🔨 <b>Виновен!</b> Но приговор не исполнить — нет прав.")
    else:
        text = head + "🕊 <b>Оправдан.</b> Народ на твоей стороне."
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id,
                                    reply_markup=None)
    except Exception:
        pass
    _later(bot, chat_id, msg_id)


# ---------- титулы недели ----------

async def titles_text(chat_id: int) -> str | None:
    """Итоги недели по статистике сообщений. None — награждать некого."""
    day = utils.day_num()
    people = {uid: f for uid, f in (await db.week_activity(chat_id)).items()
              if f["week"] > 0}
    if not people:
        return None

    lines = ["🏆 <b>Титулы недели</b>\n"]
    top = max(people.items(), key=lambda kv: kv[1]["week"])
    lines.append(f"🥇 <b>Болтун недели</b> — {await _who(top[0])} "
                 f"({top[1]['week']} сообщений)")

    quiet = min(people.items(), key=lambda kv: kv[1]["week"])
    if quiet[0] != top[0]:
        lines.append(f"🐢 <b>Молчун недели</b> — {await _who(quiet[0])} "
                     f"({quiet[1]['week']} — но ведь был!)")

    rookies = {u: f for u, f in people.items() if (f["first_day"] or 0) >= day - 6}
    if rookies:
        rookie = max(rookies.items(), key=lambda kv: kv[1]["week"])
        lines.append(f"🌱 <b>Новичок недели</b> — {await _who(rookie[0])} "
                     f"(ворвался с {rookie[1]['week']})")

    grown = {u: f for u, f in people.items() if f["week"] > f["prev"] and f["prev"]}
    if grown:
        best = max(grown.items(), key=lambda kv: kv[1]["week"] - kv[1]["prev"])
        delta = best[1]["week"] - best[1]["prev"]
        lines.append(f"📈 <b>Прорыв недели</b> — {await _who(best[0])} (+{delta})")

    steady = [u for u, f in people.items() if f["days"] >= 7]
    if steady:
        lines.append(f"🎯 <b>Железная дисциплина</b> — "
                     f"{', '.join([await _who(u) for u in steady[:3]])} "
                     f"(писали каждый день)")
    return "\n".join(lines)


async def titles_scheduler(bot: Bot) -> None:
    """Раз в воскресенье в 19:15 по местному времени раздаём титулы."""
    while True:
        now = utils.local_now()
        target = now.replace(hour=config.TITLES_HOUR, minute=config.TITLES_MINUTE,
                             second=0, microsecond=0)
        days_ahead = (6 - now.weekday()) % 7        # 6 = воскресенье
        if days_ahead == 0 and now >= target:
            days_ahead = 7
        from datetime import timedelta
        target += timedelta(days=days_ahead)
        await asyncio.sleep(max(60, (target - now).total_seconds()))
        try:
            await send_titles(bot)
        except Exception:
            logger.warning("титулы недели не разошлись", exc_info=True)


async def send_titles(bot: Bot) -> int:
    """Разослать титулы во все чаты, где игра включена. Вернуть число чатов."""
    done = 0
    for ch in await db.moderated_chats():
        s = await db.get_settings(ch["chat_id"])
        if not (s.games_on & config.GAME_TITLES):
            continue
        text = await titles_text(ch["chat_id"])
        if not text:
            continue
        try:
            await bot.send_message(ch["chat_id"], text)
            done += 1
        except Exception:
            logger.warning("титулы: не отправить в %s", ch["chat_id"], exc_info=True)
        await asyncio.sleep(1)
    return done
