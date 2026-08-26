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
    """Кому игра вправе выдать наказание.

    Админов и самого бота — нельзя. Тех, кто в чате не состоит, — тоже: под
    постами привязанного канала пишут и жмут кнопки люди, которые в группу не
    вступали, и приз им вручать некуда.
    """
    if user_id in await adm_cache.chat_admin_ids(bot, chat_id):
        return False
    if user_id == (await bot.me()).id:
        return False
    return await adm_cache.is_member(bot, chat_id, user_id)


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
    # итог выстрела оставляем в чате: он короткий, и по нему видно, кто
    # когда крутил. Самоуничтожается только служебная воркотня про кулдаун
    if not hit:
        await sent.edit_text(f"🔫 {who}: {random.choice(_RUS_SAFE)}")
        return
    if not await _can_target(bot, message.chat.id, message.from_user.id):
        await sent.edit_text(f"🔫 {who}: {random.choice(_RUS_HIT)}\n"
                             f"<i>…но админов пуля не берёт.</i>")
        return
    ok = await _punish(bot, message.chat.id, message.from_user.id, kind, minutes,
                       "проиграл в русскую рулетку")
    tail = (f"{prize_label(kind, minutes).capitalize()}." if ok
            else "…но пистолет заклинило: у бота нет прав.")
    await sent.edit_text(f"🔫 {who}: {random.choice(_RUS_HIT)}\n{tail}")


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


# Уже выпавшая причина почти не повторяется: на пятерых игроков одна и та же
# «бочка с огурцами» четыре раза выглядит как поломка, а не как шутка.
REPEAT_WEIGHT = 0.05


def _death(used: set[str]) -> str:
    """Причина выбывания: свежая — обычный шанс, повторная — 5%."""
    weights = [REPEAT_WEIGHT if d in used else 1.0 for d in _DEATHS]
    pick = random.choices(_DEATHS, weights=weights, k=1)[0]
    used.add(pick)
    return pick


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
        f"👥 Бойцов: 0\n⏳ До начала матча: {config.BATTLE_JOIN} сек",
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


async def _battle_draw(bot: Bot, chat_id: int, msg_id: int, log: list,
                       left: int) -> None:
    """Перерисовать сводку матча с таймером до следующего события."""
    tail = f"\n\n⏳ Следующее событие через {left} сек" if left else ""
    try:
        await bot.edit_message_text("\n".join(log[-12:]) + tail, chat_id=chat_id,
                                    message_id=msg_id, reply_markup=None)
    except Exception:
        pass          # текст не изменился или сообщение удалили


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
                f"{head}\n\n👥 Бойцов: {len(_battles.get(key, ()))}\n"
                f"⏳ До начала матча: {left} сек",
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

    used: set[str] = set()
    log = ["🏝 <b>Сводка матча:</b>\n",
           f"На арене {len(fighters)} "
           f"{utils.plural(len(fighters), 'боец', 'бойца', 'бойцов')}."]
    first_out = None
    while len(fighters) > 1:
        # тикаем чаще, чем выбиваем: внизу живой таймер до следующего события
        for left in range(config.BATTLE_TICK, 0, -config.BATTLE_REFRESH):
            await asyncio.sleep(min(config.BATTLE_REFRESH, left))
            await _battle_draw(bot, chat_id, msg_id, log,
                               max(0, left - config.BATTLE_REFRESH))
        dead = fighters.pop()
        first_out = first_out or dead
        log.append(f"💀 {await _who(dead)} {_death(used)}")
        await _battle_draw(bot, chat_id, msg_id, log,
                           config.BATTLE_TICK if len(fighters) > 1 else 0)

    winner = fighters[0]
    tail = f"\n\n👑 Победитель: {await _who(winner)}"
    log = [line for line in log if not line.startswith("⏳")]
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
        f"⏳ Голосование {config.COURT_VOTE} сек\n👎 0 · 👍 0",
        reply_markup=b.as_markup(),
    )
    key = (message.chat.id, sent.message_id)
    _courts[key] = {"accused": accused.id, "charge": charge, "votes": {}}
    asyncio.create_task(_court_run(bot, key))


_CHEATS = (
    "Дважды за одно и то же? В зале суда так не делают. Минута молчания — тебе.",
    "Попытка накрутить голос замечена. Присяжный удаляется на минуту.",
    "Один человек — один голос. За жульничество минута тишины.",
)


