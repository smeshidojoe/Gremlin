"""Приколы владельца бота: пока тут одна бан-рулетка.

Раздел виден только владельцу (config.ADMIN_IDS) — это развлечение, а не
инструмент модерации, и в чужих чатах ему делать нечего.

Рулетка бывает двух видов. «Весь чат» — победителя выбираем из тех, кто писал
за последний месяц (список участников через Bot API не получить, поэтому берём
свою же статистику сообщений). «По кнопке» — сначала собираем добровольцев,
потом крутим среди них.
"""
import asyncio
import json
import logging
import random

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import config, db, utils
from ..services import adm_cache, moderation

logger = logging.getLogger("gremlin.fun")

router = Router()

SPIN_TICKS = 8          # сколько раз крутим точки
SPIN_STEP = 0.7         # пауза между кадрами, сек
CLEANUP_AFTER = 15 * 60  # через сколько убрать сообщение рулетки
ACTIVE_DAYS = 30        # кого считаем «писавшим недавно»
MEMBER_CHECKS = 60      # сколько кандидатов максимум проверяем на членство

KIND_LABEL = {"ban": "бан", "mute": "мут"}
MODE_LABEL = {"all": "весь чат", "opt": "по кнопке"}
TIMER_PRESETS = (30, 60, 120, 300, 600)

_DEFAULTS = {"chat_id": 0, "kind": "mute", "minutes": 60, "mode": "all", "timer": 60}

# идущие розыгрыши «по кнопке»: (chat_id, message_id) -> set(user_id)
_joined: dict[tuple[int, int], set[int]] = {}


def _key(owner_id: int) -> str:
    return f"roulette:{owner_id}"


async def _cfg(owner_id: int) -> dict:
    raw = await db.kv_get(_key(owner_id))
    cfg = dict(_DEFAULTS)
    if raw:
        try:
            cfg.update(json.loads(raw))
        except ValueError:
            pass
    return cfg


async def _save(owner_id: int, cfg: dict) -> None:
    await db.kv_set(_key(owner_id), json.dumps(cfg))


def _own(cb: CallbackQuery) -> bool:
    return cb.from_user.id in config.ADMIN_IDS


# ---------- экраны ----------

async def view_fun() -> tuple[str, InlineKeyboardMarkup]:
    b = InlineKeyboardBuilder()
    b.button(text="🎯 Бан-рулетка", callback_data="f:rl")
    b.button(text="⬅️ Назад", callback_data="u:home")
    b.adjust(1)
    return ("<b>🎪 Приколы</b>\n\nРазвлечения для своих чатов. "
            "Раздел видите только вы."), b.as_markup()


async def view_roulette(owner_id: int) -> tuple[str, InlineKeyboardMarkup]:
    cfg = await _cfg(owner_id)
    ch = await db.get_chat(cfg["chat_id"]) if cfg["chat_id"] else None
    where = utils.esc(ch["title"]) if ch else "<b>не выбран</b>"
    dur = utils.fmt_minutes(cfg["minutes"]) if cfg["kind"] == "mute" else "навсегда"

    text = (
        "<b>🎯 Бан-рулетка</b>\n\n"
        "Бот объявляет розыгрыш, крутит барабан и выдаёт наказание победителю. "
        "Сообщение само исчезает через 15 минут.\n\n"
        f"💬 Чат: {where}\n"
        f"🔨 Приз: <b>{KIND_LABEL[cfg['kind']]}</b> · {dur}\n"
        f"🎛 Режим: <b>{MODE_LABEL[cfg['mode']]}</b>"
        + (f"\n⏳ Сбор участников: <b>{cfg['timer']} сек</b>" if cfg["mode"] == "opt" else "")
        + "\n\n<i>«Весь чат» — участвуют все, кто писал за последний месяц. "
          "«По кнопке» — только нажавшие. Админы не участвуют.</i>"
    )
    b = InlineKeyboardBuilder()
    b.row(_b(f"💬 Чат: {ch['title'][:20] if ch else 'выбрать'}", "f:rlchat:0"))
    b.row(_b(f"🔨 Приз: {KIND_LABEL[cfg['kind']]}", "f:rlkind"),
          _b(f"⏰ {dur}", "f:rlmin"))
    b.row(_b(f"🎛 Режим: {MODE_LABEL[cfg['mode']]}", "f:rlmode"))
    if cfg["mode"] == "opt":
        b.row(_b(f"⏳ Сбор: {cfg['timer']} сек", "f:rltimer"))
    if cfg["chat_id"]:
        b.row(InlineKeyboardButton(text="🎲 Крутить!", callback_data="f:rlgo",
                                   style="danger"))
    b.row(_b("⬅️ Назад", "f:home"))
    return text, b.as_markup()


