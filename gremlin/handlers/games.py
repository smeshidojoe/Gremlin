"""Игры в чате: рулетка, дуэль, королевская битва, суд, титулы недели;
и жесты — любовь, мог, статус.

Каждая включается отдельно в меню чата и может быть открыта либо всем, либо
только админам. Наказания настоящие — выдаются через общий механизм, поэтому
снимаются обычной кнопкой в меню наказаний.

Итоговое сообщение любой партии само исчезает через десять минут: игра
разовая, в истории чата ей делать нечего.
"""
import asyncio
import json
import logging
import random
import re
import time
from types import SimpleNamespace

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import config, db, runtime, utils
from ..services import adm_cache, countdown, moderation

logger = logging.getLogger("gremlin.games")

router = Router()
router.message.filter(F.chat.type.in_({"group", "supergroup"}))

# Револьвер один на чат: барабан на RUS_CHANCE гнёзд и один патрон.
# Раньше каждый выстрел был независимым «один из шести», а ожидание в 6 часов
# висело на человеке — по сути монетка, которую можно бросить раз в шесть
# часов. Барабан ничего не помнил, и пять промахов подряд выглядели как
# поломка. Теперь он проворачивается: шанс растёт с каждым щелчком, на
# последнем гнезде выстрел гарантирован, и после выстрела револьвер уходит
# на перезарядку для всего чата.
#
# Состояние лежит в kv, а не в памяти: иначе перезапуск бота бесплатно
# перезаряжал бы револьвер и обнулял набитые щелчки.
_RUS_KEY = "rus_drum:{}"
# один щелчок за раз: двое, нажавшие одновременно, иначе прочли бы барабан
# в одном положении и провернули его на одно гнездо вместо двух
_rus_locks: dict[int, asyncio.Lock] = {}


async def _drum_load(chat_id: int) -> dict:
    raw = await db.kv_get(_RUS_KEY.format(chat_id))
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}


async def _drum_save(chat_id: int, drum: dict) -> None:
    await db.kv_set(_RUS_KEY.format(chat_id), json.dumps(drum))


async def _pull(chat_id: int, reload_min: int) -> tuple[str, int]:
    """Нажать на спуск. Вернуть (что вышло, число).

    reload_min — перезарядка после выстрела, из настроек чата.

    ('reload', секунд до готовности) — револьвер на перезарядке;
    ('miss', сколько гнёзд осталось) — щелчок, барабан провернулся;
    ('hit', 0) — выстрел, револьвер ушёл на перезарядку.
    """
    lock = _rus_locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        now = int(time.time())
        drum = await _drum_load(chat_id)
        if drum.get("reload_until", 0) > now:
            return "reload", drum["reload_until"] - now
        if "bullet" not in drum:
            # заряжаем: патрон в случайное гнездо, крутим с нуля
            drum = {"bullet": random.randrange(config.RUS_CHANCE), "pulls": 0}
        if drum["pulls"] >= drum["bullet"]:
            await _drum_save(chat_id, {"reload_until": now + reload_min * 60})
            return "hit", 0
        drum["pulls"] += 1
        await _drum_save(chat_id, drum)
        return "miss", config.RUS_CHANCE - drum["pulls"]
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


def _later(bot: Bot, chat_id: int, msg_id: int,
           delay: int = config.GAME_CLEANUP) -> None:
    runtime.spawn(_cleanup(bot, chat_id, msg_id, delay))


async def _who(user_id: int) -> str:
    row = await db.get_user(user_id)
    return utils.mention(user_id, row["first_name"] if row else None,
                         row["username"] if row else None)


def _replied(message: Message):
    """Сообщение, на которое ответили по-настоящему, или None.

    В чате с темами Telegram сам делает каждое сообщение темы «ответом» на
    её первое, служебное сообщение — без этой проверки команда без ответа
    доставалась бы автору темы: его вызывали на дуэль, судили, ему кидали
    жест.
    """
    reply = message.reply_to_message
    if (reply is None or reply.from_user is None
            or getattr(reply, "forum_topic_created", None) is not None):
        return None
    return reply


_STRICTER = {"ban": "и так забанен", "mute": "и так в муте навсегда"}