@router.callback_query(F.data.startswith("g:court:"))
async def cb_court(cb: CallbackQuery, bot: Bot) -> None:
    key = (cb.message.chat.id, cb.message.message_id)
    court = _courts.get(key)
    if court is None:
        await cb.answer("Суд уже вынес решение.", show_alert=True)
        return
    if cb.from_user.id == court["accused"]:
        await cb.answer("Подсудимый не голосует.", show_alert=True)
        return
    guilty = cb.data.endswith("1")
    was = court["votes"].get(cb.from_user.id)
    if was is not None and was == guilty:
        # жмёт свою же кнопку второй раз — накрутка
        await cb.answer(random.choice(_CHEATS), show_alert=True)
        if await _can_target(bot, key[0], cb.from_user.id):
            await _punish(bot, key[0], cb.from_user.id, "mute",
                          config.COURT_CHEAT_MUTE, "жульничал на голосовании")
        return
    court["votes"][cb.from_user.id] = guilty
    await cb.answer("Голос изменён" if was is not None else "Голос учтён")


async def _punish_by_court(bot: Bot, chat_id: int, court: dict) -> bool:
    s = await db.get_settings(chat_id)
    kind, minutes = await prize(s, config.GAME_COURT)
    return await _punish(bot, chat_id, court["accused"], kind, minutes,
                         f"приговор чата: {court['charge']}")


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

    if not votes:
        text = head + "Присяжные разошлись по домам. Дело закрыто за отсутствием суда."
    elif len(votes) == 1:
        voter, verdict = next(iter(votes.items()))
        who_voted = await _who(voter)
        if random.random() < 0.5:
            text = (head + f"Голосовал ровно один человек ({who_voted}), и суд "
                    f"счёл это несерьёзным. Дело закрыто.")
        elif verdict:
            ok = await _punish_by_court(bot, chat_id, court)
            text = head + (f"Решением большинства (1 человека, {who_voted}) "
                           f"подсудимый признан <b>виновным</b>." if ok
                           else "🔨 Виновен, но приговор не исполнить — нет прав.")
        else:
            text = (head + f"Решением большинства (1 человека, {who_voted}) "
                    f"подсудимый <b>оправдан</b>.")
    elif guilty > innocent:
        ok = await _punish_by_court(bot, chat_id, court)
        s = await db.get_settings(chat_id)
        kind, minutes = await prize(s, config.GAME_COURT)
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
    """Итоги недели по статистике сообщений. None — награждать некого.

    Служебные аккаунты пропускаем: «Telegram» приносит в обсуждение посты
    канала и по счётчику легко обгоняет живых людей.
    """
    day = utils.day_num()
    people = {uid: f for uid, f in (await db.week_activity(chat_id)).items()
              if f["week"] > 0 and uid not in config.SERVICE_IDS}
    if not people:
        return None

    lines = ["🏆 <b>Титулы недели</b>\n"]
    top = max(people.items(), key=lambda kv: kv[1]["week"])
    lines.append(f"🥇 <b>Болтун недели</b> — {await _who(top[0])}\n"
                 f"    больше всех сообщений за неделю: {top[1]['week']}")

    quiet = min(people.items(), key=lambda kv: kv[1]["week"])
    if quiet[0] != top[0]:
        lines.append(f"🐢 <b>Молчун недели</b> — {await _who(quiet[0])}\n"
                     f"    заходил, но сказал всего {quiet[1]['week']}")

    rookies = {u: f for u, f in people.items()
               if (f["first_day"] or 0) >= day - 6 and u != top[0]}
    if rookies:
        rookie = max(rookies.items(), key=lambda kv: kv[1]["week"])
        lines.append(f"🌱 <b>Новичок недели</b> — {await _who(rookie[0])}\n"
                     f"    впервые заговорил на этой неделе и сразу "
                     f"{rookie[1]['week']}")

    grown = {u: f for u, f in people.items() if f["week"] > f["prev"] and f["prev"]}
    if grown:
        best = max(grown.items(), key=lambda kv: kv[1]["week"] - kv[1]["prev"])
        was, now = best[1]["prev"], best[1]["week"]
        lines.append(f"📈 <b>Прорыв недели</b> — {await _who(best[0])}\n"
                     f"    разошёлся сильнее прошлой недели: было {was}, стало {now}")

    steady = [u for u, f in people.items() if f["days"] >= 7]
    if steady:
        who = ", ".join([await _who(u) for u in steady[:3]])
        tail = f" и ещё {len(steady) - 3}" if len(steady) > 3 else ""
        lines.append(f"🎯 <b>Железная дисциплина</b> — {who}{tail}\n"
                     f"    не пропустили ни одного дня недели")
    return "\n".join(lines)


TITLES_KEY = "titles_sent"       # метка недели, чтобы не разослать дважды


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
        # сон мог кончиться чуть раньше срока: тогда следующий круг проснётся
        # снова в это же воскресенье. Метка недели не даёт разослать дважды.
        stamp = utils.local_now().strftime("%G-%V")
        if await db.kv_get(TITLES_KEY) == stamp:
            continue
        if utils.local_now().weekday() != 6:
            continue                     # проснулись не в тот день — ждём дальше
        await db.kv_set(TITLES_KEY, stamp)
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