def _b(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


async def view_pick_chat(owner_id: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
    chats = await db.chats_for(owner_id)      # лог-чаты сюда не попадают
    per = 8
    pages = max(1, -(-len(chats) // per))
    page = max(0, min(page, pages - 1))
    b = InlineKeyboardBuilder()
    for c in chats[page * per:(page + 1) * per]:
        b.row(_b(f"{(c['title'] or c['chat_id'])}"[:40], f"f:rlset:{c['chat_id']}"))
    if pages > 1:
        b.row(_b("◀", f"f:rlchat:{(page - 1) % pages}"),
              _b(f"{page + 1}/{pages}", f"f:rlchat:{page}"),
              _b("▶", f"f:rlchat:{(page + 1) % pages}"))
    b.row(_b("⬅️ Назад", "f:rl"))
    return "<b>🎯 Где крутим?</b>\n\nВыберите чат.", b.as_markup()


# ---------- навигация ----------

@router.callback_query(F.data == "f:home")
async def cb_home(cb: CallbackQuery) -> None:
    if not _own(cb):
        return
    text, kb = await view_fun()
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data == "f:rl")
async def cb_roulette(cb: CallbackQuery) -> None:
    if not _own(cb):
        return
    text, kb = await view_roulette(cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("f:rlchat:"))
async def cb_pick_chat(cb: CallbackQuery) -> None:
    if not _own(cb):
        return
    text, kb = await view_pick_chat(cb.from_user.id, int(cb.data.split(":")[2]))
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("f:rlset:"))
async def cb_set_chat(cb: CallbackQuery) -> None:
    if not _own(cb):
        return
    cfg = await _cfg(cb.from_user.id)
    cfg["chat_id"] = int(cb.data.split(":")[2])
    await _save(cb.from_user.id, cfg)
    await cb_roulette(cb)


@router.callback_query(F.data == "f:rlkind")
async def cb_kind(cb: CallbackQuery) -> None:
    if not _own(cb):
        return
    cfg = await _cfg(cb.from_user.id)
    cfg["kind"] = "ban" if cfg["kind"] == "mute" else "mute"
    await _save(cb.from_user.id, cfg)
    await cb_roulette(cb)


@router.callback_query(F.data == "f:rlmin")
async def cb_minutes(cb: CallbackQuery) -> None:
    if not _own(cb):
        return
    cfg = await _cfg(cb.from_user.id)
    presets = list(config.MUTE_PRESETS)
    cur = presets.index(cfg["minutes"]) if cfg["minutes"] in presets else 0
    cfg["minutes"] = presets[(cur + 1) % len(presets)]
    await _save(cb.from_user.id, cfg)
    await cb_roulette(cb)


@router.callback_query(F.data == "f:rlmode")
async def cb_mode(cb: CallbackQuery) -> None:
    if not _own(cb):
        return
    cfg = await _cfg(cb.from_user.id)
    cfg["mode"] = "opt" if cfg["mode"] == "all" else "all"
    await _save(cb.from_user.id, cfg)
    await cb_roulette(cb)


@router.callback_query(F.data == "f:rltimer")
async def cb_timer(cb: CallbackQuery) -> None:
    if not _own(cb):
        return
    cfg = await _cfg(cb.from_user.id)
    cur = TIMER_PRESETS.index(cfg["timer"]) if cfg["timer"] in TIMER_PRESETS else 0
    cfg["timer"] = TIMER_PRESETS[(cur + 1) % len(TIMER_PRESETS)]
    await _save(cb.from_user.id, cfg)
    await cb_roulette(cb)


# ---------- сам розыгрыш ----------

async def _candidates(bot: Bot, chat_id: int) -> list[int]:
    """Кто может выиграть: писавшие за месяц, кроме админов и ботов."""
    admins = await adm_cache.chat_admin_ids(bot, chat_id)
    me = (await bot.me()).id
    out = []
    for uid in await db.active_writers(chat_id, ACTIVE_DAYS):
        if (uid in admins or uid == me or uid in config.ADMIN_IDS or uid < 0
                or uid in config.SERVICE_IDS):
            continue
        row = await db.get_user(uid)
        if row and row["banned"]:
            continue
        out.append(uid)
    return out


async def _in_chat(bot: Bot, chat_id: int, people: list[int]) -> list[int]:
    """Оставить из кандидатов тех, кто в чате состоит. Не больше двух.

    Статистика сообщений участником не делает: под постами привязанного канала
    пишут те, кто в группу не вступал, а кто-то из писавших давно ушёл. Барабан
    таких выбирал наравне со всеми — и приз уезжал человеку, которого в чате
    нет. Проверяем членство по одному в случайном порядке и останавливаемся,
    как только нашли двоих: первый — победитель (порядок случайный, так что
    выбор равномерный), второй нужен лишь чтобы понять, что он не один.

    Проверок не больше MEMBER_CHECKS: у Telegram на каждую уходит запрос.
    Если за них нашёлся ровно один — считаем, что он и правда один, и разыграем
    ему 50 на 50. Ошибиться в эту сторону мягче, чем забанить наверняка.
    """
    pool = list(people)
    random.shuffle(pool)
    found, checked = [], 0
    for uid in pool:
        if len(found) >= 2 or checked >= MEMBER_CHECKS:
            break
        checked += 1
        if await adm_cache.is_member(bot, chat_id, uid):
            found.append(uid)
    return found


async def _spin(bot: Bot, chat_id: int, msg_id: int, head: str) -> None:
    """Анимация «крутим барабан»: три точки бегают в конце строки."""
    frames = [".", "..", "..."]
    for i in range(SPIN_TICKS):
        try:
            await bot.edit_message_text(f"{head}{frames[i % 3]}",
                                        chat_id=chat_id, message_id=msg_id)
        except Exception:
            return                     # сообщение удалили — крутить нечего
        await asyncio.sleep(SPIN_STEP)


async def _cleanup(bot: Bot, chat_id: int, msg_id: int) -> None:
    await asyncio.sleep(CLEANUP_AFTER)
    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception:
        pass


HERO_TEXT = (
    "🎯 <b>Бан-рулетка</b>\n\n"
    "🦸 Единственный смельчак: {who}\n\n"
    "Барабан крутанулся, судьба посмотрела на этого отчаянного героя… и "
    "прошла мимо. Один против всех — и вышел сухим из воды.\n"
    "<i>Особенный. Молодец. Сообщение исчезнет через 15 минут.</i>"
)


async def _lone_hero(bot: Bot, chat_id: int, msg_id: int, who_id: int, cfg: dict,
                     by_id: int) -> None:
    """Участник ровно один: разыгрываем не «кто», а «повезёт ли» — 50 на 50."""
    if random.random() < 0.5:
        await _finish(bot, chat_id, msg_id, who_id, cfg, by_id)
        return
    try:
        await bot.edit_message_text(HERO_TEXT.format(who=await _mention(who_id)),
                                    chat_id=chat_id, message_id=msg_id,
                                    reply_markup=None)
    except Exception:
        logger.warning("рулетка: не вышло похвалить героя", exc_info=True)
    await db.add_event(chat_id, "manual", f"бан-рулетка: {who_id} уцелел | by {by_id}")
    asyncio.create_task(_cleanup(bot, chat_id, msg_id))


async def _mention(user_id: int) -> str:
    row = await db.get_user(user_id)
    return utils.mention(user_id, row["first_name"] if row else None,
                         row["username"] if row else None)


async def _finish(bot: Bot, chat_id: int, msg_id: int, winner: int, cfg: dict,
                  by_id: int) -> None:
    """Объявить победителя и выдать приз."""
    row = await db.get_user(winner)
    who = await _mention(winner)
    user = type("U", (), {"id": winner,
                          "username": row["username"] if row else None,
                          "full_name": (row["first_name"] if row else None) or str(winner)})()
    pid = await moderation.apply_punishment(
        bot, chat_id, user, cfg["kind"], cfg["minutes"], "победа в бан-рулетке", by_id,
    )
    dur = utils.fmt_minutes(cfg["minutes"]) if cfg["kind"] == "mute" else "навсегда"
    if pid is None:
        text = (f"🎯 <b>Бан-рулетка</b>\n\n🎉 Победитель: {who}\n"
                f"…но приз вручить не вышло — у бота не хватило прав. Повезло!")
    else:
        text = (f"🎯 <b>Бан-рулетка</b>\n\n🎉 Победитель: {who}\n"
                f"🎁 Приз: <b>{KIND_LABEL[cfg['kind']]}</b> · {dur}\n\n"
                f"<i>Поздравляем! Сообщение исчезнет через 15 минут.</i>")
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id,
                                    reply_markup=None)
    except Exception:
        logger.warning("рулетка: не вышло объявить победителя", exc_info=True)
    await db.add_event(chat_id, "manual",
                       f"бан-рулетка: {cfg['kind']} для {winner} | by {by_id}")
    asyncio.create_task(_cleanup(bot, chat_id, msg_id))


async def _run_all(bot: Bot, cfg: dict, by_id: int) -> str:
    """Режим «весь чат»: крутим сразу среди писавших."""
    chat_id = cfg["chat_id"]
    people = await _candidates(bot, chat_id)
    if not people:
        return "Некого разыгрывать: за месяц никто не писал."
    # членство проверяем до объявления: если играть не с кем, лучше промолчать
    members = await _in_chat(bot, chat_id, people)
    if not members:
        return ("Некого разыгрывать: из писавших за месяц в чате никого не "
                "осталось — это были комментаторы под постами или уже ушедшие.")
    head = "🎯 <b>Бан-рулетка!</b>\n\nБарабан крутится, судьба выбирает жертву"
    msg = await bot.send_message(chat_id, f"{head}.")
    await _spin(bot, chat_id, msg.message_id, head)
    if len(members) == 1:
        await _lone_hero(bot, chat_id, msg.message_id, members[0], cfg, by_id)
        return "Крутанул: участник был один, шанс 50 на 50."
    await _finish(bot, chat_id, msg.message_id, members[0], cfg, by_id)
    return "Крутанул среди участников чата."


async def _run_opt(bot: Bot, cfg: dict, by_id: int) -> str:
    """Режим «по кнопке»: сначала сбор добровольцев, потом розыгрыш."""
    chat_id, timer = cfg["chat_id"], cfg["timer"]
    b = InlineKeyboardBuilder()
    b.button(text="🎰 Участвовать", callback_data="f:join")
    head = ("🎯 <b>Бан-рулетка!</b>\n\nЖми кнопку, если чувствуешь удачу.\n"
            f"Приз — <b>{KIND_LABEL[cfg['kind']]}</b>.")
    msg = await bot.send_message(chat_id, f"{head}\n\n⏳ Сбор: {timer} сек\n👥 Смельчаков: 0",
                                 reply_markup=b.as_markup())
    key = (chat_id, msg.message_id)
    _joined[key] = set()
    asyncio.create_task(_collect(bot, key, cfg, by_id, head))
    return f"Сбор участников на {timer} сек запущен."


async def _collect(bot: Bot, key: tuple[int, int], cfg: dict, by_id: int,
                   head: str) -> None:
    """Обратный отсчёт, затем розыгрыш среди нажавших."""
    chat_id, msg_id = key
    left = cfg["timer"]
    step = 10 if left > 60 else 5
    b = InlineKeyboardBuilder()
    b.button(text="🎰 Участвовать", callback_data="f:join")
    while left > 0:
        await asyncio.sleep(min(step, left))
        left -= min(step, left)
        try:
            await bot.edit_message_text(
                f"{head}\n\n⏳ Сбор: {left} сек\n👥 Смельчаков: {len(_joined.get(key, ()))}",
                chat_id=chat_id, message_id=msg_id, reply_markup=b.as_markup(),
            )
        except Exception:
            pass          # текст не изменился или сообщение удалили — не страшно

    # кнопку жмут и комментаторы под постами: они в группе не состоят
    people = await _in_chat(bot, chat_id, list(_joined.pop(key, set())))
    if not people:
        try:
            await bot.edit_message_text(
                "🎯 <b>Бан-рулетка</b>\n\nНикто не рискнул. Скучно.",
                chat_id=chat_id, message_id=msg_id, reply_markup=None)
        except Exception:
            pass
        asyncio.create_task(_cleanup(bot, chat_id, msg_id))
        return
    spin_head = "🎯 <b>Бан-рулетка!</b>\n\nБарабан крутится, судьба выбирает жертву"
    await _spin(bot, chat_id, msg_id, spin_head)
    if len(people) == 1:
        await _lone_hero(bot, chat_id, msg_id, people[0], cfg, by_id)
        return
    await _finish(bot, chat_id, msg_id, random.choice(people), cfg, by_id)


@router.callback_query(F.data == "f:join")
async def cb_join(cb: CallbackQuery) -> None:
    """Кнопка «Участвовать» под сообщением рулетки — жать может кто угодно."""
    key = (cb.message.chat.id, cb.message.message_id)
    people = _joined.get(key)
    if people is None:
        await cb.answer("Розыгрыш уже закончился.", show_alert=True)
        return
    if cb.from_user.id in people:
        await cb.answer("Ты уже в игре. Удачи!")
        return
    if cb.from_user.id in await adm_cache.chat_admin_ids(cb.bot, key[0]):
        await cb.answer("Админам нельзя, это нечестно.", show_alert=True)
        return
    if not await adm_cache.is_member(cb.bot, key[0], cb.from_user.id):
        # пишущим под постами канала кнопка видна, но приз им вручить некуда
        await cb.answer("Ты в чате не состоишь — сначала вступи.", show_alert=True)
        return
    people.add(cb.from_user.id)
    await cb.answer("Ты в игре 🎰")


@router.callback_query(F.data == "f:rlgo")
async def cb_go(cb: CallbackQuery, bot: Bot) -> None:
    if not _own(cb):
        return
    cfg = await _cfg(cb.from_user.id)
    if not cfg["chat_id"]:
        await cb.answer("Сначала выберите чат.", show_alert=True)
        return
    await cb.answer("Поехали!")
    try:
        note = (await _run_opt(bot, cfg, cb.from_user.id) if cfg["mode"] == "opt"
                else await _run_all(bot, cfg, cb.from_user.id))
    except Exception as e:
        note = f"Не вышло: {utils.esc(str(e)[:80])}"
        logger.warning("рулетка упала", exc_info=True)
    text, kb = await view_roulette(cb.from_user.id)
    try:
        await cb.message.edit_text(f"{text}\n\n🎲 {utils.esc(note)}", reply_markup=kb)
    except Exception:
        pass