async def _punish(bot: Bot, chat_id: int, user_id: int, kind: str, minutes: int,
                  reason: str) -> str | None:
    """Выдать приз. Вернуть подпись для сообщения игры или None — нет прав.

    Мут складывается с уже идущим (см. moderation.game_punish), и тогда в
    подписи виден общий срок.
    """
    from ..services import net
    user = await net.user_stub(user_id, bot, chat_id)
    pid, total, stricter = await moderation.game_punish(
        bot, chat_id, user, kind, minutes, reason, None)
    label = prize_label(kind, minutes)
    if stricter:
        return f"{label}, но он {_STRICTER[stricter]}"
    if pid is None:
        return None
    await db.add_event(chat_id, "manual", f"игра: {reason} — {user_id}"
                       + (f", всего {total} мин" if total else ""))
    return label + (f" · всего {utils.fmt_minutes(total)}" if total else "")


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


async def _rus_target(message: Message, bot: Bot):
    """За кого крутим барабан: (игрок, отправил ли его админ).

    Обычно за того, кто позвал. Но админ может ответить командой на чужое
    сообщение — тогда крутим за автора этого сообщения: чат нередко просит
    «прогони его через рулетку», и админу не приходится объяснять, что
    вызвать может только сам человек.

    Обычному участнику так нельзя: иначе рулетка стала бы способом мутить
    кого хочешь чужими руками с шансом один к шести.
    """
    reply = _replied(message)
    author = reply.from_user if reply else None
    if author is None or author.id == message.from_user.id or author.is_bot:
        return message.from_user, False
    if message.from_user.id not in await adm_cache.chat_admin_ids(
            bot, message.chat.id):
        return message.from_user, False
    return author, True


async def cmd_roulette(message: Message, bot: Bot) -> None:
    if not await _allowed(bot, message, config.GAME_RUS):
        return
    player, by_admin = await _rus_target(message, bot)
    # Щелчок считаем сразу, до паузы: пока барабан «крутится» две секунды,
    # следующий игрок уже должен видеть его провёрнутым.
    s = await db.get_settings(message.chat.id)
    outcome, value = await _pull(message.chat.id, s.rus_cd)
    if outcome == "reload":
        sent = await message.reply(
            f"🔫 Револьвер на перезарядке. Будет готов через "
            f"{utils.fmt_minutes(value // 60 or 1)}.")
        _later(bot, message.chat.id, sent.message_id)
        return

    kind, minutes = await prize(s, config.GAME_RUS)
    who = utils.mention(player.id, player.full_name, player.username)
    sent = await message.reply("🔫 Крутим барабан…" if not by_admin
                               else f"🔫 Барабан крутят за {who}…")
    await asyncio.sleep(2)
    # итог выстрела оставляем в чате: он короткий, и по нему видно, кто
    # когда крутил. Самоуничтожается только служебная воркотня про перезарядку
    if outcome == "miss":
        word = utils.plural(value, "гнездо", "гнезда", "гнёзд")
        await sent.edit_text(
            f"🔫 {who}: {random.choice(_RUS_SAFE)}\n"
            f"<i>В барабане {value} {word} — шанс 1 из {value}.</i>")
        return
    reload_note = (f"<i>Револьвер ушёл на перезарядку на "
                   f"{utils.fmt_minutes(s.rus_cd)}.</i>")
    # патрон потрачен в любом случае: иначе админ разряжал бы барабан без
    # последствий, а перезарядку чату всё равно пришлось бы ждать
    if not await _can_target(bot, message.chat.id, player.id):
        await sent.edit_text(f"🔫 {who}: {random.choice(_RUS_HIT)}\n"
                             f"<i>…но админов пуля не берёт.</i>\n{reload_note}")
        return
    label = await _punish(bot, message.chat.id, player.id, kind, minutes,
                          "проиграл в русскую рулетку")
    tail = (f"{label[0].upper()}{label[1:]}." if label
            else "…но пистолет заклинило: у бота нет прав.")
    await sent.edit_text(f"🔫 {who}: {random.choice(_RUS_HIT)}\n{tail}\n"
                         f"{reload_note}")


# ---------- дуэль ----------

# Кто когда последний раз вызывал: (chat_id, user_id) -> time.time().
# Держим в памяти — перезапуск обнулит часовую паузу, для дуэли это мелочь.
_duel_last: dict[tuple[int, int], float] = {}

_DUEL_NO = (
    "{foe} струсил перед грозной силой {caller} и сбежал, теряя тапки.",
    "{foe} внезапно вспомнил, что у него суп на плите, и покинул поле боя.",
    "{foe} отказался: пистолет не подходит к цвету его носков.",
    "{foe} предъявил справку от мамы — к дуэлям не допущен.",
    "{foe} поймал взгляд {caller} и упал в обморок ещё до барьера.",
    "{foe} спрятался под стол и делает вид, что его тут нет.",
    "{foe} объявил себя пацифистом и ушёл обнимать деревья.",
    "{foe} заранее притворился мёртвым, чтобы не тратить время на дуэль.",
    "{foe} сбежал так быстро, что на его месте осталось облачко пыли.",
    "{foe} сослался на ретроградный Меркурий: дуэли ему сегодня противопоказаны.",
    "{foe} попросил перенести дуэль на после дождичка в четверг.",
)


async def _is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Админ чата или владелец бота: паузы и антиспам приколов их не держат."""
    return (user_id in config.ADMIN_IDS
            or user_id in await adm_cache.chat_admin_ids(bot, chat_id))


async def _quiet_penalty(bot: Bot, message: Message, minutes: int, reason: str,
                         wipe: int = 0) -> None:
    """Молча убрать команду (и wipe последних сообщений автора) и выдать мут.

    Без карточки в лог: это не нарушение правил чата, а перебор с приколом.
    Мут всё равно ложится в «Наказания», чтобы его можно было снять кнопкой.
    minutes=0 — только удаляем.
    """
    from ..services import deleting
    chat_id, user = message.chat.id, message.from_user
    ids = (moderation.take_recent(chat_id, user.id, wipe, skip=message.message_id)
           if wipe else [])
    await deleting.many(bot, chat_id, ids + [message.message_id])
    if not minutes:
        return
    pid, _total, _stricter = await moderation.game_punish(
        bot, chat_id, user, "mute", minutes, reason, None)
    if pid is not None:
        await db.add_event(chat_id, "manual", f"игра: {reason} — {user.id}")


def _prune(last: dict, older: float) -> None:
    """Выкинуть давние отметки, чтобы словари пауз не росли бесконечно."""
    if len(last) > 5000:
        for k in [k for k, t in last.items() if t < older]:
            del last[k]


async def cmd_duel(message: Message, bot: Bot) -> None:
    if not await _allowed(bot, message, config.GAME_DUEL):
        return
    chat_id, caller = message.chat.id, message.from_user.id
    admin = await _is_admin(bot, chat_id, caller)
    now = time.time()
    if not admin and now - _duel_last.get((chat_id, caller), 0) < config.DUEL_CD:
        await _quiet_penalty(bot, message, config.DUEL_CD_MUTE,
                             "вызов на дуэль раньше срока")
        return
    if _replied(message) is None:
        sent = await message.reply("⚔️ Вызывать на дуэль надо ответом на сообщение.")
        _later(bot, message.chat.id, sent.message_id, 60)
        return
    foe = _replied(message).from_user
    me = message.from_user
    if foe.id == me.id or foe.is_bot:
        sent = await message.reply("⚔️ С собой и с ботами не дерутся.")
        _later(bot, message.chat.id, sent.message_id, 60)
        return

    s = await db.get_settings(message.chat.id)
    kind, minutes = await prize(s, config.GAME_DUEL)
    b = InlineKeyboardBuilder()
    b.button(text="⚔️ Принять вызов", callback_data="g:duel")
    b.button(text="🏳️ Отказаться", callback_data="g:duelno")
    b.adjust(2)
    head = (f"⚔️ <b>Дуэль!</b>\n\n{utils.mention(me.id, me.full_name, me.username)} "
            f"вызывает {utils.mention(foe.id, foe.full_name, foe.username)}.\n"
            f"Проигравший получает {prize_label(kind, minutes)}.\n\n")
    sent = await message.answer(
        f"{head}⏳ На раздумья {countdown.label(config.DUEL_WAIT)}.",
        reply_markup=b.as_markup(),
    )
    key = (message.chat.id, sent.message_id)
    _duels[key] = {"caller": me.id, "foe": foe.id}
    # час идёт с вызова, а не с боя: иначе отказы соперника давали бы
    # вызывать снова и снова
    if not admin:
        _prune(_duel_last, now - config.DUEL_CD)
        _duel_last[(chat_id, caller)] = now
    runtime.spawn(_duel_timeout(bot, key, foe.id, head, b.as_markup()))


async def _duel_timeout(bot: Bot, key: tuple[int, int], foe: int, head: str,
                        markup) -> None:
    chat_id, msg_id = key

    async def draw(left: int) -> None:
        await bot.edit_message_text(f"{head}⏳ На раздумья {countdown.label(left)}.",
                                    chat_id=chat_id, message_id=msg_id,
                                    reply_markup=markup)

    # вызов приняли — отсчёт обрывается, дальше сообщение правит сама дуэль
    await countdown.run(config.DUEL_WAIT, draw, stop=lambda: key not in _duels)
    if _duels.pop(key, None) is None:
        return                       # дуэль уже состоялась
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
    await db.tally_add(key[0], winner, "duel_win")
    await db.tally_add(key[0], loser, "duel_loss")
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
        label = await _punish(bot, key[0], loser, kind, minutes, "проиграл дуэль")
        text = (f"⚔️ <b>Дуэль окончена</b>\n\n🏆 Победитель: {await _who(winner)}\n"
                f"💀 Проиграл: {await _who(loser)}"
                + (f" · {label}" if label
                   else " · но приз не вручить, у бота нет прав"))
    try:
        await cb.message.edit_text(text, reply_markup=None)
    except Exception:
        pass
    _later(bot, key[0], key[1])


@router.callback_query(F.data == "g:duelno")
async def cb_duel_no(cb: CallbackQuery, bot: Bot) -> None:
    key = (cb.message.chat.id, cb.message.message_id)
    duel = _duels.get(key)
    if duel is None:
        await cb.answer("Дуэль уже закончилась.", show_alert=True)
        return
    if cb.from_user.id != duel["foe"]:
        await cb.answer(CLICK_ONLY_PLAYERS, show_alert=True)
        return
    _duels.pop(key, None)
    await cb.answer("Позор засчитан.")
    line = random.choice(_DUEL_NO).format(foe=await _who(duel["foe"]),
                                          caller=await _who(duel["caller"]))
    try:
        await cb.message.edit_text(f"🏳️ <b>Дуэль не состоялась</b>\n\n{line}",
                                   reply_markup=None)
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
        f"👥 Бойцов: 0\n⏳ До начала матча: {countdown.label(config.BATTLE_JOIN)}",
        reply_markup=b.as_markup(),
    )
    key = (message.chat.id, sent.message_id)
    _battles[key] = set()
    runtime.spawn(_battle_run(bot, key))


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


async def _battle_edit(bot: Bot, chat_id: int, msg_id: int, log: list,
                       left: int) -> None:
    """Перерисовать сводку матча с таймером до следующего события."""
    tail = f"\n\n⏳ Следующее событие через {countdown.label(left)}" if left else ""
    await bot.edit_message_text("\n".join(log[-12:]) + tail, chat_id=chat_id,
                                message_id=msg_id, reply_markup=None)


async def _battle_draw(bot: Bot, chat_id: int, msg_id: int, log: list,
                       left: int) -> None:
    """То же разово, вне отсчёта."""
    try:
        await _battle_edit(bot, chat_id, msg_id, log, left)
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
    async def draw_join(left: int) -> None:
        await bot.edit_message_text(
            f"{head}\n\n👥 Бойцов: {len(_battles.get(key, ()))}\n"
            f"⏳ До начала матча: {countdown.label(left)}",
            chat_id=chat_id, message_id=msg_id, reply_markup=b.as_markup())

    await countdown.run(config.BATTLE_JOIN, draw_join)

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
        # Живой таймер до следующего события — без посекундного хвоста:
        # события идут подряд весь матч, и секунды в конце каждого дали бы
        # больше правок в минуту, чем Telegram разрешает
        await countdown.run(
            config.BATTLE_TICK,
            lambda left: _battle_edit(bot, chat_id, msg_id, log, left), fine=0)
        dead = fighters.pop()
        first_out = first_out or dead
        log.append(f"💀 {await _who(dead)} {_death(used)}")
        await _battle_draw(bot, chat_id, msg_id, log,
                           config.BATTLE_TICK if len(fighters) > 1 else 0)

    winner = fighters[0]
    tail = f"\n\n👑 Победитель: {await _who(winner)}"
    log = [line for line in log if not line.startswith("⏳")]
    if first_out and await _can_target(bot, chat_id, first_out):
        label = await _punish(bot, chat_id, first_out, kind, minutes,
                              "выбыл первым в королевской битве")
        if label:
            tail += f"\n💀 Первым пал {await _who(first_out)} — {label}"
    try:
        await bot.edit_message_text("\n".join(log[-12:]) + tail, chat_id=chat_id,
                                    message_id=msg_id, reply_markup=None)
    except Exception:
        pass
    _later(bot, chat_id, msg_id)


# ---------- народный суд ----------

async def cmd_court(message: Message, bot: Bot) -> None:
    if not await _allowed(bot, message, config.GAME_COURT):
        return
    if _replied(message) is None:
        sent = await message.reply("⚖️ Судить надо ответом на сообщение обвиняемого.")
        _later(bot, message.chat.id, sent.message_id, 60)
        return
    accused = _replied(message).from_user
    if accused.is_bot or not await _can_target(bot, message.chat.id, accused.id):
        sent = await message.reply("⚖️ Этот подсудимый неподсуден.")
        _later(bot, message.chat.id, sent.message_id, 60)
        return

    charge = " ".join((message.text or "").split()[1:]) or "без объяснения причин"
    b = InlineKeyboardBuilder()
    b.button(text="👎 Виновен", callback_data="g:court:1")
    b.button(text="👍 Невиновен", callback_data="g:court:0")
    b.adjust(2)
    head = (f"⚖️ <b>Народный суд</b>\n\n"
            f"Подсудимый: {utils.mention(accused.id, accused.full_name, accused.username)}\n"
            f"Обвинение: {utils.esc(charge)}\n\n")
    sent = await message.answer(head + _court_tail(config.COURT_VOTE, {}),
                                reply_markup=b.as_markup())
    key = (message.chat.id, sent.message_id)
    _courts[key] = {"accused": accused.id, "charge": charge, "votes": {}}
    runtime.spawn(_court_run(bot, key, head, b.as_markup()))


def _court_tail(left: int, votes: dict) -> str:
    """Таймер и сколько проголосовало. Как именно голосуют, до приговора не
    показываем: видя счёт, остальные просто присоединялись бы к большинству."""
    return f"⏳ Голосование: {countdown.label(left)}\n🗳 Голосов: {len(votes)}"


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


async def _punish_by_court(bot: Bot, chat_id: int, court: dict) -> str | None:
    s = await db.get_settings(chat_id)
    kind, minutes = await prize(s, config.GAME_COURT)
    return await _punish(bot, chat_id, court["accused"], kind, minutes,
                         f"приговор чата: {court['charge']}")


async def _court_run(bot: Bot, key: tuple[int, int], head: str, markup) -> None:
    chat_id, msg_id = key

    async def draw(left: int) -> None:
        court = _courts.get(key)
        if court is None:
            return
        await bot.edit_message_text(head + _court_tail(left, court["votes"]),
                                    chat_id=chat_id, message_id=msg_id,
                                    reply_markup=markup)

    await countdown.run(config.COURT_VOTE, draw)
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
            label = await _punish_by_court(bot, chat_id, court)
            text = head + (f"Решением большинства (1 человека, {who_voted}) "
                           f"подсудимый признан <b>виновным</b> — {label}."
                           if label
                           else "🔨 Виновен, но приговор не исполнить — нет прав.")
        else:
            text = (head + f"Решением большинства (1 человека, {who_voted}) "
                    f"подсудимый <b>оправдан</b>.")
    elif guilty > innocent:
        label = await _punish_by_court(bot, chat_id, court)
        text = head + (f"🔨 <b>Виновен!</b> Приговор — {label}."
                       if label
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

async def titles_text(chat_id: int, bot: Bot | None = None) -> str | None:
    """Итоги недели по статистике сообщений. None — награждать некого.

    Служебные аккаунты пропускаем: «Telegram» приносит в обсуждение посты
    канала и по счётчику легко обгоняет живых людей.

    Считаем только тех, кто в чате состоит: под постами привязанного канала
    пишут комментаторы, которые в группу не вступали, и «болтуном недели»
    оказывался случайный прохожий. Статус спрашиваем только у претендентов —
    это несколько запросов, а не по одному на каждого писавшего.
    """
    day = utils.day_num()
    people = {uid: f for uid, f in (await db.week_activity(chat_id)).items()
              if f["week"] > 0 and uid not in config.SERVICE_IDS}
    if bot is not None and people:
        from ..services import adm_cache
        # проверяем сверху вниз по активности и останавливаемся, когда набрали
        # десяток своих: дальше в титулы всё равно никто не попадёт
        checked, members = 0, {}
        for uid, f in sorted(people.items(), key=lambda kv: -kv[1]["week"]):
            if checked >= 15 or len(members) >= 10:
                break
            checked += 1
            if await adm_cache.is_member(bot, chat_id, uid):
                members[uid] = f
        people = members or people
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
        text = await titles_text(ch["chat_id"], bot)
        if not text:
            continue
        try:
            await bot.send_message(ch["chat_id"], text)
            done += 1
        except Exception:
            logger.warning("титулы: не отправить в %s", ch["chat_id"], exc_info=True)
        await asyncio.sleep(1)
    return done


# ---------- запуск из общего пайплайна ----------
#
# Раньше каждая игра висела своим хендлером и перехватывала сообщение целиком:
# aiogram отдаёт его первому подошедшему обработчику и на этом останавливается,
# поэтому «!суд заходи на t.me/spam» до модерации не доходило вовсе. Теперь
# игры зовутся последними, после всех проверок — на то, что их пережило.
# Комментарий к команде (обвинение в суде) при этом сохраняется: он проходит
# те же фильтры, что любой другой текст.
async def cmd_vanish(message: Message, bot: Bot) -> None:
    """Стереть последние сообщения позвавшего — или, у админа ответом, чужие.

    Историю чата бот читать не может: стирает только то, что запомнил сам
    (moderation.remember_message). Отвечать в чат нечем и незачем — команда
    исчезает вместе с сообщениями, в этом и смысл.
    """
    if not await _allowed(bot, message, config.GAME_VANISH):
        return
    chat_id = message.chat.id
    s = await db.get_settings(chat_id)
    target = message.from_user.id
    reply = _replied(message)
    author = reply.from_user if reply else None
    # ответом на чужое — только админ: иначе любой вычищал бы переписку других
    if (author is not None and author.id != target and not author.is_bot
            and target in await adm_cache.chat_admin_ids(bot, chat_id)):
        target = author.id
    ids = moderation.take_recent(chat_id, target, s.vanish_n, skip=message.message_id)
    from ..services import deleting
    await deleting.many(bot, chat_id, ids + [message.message_id])


# ---------- жесты: любовь, мог, статус ----------
#
# Не игры: приза нет, кидать может любой. Бот отвечает случайной фразой из
# списка чата, в фразе {кто}, {кому} и {сколько} подставляются сами.

# последний жест: (chat_id, user_id, бит жеста) -> time.time(); у каждого
# жеста своя пауза
_gest_last: dict[tuple[int, int, int], float] = {}
_status_last: dict[tuple[int, int], float] = {}

# бит -> (ключ «кинул», ключ «получил») в tally
_GEST_KEYS = {config.GAME_LOVE: ("love_gave", "love_got"),
              config.GAME_MOG: ("mog_gave", "mog_got")}
_GEST_HINT = {
    config.GAME_LOVE: "💘 Любовь кидают ответом на сообщение или так: "
                      "<code>!любовь @ник</code>.",
    config.GAME_MOG: "🗿 Моггнуть можно ответом на сообщение или так: "
                     "<code>!моггнуть @ник</code>.",
}
_GEST_SELF = {config.GAME_LOVE: "{кто} любит себя — и правильно 💖",
              config.GAME_MOG: "{кто} моггнул сам себя в зеркале 🪞"}
_UNKNOWN = "🤷 Не знаю, кто это: пусть сначала что-нибудь напишет в чат."


def tag(user_id: int, name: str | None, username: str | None) -> str:
    """Имя профиля ссылкой на профиль. Ник не показываем — только если имени
    нет вовсе, он идёт вместо него."""
    return (f'<a href="tg://user?id={user_id}">'
            f'{utils.esc(name or username or str(user_id))}</a>')


async def _target(message: Message, bot: Bot):
    """Кому жест: по @нику или упоминанию в самой команде, иначе автор
    сообщения, на которое ответили.

    None — не указано никого; "unknown" — ник есть, но такого человека бот
    не видел. Bot API не умеет искать людей по нику, поэтому ищем в своей
    базе — там все, кто писал при боте. Себя бот узнаёт и так.
    """
    from ..services import filters
    for ent in message.entities or ():
        if ent.type == "text_mention" and ent.user:
            u = ent.user
            return SimpleNamespace(id=u.id, full_name=u.full_name,
                                   username=u.username, is_bot=u.is_bot)
    names = filters.mentions_in(message)
    if names:
        me = await bot.me()
        if me.username and names[0].lower() == me.username.lower():
            return SimpleNamespace(id=me.id, full_name=getattr(me, "full_name", None),
                                   username=me.username, is_bot=True)
        row = await db.user_by_username(names[0])
        if row is None:
            return "unknown"
        return SimpleNamespace(id=row["user_id"], full_name=row["first_name"],
                               username=row["username"], is_bot=False)
    reply = _replied(message)
    return reply.from_user if reply is not None else None


async def _notice(bot: Bot, message: Message, text: str) -> None:
    sent = await message.reply(text)
    _later(bot, message.chat.id, sent.message_id, 60)


async def cmd_gesture(message: Message, bot: Bot, bit: int,
                      inline: bool = False) -> bool:
    """Кинуть жест. Вернуть, был ли это жест.

    inline — команда стоит посреди текста («дать тебе !любовь»). Тогда без
    адресата молчим и отдаём сообщение дальше триггерам: человек мог просто
    написать слово с восклицательным знаком, подсказка тут была бы шумом.
    """
    chat_id = message.chat.id
    s = await db.get_settings(chat_id)
    me = message.from_user
    if not s.games_on & bit:
        # Жест выключен — команду в начале сообщения убираем и, если так
        # настроено, мутим: чат, где жесты не нужны, не должен ими зарастать.
        # Посреди текста не трогаем: это может быть просто слово с «!»
        if inline:
            return False
        if s.gest_off_punish and not await _is_admin(bot, chat_id, me.id):
            await _quiet_penalty(bot, message, s.gest_mute, "жест, когда жесты выключены")
        return True
    target = await _target(message, bot)
    if target is None or target == "unknown":
        if inline:
            return False
        await _notice(bot, message, _GEST_HINT[bit] if target is None else _UNKNOWN)
        return True

    now = time.time()
    key = (chat_id, me.id, bit)
    if (not await _is_admin(bot, chat_id, me.id)
            and now - _gest_last.get(key, 0) < config.GESTURE_CD):
        await _quiet_penalty(bot, message, s.gest_mute, "спам жестами",
                             config.GESTURE_WIPE)
        return True
    _prune(_gest_last, now - config.GESTURE_CD)
    _gest_last[key] = now

    who = tag(me.id, me.full_name, me.username)
    if target.id == me.id:
        await message.reply(_GEST_SELF[bit].replace("{кто}", who))
        return True
    gave, got = _GEST_KEYS[bit]
    # Гремлину жесты считаем, как человеку. Чужим ботам — нет: от их имени
    # пишут анонимные админы и канал, и счёт был бы общим на всех сразу
    if target.is_bot and target.id != (await bot.me()).id:
        n = (await db.tally_get(chat_id, target.id)).get(got, 0)
    else:
        await db.tally_add(chat_id, me.id, gave)
        n = await db.tally_add(chat_id, target.id, got)

    owner = config.GESTURE_OWNER[bit]
    # список пуст (все фразы удалили) — берём встроенную, а не молчим
    ans = await db.ans_pick(owner, chat_id) or {
        "text": random.choice(config.GESTURE_SEED[owner]),
        "file_path": None, "media_type": None}
    subs = {"{кто}": who,
            "{кому}": tag(target.id, target.full_name, target.username),
            "{сколько}": str(n)}
    # отвечаем на сообщение того, кому жест; кинули по нику — на саму команду
    reply = _replied(message)
    anchor = (reply if reply is not None and reply.from_user.id == target.id
              else message)
    from ..services import triggers
    await triggers.send_answer(anchor, ans, subs=subs)
    return True


async def cmd_love(message: Message, bot: Bot) -> None:
    await cmd_gesture(message, bot, config.GAME_LOVE)


async def cmd_mog(message: Message, bot: Bot) -> None:
    await cmd_gesture(message, bot, config.GAME_MOG)


async def status_text(chat_id: int, user, itself: bool = False) -> str:
    """Карточка для !статус. itself — карточка самого Гремлина: сообщения
    свои он не считает, в дуэлях не участвует, жесты только получает."""
    t = await db.tally_get(chat_id, user.id)
    head = f"📋 <b>Статус</b> · {tag(user.id, user.full_name, user.username)}\n\n"
    if itself:
        return (head + "🤖 Это я, местный бот. Работаю, не жалуюсь.\n\n"
                f"❤️ Получил любви: <b>{t.get('love_got', 0)}</b>\n"
                f"🗿 Моггнули меня: <b>{t.get('mog_got', 0)}</b>")
    msgs = await db.msg_total(chat_id, user.id)
    return (
        head +
        f"💬 Сообщений в чате: <b>{msgs}</b>\n\n"
        f"❤️ Получил любви: <b>{t.get('love_got', 0)}</b>\n"
        f"💝 Подарил любви: <b>{t.get('love_gave', 0)}</b>\n\n"
        f"🗿 Моггнули его: <b>{t.get('mog_got', 0)}</b>\n"
        f"😎 Моггнул сам: <b>{t.get('mog_gave', 0)}</b>\n\n"
        f"🏆 Побед в дуэлях: <b>{t.get('duel_win', 0)}</b>\n"
        f"💀 Поражений в дуэлях: <b>{t.get('duel_loss', 0)}</b>"
    )


async def cmd_status(message: Message, bot: Bot) -> None:
    chat_id = message.chat.id
    s = await db.get_settings(chat_id)
    if not s.games_on & config.GAME_STATUS:
        return
    me = message.from_user
    admin = await _is_admin(bot, chat_id, me.id)
    now = time.time()
    # это просмотр, а не жест: лишние вызовы просто не замечаем
    if not admin and now - _status_last.get((chat_id, me.id), 0) < config.STATUS_CD:
        return
    _prune(_status_last, now - config.STATUS_CD)
    _status_last[(chat_id, me.id)] = now
    target = await _target(message, bot)
    if target == "unknown" and admin:
        await _notice(bot, message, _UNKNOWN)
        return
    # чужой профиль смотрят только админы; остальным — свой, и ещё ботов
    if target is None or target == "unknown" or (not admin and not target.is_bot):
        target = me
    itself = target.id == (await bot.me()).id
    sent = await message.reply(await status_text(chat_id, target, itself))
    _later(bot, chat_id, sent.message_id)


async def seed_gesture(cid: int, bit: int) -> None:
    """Включили жест, а фраз нет — кладём встроенные, чтобы было с чего начать."""
    owner = config.GESTURE_OWNER.get(bit)
    if owner and not await db.ans_list(owner, cid):
        for text in config.GESTURE_SEED[owner]:
            await db.ans_add(owner, cid, text)


COMMANDS = (
    (re.compile(r"^!(рулетка|roulette)(\s|$)", re.IGNORECASE), cmd_roulette),
    (re.compile(r"^!(дуэль|duel)(\s|$)", re.IGNORECASE), cmd_duel),
    (re.compile(r"^!(битва|battle)(\s|$)", re.IGNORECASE), cmd_battle),
    (re.compile(r"^!(суд|court)(\s|$)", re.IGNORECASE), cmd_court),
    (re.compile(r"^!vanish(\s|$)", re.IGNORECASE), cmd_vanish),
    (re.compile(r"^!(любить|любовь|love)(\s|$)", re.IGNORECASE), cmd_love),
    (re.compile(r"^!(моггнуть|моггнул|mog)(\s|$)", re.IGNORECASE), cmd_mog),
    (re.compile(r"^!(статус|status)(\s|$)", re.IGNORECASE), cmd_status),
)


# жесты срабатывают и посреди текста: «я бы хотел дать тебе !любовь»
INLINE_GESTURES = (
    (re.compile(r"(?:^|\s)!(любить|любовь|love)(?=$|[\s.,!?;:)…])", re.IGNORECASE),
     config.GAME_LOVE),
    (re.compile(r"(?:^|\s)!(моггнуть|моггнул|mog)(?=$|[\s.,!?;:)…])", re.IGNORECASE),
     config.GAME_MOG),
)


async def fire_game(bot: Bot, message: Message) -> bool:
    """Запустить игру, если сообщение начинается с её команды, или жест,
    если его команда стоит где-то в тексте."""
    text = message.text or ""
    for pattern, handler in COMMANDS:
        if pattern.match(text):
            await handler(message, bot)
            return True
    for pattern, bit in INLINE_GESTURES:
        if pattern.search(text):
            return await cmd_gesture(message, bot, bit, inline=True)
    return False
