"""Личное меню владельца чатов: настройка всех функций. Всё в одном сообщении."""
import asyncio
import logging
import re

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    Message, ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import config, db, schema, utils
from ..services import adm_cache, filters as flt, resolve, triggers

logger = logging.getLogger("gremlin.user_menu")

router = Router()
router.message.filter(F.chat.type == "private")


class Input(StatesGroup):
    wl_target = State()   # ждём id/@username для вайтлиста
    words = State()       # ждём стоп-слова
    welcome = State()     # ждём текст приветствия
    net_title = State()   # ждём название сетки
    trig_phrase = State() # ждём фразу триггера
    trig_reply = State()  # ждём ответ триггера (текст/медиа)
    pick_log = State()    # ждём выбор лог-чата (нативный пикер)
    access = State()      # ждём id/@username для доступа к боту
    cmd_name = State()      # ждём команду счётчика
    cmd_template = State()  # ждём заготовку ответа
    digest_to = State()     # ждём id получателя недельной сводки
    inline_wl = State()     # ждём @username разрешённого инлайн-бота
    link_wl = State()       # ждём чат/канал для вайтлиста ссылок
    trig_edit_phrase = State()  # ждём новую фразу триггера
    ans_new = State()           # ждём новый вариант ответа (триггер/счётчик)
    mass_unban = State()        # ждём список id для массового разбана
    mass_kick = State()         # ждём список id для массового кика
    mass_ban = State()          # ждём список id для массового бана


_HOME_TEXT = "<b>🧌 Gremlin</b>\n\nМодерация и мониторинг чатов."

# ---------- вьюхи (текст + клавиатура) ----------

_ADD_RIGHTS = "delete_messages+restrict_members+invite_users+pin_messages+manage_chat"

# имена системных команд бота — занимать их пользовательскими нельзя
_RESERVED_CMDS = {
    "mute", "мут", "ban", "бан", "warn", "варн", "пред", "unwarn", "снятьварн",
    "report", "репорт", "жалоба", "unmute", "размут", "unban", "разбан",
}


async def view_home(user_id: int, bot: Bot) -> tuple[str, InlineKeyboardMarkup]:
    """Единое меню — одинаковое для всех допущенных."""
    b = InlineKeyboardBuilder()
    b.button(text="💬 Чаты", callback_data="u:chats")
    b.button(text="🕸 Сетки чатов", callback_data="u:netsh")
    if user_id in config.ADMIN_IDS:
        # служебные разделы и управление доступом — только владельцу бота
        b.button(text="📜 Лог событий", callback_data="a:log")
        b.button(text="🐞 Ошибки", callback_data="a:errors")
        b.button(text="⚙️ Состояние", callback_data="a:health")
        b.button(text="👥 Доступ к боту", callback_data="u:acc")
        b.button(text="🎪 Приколы", callback_data="f:home")
    b.button(text="✖️ Закрыть", callback_data="u:close")
    b.adjust(1, 1, 2, 1, 1, 1, 1)
    return _HOME_TEXT, b.as_markup()


CHATS_PER_PAGE = 5


async def view_chats(bot: Bot, viewer_id: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """Чаты, доступные этому человеку.

    Владелец бота видит все, остальные — только свои. Лог-чаты сюда не попадают:
    они настраиваются внутри того чата, которому служат логом.
    """
    me = await bot.me()
    chats = await db.chats_for(viewer_id)
    pages = max(1, -(-len(chats) // CHATS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    chunk = chats[page * CHATS_PER_PAGE:(page + 1) * CHATS_PER_PAGE]

    b = InlineKeyboardBuilder()
    if chats:
        text = f"<b>💬 Чаты</b> ({len(chats)})"
        if pages > 1:
            text += f" · страница {page + 1} из {pages}"
        text += "\n\nВыберите чат — откроются его данные и настройки."
        for c in chunk:
            title = c["title"] or str(c["chat_id"])
            # у обсуждений название канала важнее собственного: чатов может быть
            # несколько, и по их именам не понять, к чему они прицеплены
            _, _, linked = await adm_cache.linked_chat(bot, c["chat_id"])
            label = f"{title} · 📣 {linked}" if linked else title
            b.row(_btn(label[:60], f"u:c:{c['chat_id']}"))
    else:
        text = ("<b>💬 Чаты</b>\n\nПока пусто. Добавьте бота администратором в свой "
                "чат — он появится здесь.")
    if pages > 1:
        prev_p = page - 1 if page else pages - 1
        next_p = page + 1 if page + 1 < pages else 0
        b.row(_btn("◀", f"u:chats:{prev_p}"),
              _btn(f"{page + 1}/{pages}", f"u:chats:{page}"),
              _btn("▶", f"u:chats:{next_p}"))
    b.row(InlineKeyboardButton(
        text="➕ Добавить в чат",
        url=f"https://t.me/{me.username}?startgroup=true&admin={_ADD_RIGHTS}",
    ))
    if viewer_id in config.ADMIN_IDS:
        gl = await db.global_log()
        gl_chat = await db.get_chat(gl) if gl else None
        gl_name = (gl_chat["title"] if gl_chat and gl_chat["title"]
                   else (str(gl) if gl else "не задан"))
        b.row(_btn(f"🌍 Глобальный лог: {gl_name}", "u:glog"))
    b.row(_btn("⬅️ Назад", "u:home"))
    return text, b.as_markup()


async def view_access() -> tuple[str, InlineKeyboardMarkup]:
    """Кому разрешено пользоваться ботом (только для владельца бота)."""
    rows = await db.access_list()
    b = InlineKeyboardBuilder()
    text = (
        "<b>👥 Доступ к боту</b>\n\n"
        "Здесь список тех, кому разрешено настраивать боты и чаты. "
        "Добавляйте по числовому id или @username.\n"
        f"Записей: <b>{len(rows)}</b>"
    )
    b.button(text="➕ Добавить", callback_data="u:acca")
    for r in rows:
        who = await db.user_label(r["user_id"], r["username"])
        b.button(text=f"❌ {who}", callback_data=f"u:accd:{r['id']}")
    b.button(text="⬅️ Назад", callback_data="u:home")
    b.adjust(1)
    return text, b.as_markup()


async def _log_chat_label(log_chat_id: int | None) -> str:
    """«Название (id)» — название берём из базы чатов, если бот там же."""
    if not log_chat_id:
        return "<b>не задан</b>"
    ch = await db.get_chat(log_chat_id)
    if ch and ch["title"]:
        return f"{utils.esc(ch['title'])} (<code>{log_chat_id}</code>)"
    return f"<code>{log_chat_id}</code>"


def setup_key(cid: int) -> str:
    return f"setup_done:{cid}"


async def needs_setup(cid: int, viewer_id: int) -> bool:
    """Чат ещё не настраивали, и человеку есть откуда перенести настройки."""
    if await db.kv_get(setup_key(cid)):
        return False
    return any(c["chat_id"] != cid for c in await db.chats_for(viewer_id))


async def view_setup(cid: int) -> tuple[str, InlineKeyboardMarkup]:
    """Развилка для свежего чата: перенести настройки или начать с нуля."""
    ch = await db.get_chat(cid)
    text = (
        f"<b>🆕 {utils.esc(ch['title'] if ch else str(cid))}</b>\n\n"
        "Чат добавлен, но ещё не настроен. Можно перенести правила из другого "
        "чата — фильтры, стоп-слова, вайтлисты, триггеры и счётчики поедут "
        "целиком, вместе с медиа.\n\n"
        "Не переносятся: получатель недельной сводки и счёт вызовов у команд."
    )
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="📥 Перенести настройки",
                               callback_data=f"u:cp:{cid}", style="success"))
    b.row(InlineKeyboardButton(text="🛠 Настроить с нуля",
                               callback_data=f"u:cpn:{cid}", style="danger"))
    b.row(_btn("⬅️ Назад", "u:chats"))
    return text, b.as_markup()


async def view_copy_from(cid: int, viewer_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Выбор чата-источника. Только свои чаты: иначе через перенос можно было бы
    вытащить чужие стоп-слова, вайтлист и триггеры."""
    others = [c for c in await db.chats_for(viewer_id) if c["chat_id"] != cid]
    b = InlineKeyboardBuilder()
    text = "<b>📥 Откуда перенести</b>\n\nВыберите чат — его настройки скопируются сюда."
    if not others:
        text = "<b>📥 Откуда перенести</b>\n\nПока неоткуда: других чатов у бота нет."
    for c in others:
        b.row(_btn(c["title"] or str(c["chat_id"]), f"u:cps:{cid}:{c['chat_id']}"))
    b.row(_btn("⬅️ Назад", f"u:c:{cid}"))
    return text, b.as_markup()


async def view_copy_pick(cid: int, src: int, picked: set[str]) -> tuple[str, InlineKeyboardMarkup]:
    """Галочки: какие разделы переносим."""
    from ..services import transfer
    ch = await db.get_chat(src)
    text = (
        f"<b>📥 Перенос из «{utils.esc(ch['title'] if ch else str(src))}»</b>\n\n"
        "Отметьте, что перенести. Вместе с настройками едут и списки раздела: "
        "стоп-слова, вайтлист, разрешённые чаты и боты, триггеры с медиа, счётчики.\n"
        f"Выбрано: <b>{len(picked)}</b> из {len(transfer.ALL_GROUPS)}"
    )
    b = InlineKeyboardBuilder()
    row = []
    for key in transfer.ALL_GROUPS:
        mark = "✅" if key in picked else "☐"
        row.append(_btn(f"{mark} {transfer.GROUPS[key][0]}", f"u:cpg:{cid}:{src}:{key}"))
        if len(row) == 2:
            b.row(*row)
            row = []
    if row:
        b.row(*row)
    b.row(_btn("Отметить все", f"u:cpg:{cid}:{src}:__all"),
          _btn("Снять все", f"u:cpg:{cid}:{src}:__none"))
    if picked:
        b.row(InlineKeyboardButton(text="✅ Подтвердить",
                                   callback_data=f"u:cpd:{cid}:{src}", style="success"))
    b.row(_btn("⬅️ Назад", f"u:cp:{cid}"))
    return text, b.as_markup()


async def view_chat(cid: int, viewer_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Мини-дашборд чата: данные + сводка настроек + кнопки разделов."""
    ch = await db.get_chat(cid)
    s = await db.get_settings(cid)
    st = await db.chat_stats(cid)
    pun = await db.active_punishments_count(cid)
    marks = [f"{'✅' if getattr(s, key) else '🚫'} {lbl}" for key, lbl in schema.OVERVIEW]
    half = (len(marks) + 1) // 2
    owner_line = ""
    if viewer_id in config.ADMIN_IDS and ch and ch["owner_id"]:
        # чужие чаты в списке видит только владелец бота — подскажем, чей это
        owner_line = f"👤 Владелец: {utils.esc(await db.user_handle(ch['owner_id']))}\n"
    text = (
        f"<b>⚙️ {utils.esc(ch['title'] if ch else str(cid))}</b>\n"
        f"<code>{cid}</code>\n"
        f"{owner_line}\n"
        f"💬 Сообщений: сегодня <b>{st['d1']}</b> · за 7д <b>{st['d7']}</b>\n"
        f"👥 За 7д: пришло <b>{st['joins']}</b> · ушло <b>{st['leaves']}</b>\n"
        f"🔨 Наказаний: активных <b>{pun}</b> · за 7д <b>{st['pun7']}</b>\n"
        f"🪪 Лог-чат: {await _log_chat_label(s.log_chat_id)}\n\n"
        f"{' · '.join(marks[:half])}\n{' · '.join(marks[half:])}"
    )
    b = InlineKeyboardBuilder()
    b.button(text="🤖 Инлайн-боты", callback_data=f"u:s:{cid}:inline")
    b.button(text="🔗 Ссылки", callback_data=f"u:s:{cid}:links")
    b.button(text="📛 Анонимы", callback_data=f"u:s:{cid}:anon")
    b.button(text="🧨 Стоп-слова", callback_data=f"u:s:{cid}:words")
    b.button(text="🌊 Антифлуд", callback_data=f"u:s:{cid}:flood")
    b.button(text="🤖 Капча", callback_data=f"u:s:{cid}:captcha")
    b.button(text="👁 Наблюдение", callback_data=f"u:s:{cid}:watch")
    b.button(text="👋 Приветствие", callback_data=f"u:s:{cid}:welcome")
    b.button(text="🖼 Медиа-фильтры", callback_data=f"u:s:{cid}:media")
    b.button(text="🎯 Триггеры", callback_data=f"u:s:{cid}:triggers")
    b.button(text="🔢 Счётчики", callback_data=f"u:s:{cid}:cmds")
    from ..services import digest as _dg
    if _dg.tracked_chat() == cid:          # подробная статистика — только этот чат
        b.button(text="📊 Недельная сводка", callback_data=f"u:s:{cid}:digest")
    b.button(text="🎪 Приколы", callback_data=f"u:games:{cid}")
    b.button(text="🎖 Доверие", callback_data=f"u:s:{cid}:trust")
    b.button(text="⚠️ Варны", callback_data=f"u:s:{cid}:warns")
    b.button(text="📜 Правила в постах", callback_data=f"u:s:{cid}:rules")
    b.button(text="🧹 Системные", callback_data=f"u:s:{cid}:service")
    b.button(text="🕊 Вайтлист", callback_data=f"u:s:{cid}:wl")
    b.button(text="🪪 Карточки и лог", callback_data=f"u:s:{cid}:cards")
    b.button(text="🚫 Наказания", callback_data=f"u:p:{cid}:0")
    b.button(text="📈 Статистика", callback_data=f"u:st:{cid}")
    b.button(text="📜 Лог чата", callback_data=f"a:clog:{cid}")
    log_ch = await db.get_chat(s.log_chat_id) if s.log_chat_id else None
    log_name = (log_ch["title"] if log_ch and log_ch["title"]
                else (str(s.log_chat_id) if s.log_chat_id else "не задан"))
    b.button(text=f"📍 Лог-чат: {log_name}", callback_data=f"u:logsel:{cid}")
    net = await db.net_of_chat(cid)
    b.button(text=f"🕸 Сетка: {net['title'][:18] if net else 'нет'}",
             callback_data=f"u:netc:{cid}")
    b.button(text="📥 Перенести настройки", callback_data=f"u:cp:{cid}")
    b.button(text="🚪 Убрать бота из чата", callback_data=f"a:leave:{cid}")
    b.button(text="⬅️ Назад", callback_data="u:chats")
    b.adjust(2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 1)
    return text, b.as_markup()


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


# ---------- ввод внутри одного сообщения ----------
#
# Все «добавь что-нибудь» работают одинаково: вопрос подменяет текст текущего меню,
# ответ юзера удаляется, на его месте снова меню. Переписка не растёт.

async def _ask(cb: CallbackQuery, state: FSMContext, st, prompt: str,
               back: str, **data) -> None:
    """Задать вопрос прямо в открытом меню и запомнить, какое сообщение править."""
    await state.set_state(st)
    await state.update_data(msg_id=cb.message.message_id, back=back, **data)
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Отмена", callback_data=back)
    await cb.message.edit_text(prompt, reply_markup=b.as_markup())
    await cb.answer()


async def _edit_menu(message: Message, bot: Bot, state: FSMContext,
                     text: str, kb: InlineKeyboardMarkup | None) -> None:
    """Убрать сообщение юзера и перерисовать меню на прежнем месте."""
    data = await state.get_data()
    msg_id = data.get("msg_id")
    try:
        await message.delete()
    except Exception:
        pass
    if msg_id:
        try:
            await bot.edit_message_text(
                text, chat_id=message.chat.id, message_id=msg_id, reply_markup=kb
            )
            return
        except Exception:
            # сообщение удалили/устарело — покажем новым
            logger.warning("edit menu %s failed", msg_id, exc_info=True)
    # дальше правим уже это новое сообщение, иначе следующий шаг снова
    # промахнётся мимо старого и в чате останется лишняя простыня
    sent = await message.answer(text, reply_markup=kb)
    await state.update_data(msg_id=sent.message_id)


async def _retry(message: Message, bot: Bot, state: FSMContext, prompt: str) -> None:
    """Ввод не подошёл — оставляем вопрос на месте с пометкой."""
    data = await state.get_data()
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Отмена", callback_data=data.get("back", "u:home"))
    await _edit_menu(message, bot, state, prompt, b.as_markup())


async def _done(message: Message, bot: Bot, state: FSMContext,
                view: tuple[str, InlineKeyboardMarkup], note: str = "") -> None:
    """Завершить ввод: показать раздел на месте и сбросить состояние."""
    await _edit_menu(message, bot, state, note + view[0], view[1])
    await state.clear()


def _digest_state() -> str:
    """Строка о состоянии базы статистики — сколько участников и когда обновляли."""
    from ..services import digest
    d = digest.collect(config.STATS_DB)
    if d is None:
        return "\n\n⚠️ База статистики не найдена."
    full = d.get("days", 7) >= 7
    silent_label = "молчали всю неделю" if full else "пока не писали на этой неделе"
    return (
        f"\n\n👥 Участников сейчас: <b>{d['members']}</b> · "
        f"{silent_label}: <b>{len(d['silent'])}</b>\n"
        f"<i>неделя {d.get('period', '—')} · данные обновлены: {d['updated']}</i>"
    )


async def view_section(cid: int, sec: str) -> tuple[str, InlineKeyboardMarkup]:
    section = schema.SECTION_BY_KEY.get(sec)
    if section is None:
        b = InlineKeyboardBuilder()
        b.row(_btn("⬅️ Назад", f"u:c:{cid}"))
        return f"Неизвестный раздел: {sec}", b.as_markup()

    s = await db.get_settings(cid)
    ch = await db.get_chat(cid)
    title = utils.esc(ch["title"] if ch else str(cid))
    b = InlineKeyboardBuilder()

    # --- заголовок + пояснение + статусные строки из схемы ---
    lines = [f"<b>{section.title}</b> · {title}\n", section.intro, ""]
    for f in section.fields:
        if not schema.visible(f, s):
            continue
        lines.append(f"{f.label}: <b>{schema.value_label(f, getattr(s, f.key))}</b>")
    text = "\n".join(lines).rstrip()
    if sec == "digest":
        text += await asyncio.to_thread(_digest_state)

    # --- кнопки полей: тумблеры отдельными рядами, селекторы рядом ◀ знач ▶ ---
    for f in section.fields:
        if not schema.visible(f, s):
            continue
        if f.kind == "toggle":
            b.row(_btn(f"{schema.value_label(f, getattr(s, f.key))} · {f.label}",
                       f"u:t:{cid}:{f.key}"))
        else:
            b.row(
                _btn("◀", f"u:y:{cid}:{f.key}:-"),
                _btn(f"{f.label}: {schema.value_label(f, getattr(s, f.key))}",
                     f"u:y:{cid}:{f.key}:+"),
                _btn("▶", f"u:y:{cid}:{f.key}:+"),
            )

    # --- кастомные виджеты (списки / выбор лог-чата / биты карточек) ---
    for w in section.widgets:
        await _render_widget(b, cid, w, s)

    # back: ключ другого раздела либо готовый callback-шаблон с {cid}
    if not section.back:
        back = f"u:c:{cid}"
    elif section.back.startswith("u:"):
        back = section.back.format(cid=cid)
    else:
        back = f"u:s:{cid}:{section.back}"
    b.row(_btn("⬅️ Назад", back))
    return text, b.as_markup()


async def _render_widget(b: InlineKeyboardBuilder, cid: int, widget: str, s) -> None:
    """Списочные части разделов, которые не сводятся к простому полю."""
    if widget == "anon":
        allowed = [r for r in await db.wl_list(cid) if r["scope"] in ("all", "anon")]
        b.row(_btn(f"🕊 Разрешённые отправители: {len(allowed)}", f"u:s:{cid}:wl"))

    elif widget == "links_pun":
        b.row(_btn("⚖️ Наказания для участников", f"u:s:{cid}:links_member"))
        b.row(_btn("⚖️ Наказания для не участников", f"u:s:{cid}:links_guest"))

    elif widget == "link_wl":
        n = len(await db.link_wl_list(cid))
        b.row(_btn(f"🔓 Разрешённые чаты и каналы: {n}", f"u:lw:{cid}"))

    elif widget == "inline_wl":
        b.row(_btn("➕ Разрешить бота", f"u:ila:{cid}"))
        for r in await db.inline_wl_list(cid):
            b.row(_btn(f"❌ @{r['username']}", f"u:ild:{cid}:{r['id']}"))

    elif widget == "words":
        n = len(await db.words_list(cid))
        b.row(_btn(f"📝 Список слов: {n}", f"u:wd:{cid}:0"))
        b.row(_btn("➕ Добавить слова", f"u:wda:{cid}"))

    elif widget == "wl":
        b.row(_btn("➕ Добавить", f"u:wla:{cid}"))
        for e in await db.wl_entries(cid):
            who = e["title"] or await db.user_label(e["user_id"], e["username"])
            b.row(_btn(f"👤 {who} · {_wl_scopes_label(e['scopes'])}",
                       f"u:wle:{cid}:{e['row_id']}"))

    elif widget == "logsel":
        log_str = str(s.log_chat_id) if s.log_chat_id else "не задан"
        b.row(_btn(f"📍 Лог-чат: {log_str}", f"u:logsel:{cid}"))

    elif widget == "cardbits":
        row = []
        for bit, label in config.CARD_BITS:
            on = bool(s.card_mask & bit)
            row.append(_btn(f"{'✅' if on else '🚫'} {label}", f"u:cb:{cid}:{bit}"))
            if len(row) == 2:
                b.row(*row)
                row = []
        if row:
            b.row(*row)

    elif widget == "welcome_text":
        mark = "задано" if s.welcome_text else "не задано"
        b.row(_btn(f"✏️ Текст приветствия ({mark})", f"u:wtxt:{cid}"))

    elif widget == "trustsoft":
        n = sum(1 for bit, _ in config.TRUST_BITS if s.trust_mask & bit)
        b.row(_btn(f"🎚 Что смягчать: {n} из {len(config.TRUST_BITS)}",
                   f"u:s:{cid}:trust_soft"))

    elif widget == "trustbits":
        row = []
        for bit, label in config.TRUST_BITS:
            on = bool(s.trust_mask & bit)
            row.append(_btn(f"{'✅' if on else '🚫'} {label}", f"u:tb:{cid}:{bit}"))
            if len(row) == 2:
                b.row(*row)
                row = []
        if row:
            b.row(*row)

    elif widget == "warnlist":
        n = len(await db.warn_users(cid))
        b.row(_btn(f"📋 Кто с варнами: {n}", f"u:wn:{cid}:0"))

    elif widget == "rules_text":
        rows = await db.ans_list("rules", cid)
        b.row(_btn(f"✏️ Заготовки: {len(rows)}", f"u:an:{cid}:r:{cid}:0"))

    elif widget == "digest_to":
        who = await db.user_label(s.digest_to) if s.digest_to else "не задан"
        b.row(_btn(f"👤 Получатель: {who}", f"u:dig:{cid}"))
        if s.digest_to:
            b.row(_btn("📤 Обновить сводку сейчас", f"u:dignow:{cid}"))
            b.row(_btn("🚫 Убрать получателя", f"u:digoff:{cid}"))

    elif widget == "mediabits":
        row = []
        for bit, _key, label in config.MEDIA_BITS:
            on = bool(s.media_mask & bit)
            row.append(_btn(f"{'🗑' if on else '▫️'} {label}", f"u:mb:{cid}:{bit}"))
            if len(row) == 2:
                b.row(*row)
                row = []
        if row:
            b.row(*row)

    elif widget == "trigs":
        n = len(await db.trig_list(cid))
        b.row(_btn(f"📋 Список триггеров: {n}", f"u:tgl:{cid}:0"))
        b.row(_btn("➕ Добавить триггер", f"u:tga:{cid}"))

    elif widget == "cmds":
        n = len(await db.cmd_list(cid))
        b.row(_btn(f"📋 Список счётчиков: {n}", f"u:cml:{cid}:0"))
        b.row(_btn("➕ Добавить счётчик", f"u:cma:{cid}"))


# уровни вайтлиста без «полного игнора» — он тумблер над всеми остальными
WL_PARTS = tuple(s for s in config.WL_SCOPES if s != "all")


def _wl_effective(scopes: set[str]) -> set[str]:
    """Что реально отмечено галочками: «полный игнор» зажигает все."""
    return set(WL_PARTS) if "all" in scopes else {s for s in scopes if s in WL_PARTS}


def _wl_scopes_label(scopes: set[str]) -> str:
    if "all" in scopes:
        return "полный игнор"
    on = _wl_effective(scopes)
    if len(on) == 1:
        return config.WL_SCOPE_LABELS[next(iter(on))]
    return f"{len(on)} из {len(WL_PARTS)}"


def _wl_pack(on: set[str]) -> set[str]:
    """Набор галочек -> строки в базе. Все отмечены — храним одним 'all'."""
    if on >= set(WL_PARTS):
        return {"all"}
    return set(on)


async def view_wl_entry(cid: int, row_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    """Карточка записи вайтлиста: галочками отмечаем, что для неё не проверять."""
    e = await db.wl_entry(cid, row_id)
    if e is None:
        return None
    who = e["title"] or await db.user_label(e["user_id"], e["username"])
    ident = f"<code>{e['user_id']}</code>" if e["user_id"] else f"@{utils.esc(e['username'])}"
    on = _wl_effective(e["scopes"])
    text = (
        f"<b>🕊 {utils.esc(who)}</b> · {ident}\n\n"
        "Отмеченное для него не проверяется. «Полный игнор» включает всё сразу; "
        "снимите с него галочку у любого пункта — останутся только выбранные.\n"
        f"Сейчас: <b>{_wl_scopes_label(e['scopes'])}</b>"
    )
    b = InlineKeyboardBuilder()
    b.row(_btn(f"{'✅' if 'all' in e['scopes'] else '☐'} {config.WL_SCOPE_LABELS['all']}",
               f"u:wlt:{cid}:{row_id}:all"))
    row = []
    for scope in WL_PARTS:
        mark = "✅" if scope in on else "☐"
        row.append(_btn(f"{mark} {config.WL_SCOPE_LABELS[scope]}", f"u:wlt:{cid}:{row_id}:{scope}"))
        if len(row) == 2:
            b.row(*row)
            row = []
    if row:
        b.row(*row)
    b.row(_btn("🗑 Убрать из вайтлиста", f"u:wld:{cid}:{row_id}"))
    b.row(_btn("⬅️ Назад", f"u:s:{cid}:wl"))
    return text, b.as_markup()


# ---------- варианты ответов (триггеры и счётчики) ----------
#
# Ответов у объекта может быть несколько, бот берёт случайный. Владельца в
# callback пишем одной буквой: t — триггер, c — счётчик (лимит 64 байта).

ANS_OWNER = {"t": "trig", "c": "cmd", "r": "rules"}
ANS_LIMIT = 60   # ответов бывает много: списки-рулетки вроде !судимости


_TAGS = re.compile(r"<[^>]+>")


def _plain(text: str | None) -> str:
    """Текст без разметки — для превью в меню: сырые теги там только мешают."""
    return utils.esc(_TAGS.sub("", text or ""))


def _ans_line(a) -> str:
    """Одна строка варианта в человеческом виде."""
    if a["file_path"]:
        s = f"🖼 медиа ({a['media_type']})"
        return s + (f" · подпись: <code>{_plain(a['text'])}</code>" if a["text"] else "")
    return f"💬 <code>{_plain(a['text'])}</code>"


def _ans_preview(answers: list) -> str:
    if not answers:
        return "<i>пусто — бот промолчит</i>"
    if len(answers) == 1:
        return _ans_line(answers[0])
    n = len(answers)
    word = "варианта" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "вариантов"
    return f"<b>{n}</b> {word}, бот берёт случайный"


def _ans_back(cid: int, code: str, oid: int) -> str:
    if code == "t":
        return f"u:tgv:{cid}:{oid}"
    if code == "r":
        return f"u:s:{cid}:rules"
    return f"u:cmv:{cid}:{oid}"


async def view_answers(cid: int, code: str, oid: int,
                       page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    owner = ANS_OWNER[code]
    rows = await db.ans_list(owner, oid)
    chunk, page, pages = _page_slice(rows, page)
    title = {"t": "🎯 Триггер", "r": "📜 Правила"}.get(code, "🔢 Счётчик")
    lines = [
        f"<b>{title} · варианты ответа</b>\n",
        "Вариантов несколько — бот отвечает случайным. "
        + ("Можно текст, медиа или медиа с подписью."
           if code in ("t", "r") else "Только текст: число в скобках дописывается само."),
        f"\nВсего: <b>{len(rows)}</b> из {ANS_LIMIT}"
        + (f" · страница {page + 1} из {pages}" if pages > 1 else ""),
        "",
    ]
    if not rows:
        lines.append("Пусто — добавьте хотя бы один, иначе бот не ответит.")
    start = page * LIST_PER_PAGE
    for i, a in enumerate(chunk, start + 1):
        lines.append(f"{i}. {_ans_line(a)}")

    b = InlineKeyboardBuilder()
    row = []
    for i, a in enumerate(chunk, start + 1):
        row.append(_btn(f"❌ {i}", f"u:and:{cid}:{code}:{oid}:{a['id']}"))
        if len(row) == 4:
            b.row(*row)
            row = []
    if row:
        b.row(*row)
    if pages > 1:                      # у вариантов свой префикс, общий _pager не подходит
        prev_p = page - 1 if page else pages - 1
        next_p = page + 1 if page + 1 < pages else 0
        b.row(
            _btn("◀", f"u:an:{cid}:{code}:{oid}:{prev_p}"),
            _btn(f"{page + 1}/{pages}", f"u:an:{cid}:{code}:{oid}:{page}"),
            _btn("▶", f"u:an:{cid}:{code}:{oid}:{next_p}"),
        )
    b.row(_btn("➕ Добавить вариант", f"u:ana:{cid}:{code}:{oid}"))
    b.row(_btn("⬅️ Назад", _ans_back(cid, code, oid)))
    return "\n".join(lines), b.as_markup()


LIST_PER_PAGE = 10


def _pager(b: InlineKeyboardBuilder, cid: int, prefix: str, page: int, pages: int) -> None:
    """Ряд навигации ◀ 2/5 ▶ по кругу. Одна страница — ряда нет."""
    if pages < 2:
        return
    prev_p = page - 1 if page else pages - 1
    next_p = page + 1 if page + 1 < pages else 0
    b.row(
        _btn("◀", f"{prefix}:{cid}:{prev_p}"),
        _btn(f"{page + 1}/{pages}", f"{prefix}:{cid}:{page}"),
        _btn("▶", f"{prefix}:{cid}:{next_p}"),
    )


def _page_slice(rows: list, page: int) -> tuple[list, int, int]:
    pages = max(1, -(-len(rows) // LIST_PER_PAGE))
    page = max(0, min(page, pages - 1))
    start = page * LIST_PER_PAGE
    return rows[start:start + LIST_PER_PAGE], page, pages


async def view_cmds(cid: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """Счётчики отдельной страницей: в разделе их лимит 30, все кнопки не влезали."""
    rows = await db.cmd_list(cid)
    chunk, page, pages = _page_slice(rows, page)
    head = f"<b>🔢 Счётчики</b>\n\nВсего: <b>{len(rows)}</b> из {config.CMD_LIMIT}"
    if pages > 1:
        head += f" · страница {page + 1} из {pages}"
    lines = [head, "", "Нажмите на счётчик, чтобы посмотреть и настроить его."]
    if not rows:
        lines.append("\nПока ни одного.")
    b = InlineKeyboardBuilder()
    row = []
    for r in chunk:
        row.append(_btn(f"{r['cmd'][:16]} [{r['count']}]", f"u:cmv:{cid}:{r['id']}"))
        if len(row) == 2:
            b.row(*row)
            row = []
    if row:
        b.row(*row)
    _pager(b, cid, "u:cml", page, pages)
    b.row(_btn("➕ Добавить счётчик", f"u:cma:{cid}"))
    b.row(_btn("⬅️ Назад", f"u:s:{cid}:cmds"))
    return "\n".join(lines), b.as_markup()


async def view_trigs(cid: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    rows = await db.trig_list(cid)
    chunk, page, pages = _page_slice(rows, page)
    head = f"<b>🎯 Триггеры</b>\n\nВсего: <b>{len(rows)}</b> из {config.TRIG_LIMIT}"
    if pages > 1:
        head += f" · страница {page + 1} из {pages}"
    lines = [head, "", "💬 текст · 🖼 медиа · 🎲 несколько вариантов. Нажмите, чтобы настроить."]
    if not rows:
        lines.append("\nПока ни одного.")
    b = InlineKeyboardBuilder()
    stats = await db.ans_stats("trig", [r["id"] for r in chunk])
    row = []
    for r in chunk:
        total, media = stats.get(r["id"], (0, 0))
        # 🎲 — несколько вариантов, дальше по содержимому единственного
        kind = "🎲" if total > 1 else ("🖼" if media else "💬")
        row.append(_btn(f"{kind} {r['phrase'][:18]}", f"u:tgv:{cid}:{r['id']}"))
        if len(row) == 2:
            b.row(*row)
            row = []
    if row:
        b.row(*row)
    _pager(b, cid, "u:tgl", page, pages)
    b.row(_btn("➕ Добавить триггер", f"u:tga:{cid}"))
    b.row(_btn("⬅️ Назад", f"u:s:{cid}:triggers"))
    return "\n".join(lines), b.as_markup()


WORDS_PER_PAGE = 12


def _word_label(word: str, mode: str) -> str:
    return f"{word}{'*' if mode == 'stem' else ''}"


async def view_words(cid: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """Стоп-слова отдельной страницей: в разделе список не помещался.

    Слова показываем текстом (там видно целиком), кнопки — только для удаления,
    по номеру из этого же списка.
    """
    rows = await db.words_list(cid)
    pages = max(1, -(-len(rows) // WORDS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    start = page * WORDS_PER_PAGE
    chunk = rows[start:start + WORDS_PER_PAGE]

    lines = [
        "<b>🧨 Список стоп-слов</b>\n",
        "Слово со звёздочкой ловит любые окончания. Кнопка с номером удаляет слово.",
        f"\nВсего: <b>{len(rows)}</b>" + (f" · страница {page + 1} из {pages}" if pages > 1 else ""),
        "",
    ]
    b = InlineKeyboardBuilder()
    if not rows:
        lines.append("Пусто — ни одного слова.")
    for i, r in enumerate(chunk, start + 1):
        lines.append(f"{i}. <code>{utils.esc(_word_label(r['word'], r['mode']))}</code>")

    row = []
    for i, r in enumerate(chunk, start + 1):
        label = _word_label(r["word"], r["mode"])
        row.append(_btn(f"❌ {i}. {label[:18]}", f"u:wdd:{cid}:{page}:{r['id']}"))
        if len(row) == 2:
            b.row(*row)
            row = []
    if row:
        b.row(*row)

    if pages > 1:
        nav = [
            _btn("⬅️", f"u:wd:{cid}:{page - 1}" if page else f"u:wd:{cid}:{pages - 1}"),
            _btn(f"{page + 1}/{pages}", f"u:wd:{cid}:{page}"),
            _btn("➡️", f"u:wd:{cid}:{page + 1}" if page + 1 < pages else f"u:wd:{cid}:0"),
        ]
        b.row(*nav)
    b.row(_btn("➕ Добавить слова", f"u:wda:{cid}"))
    if rows:
        b.row(_btn("🗑 Очистить список", f"u:wdc:{cid}"))
    b.row(_btn("⬅️ Назад", f"u:s:{cid}:words"))
    return "\n".join(lines), b.as_markup()


async def view_games(cid: int) -> tuple[str, InlineKeyboardMarkup]:
    """Игры чата: каждую можно включить и решить, кому она доступна."""
    s = await db.get_settings(cid)
    lines = [
        "<b>🎪 Приколы</b>\n",
        "Игры для этого чата. Наказания настоящие — снимаются как обычные, "
        "в разделе «🚫 Наказания». Админов и бота игры не трогают, а итоговое "
        "сообщение партии само исчезает через 10 минут.\n",
        "Левая кнопка включает игру, правая решает, кому её можно звать: "
        "<b>всем</b> или <b>только админам</b>.\n",
    ]
    b = InlineKeyboardBuilder()
    for bit, label, how, about in config.GAME_BITS:
        on = bool(s.games_on & bit)
        adm = bool(s.games_adm & bit)
        # у титулов команды нет, бот шлёт их сам — выбирать «кому можно» нечего
        by_hand = bit != config.GAME_TITLES
        prize_line = ""
        if by_hand:
            kind = getattr(s, config.GAME_FIELDS[bit][0])
            minutes = getattr(s, config.GAME_FIELDS[bit][1])
            prize_line = ("бан" if kind == "ban"
                          else f"мут на {utils.fmt_minutes(minutes)}")
            prize_line = f" · приз: <b>{prize_line}</b>"
        lines.append(
            f"{'✅' if on else '🚫'} <b>{label}</b> · <code>{how}</code>"
            + (" · только админы" if on and adm and by_hand else "")
            + prize_line
            + f"\n<i>{about}</i>\n"
        )
        toggle = _btn(f"{'✅' if on else '🚫'} {label}", f"u:gb:{cid}:{bit}")
        if by_hand:
            kind, minutes = getattr(s, config.GAME_FIELDS[bit][0]), \
                getattr(s, config.GAME_FIELDS[bit][1])
            prize = "бан" if kind == "ban" else utils.fmt_minutes(minutes)
            b.row(toggle, _btn("🛡 админы" if adm else "👥 все", f"u:ga:{cid}:{bit}"),
                  _btn(f"🔨 {prize}", f"u:gp:{cid}:{bit}"))
        else:
            b.row(toggle)
    b.row(_btn("⬅️ Назад", f"u:c:{cid}"))
    return "\n".join(lines), b.as_markup()


@router.callback_query(F.data.startswith("u:games:"))
async def cb_games(cb: CallbackQuery) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    text, kb = await view_games(cid)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:gb:"))
async def cb_game_toggle(cb: CallbackQuery) -> None:
    _, _, cid, bit = cb.data.split(":")
    cid, bit = int(cid), int(bit)
    if not await _guard(cb, cid):
        return
    s = await db.get_settings(cid)
    await db.set_setting(cid, "games_on", s.games_on ^ bit)
    text, kb = await view_games(cid)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


async def view_game_prize(cid: int, bit: int) -> tuple[str, InlineKeyboardMarkup]:
    """Приз проигравшему в конкретной игре."""
    s = await db.get_settings(cid)
    label = next(x[1] for x in config.GAME_BITS if x[0] == bit)
    kind_field, min_field = config.GAME_FIELDS[bit]
    kind, minutes = getattr(s, kind_field), getattr(s, min_field)
    text = (
        f"<b>🔨 {utils.esc(label)} · приз</b>\n\n"
        "Что достаётся проигравшему. Бан выдаётся навсегда — снимать вручную "
        "в разделе «🚫 Наказания».\n\n"
        f"Сейчас: <b>{'бан' if kind == 'ban' else 'мут ' + utils.fmt_minutes(minutes)}</b>"
    )
    b = InlineKeyboardBuilder()
    b.row(_btn(f"🔨 Наказание: {'бан' if kind == 'ban' else 'мут'}",
               f"u:gpk:{cid}:{bit}"))
    if kind == "mute":
        b.row(_btn("◀", f"u:gpm:{cid}:{bit}:-"),
              _btn(f"⏰ {utils.fmt_minutes(minutes)}", f"u:gpm:{cid}:{bit}:+"),
              _btn("▶", f"u:gpm:{cid}:{bit}:+"))
    b.row(_btn("⬅️ Назад", f"u:games:{cid}"))
    return text, b.as_markup()


@router.callback_query(F.data.startswith("u:gp:"))
async def cb_game_prize(cb: CallbackQuery) -> None:
    _, _, cid, bit = cb.data.split(":")
    cid, bit = int(cid), int(bit)
    if not await _guard(cb, cid):
        return
    text, kb = await view_game_prize(cid, bit)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:gpk:"))
async def cb_game_prize_kind(cb: CallbackQuery) -> None:
    _, _, cid, bit = cb.data.split(":")
    cid, bit = int(cid), int(bit)
    if not await _guard(cb, cid):
        return
    field = config.GAME_FIELDS[bit][0]
    s = await db.get_settings(cid)
    await db.set_setting(cid, field, "ban" if getattr(s, field) == "mute" else "mute")
    text, kb = await view_game_prize(cid, bit)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:gpm:"))
async def cb_game_prize_min(cb: CallbackQuery) -> None:
    _, _, cid, bit, way = cb.data.split(":")
    cid, bit = int(cid), int(bit)
    if not await _guard(cb, cid):
        return
    field = config.GAME_FIELDS[bit][1]
    s = await db.get_settings(cid)
    presets = list(config.MUTE_PRESETS)
    cur = presets.index(getattr(s, field)) if getattr(s, field) in presets else 0
    step = 1 if way == "+" else -1
    await db.set_setting(cid, field, presets[(cur + step) % len(presets)])
    text, kb = await view_game_prize(cid, bit)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:ga:"))
async def cb_game_access(cb: CallbackQuery) -> None:
    _, _, cid, bit = cb.data.split(":")
    cid, bit = int(cid), int(bit)
    if not await _guard(cb, cid):
        return
    s = await db.get_settings(cid)
    await db.set_setting(cid, "games_adm", s.games_adm ^ bit)
    text, kb = await view_games(cid)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


_LIFT_LABEL = {"any": "любой чат", "source": "только тот, где выдали"}

NET_CHATS_PER_PAGE = 8


async def _my_nets(viewer_id: int) -> list:
    """Сетки, которыми человек вправе управлять."""
    if viewer_id in config.ADMIN_IDS:
        return await db.nets_all()
    return await db.nets_of(viewer_id)


async def _net_guard(cb: CallbackQuery, net_id: int):
    """Сетка + проверка прав. None — чужая или удалена."""
    net = await db.net_get(net_id)
    if net is None:
        await cb.answer("Сетка удалена.", show_alert=True)
        return None
    if cb.from_user.id != net["owner_id"] and cb.from_user.id not in config.ADMIN_IDS:
        await cb.answer("Это чужая сетка.", show_alert=True)
        return None
    return net


# Откуда человек вошёл в сетки: 0 — из главного меню, иначе id чата. Нужно,
# чтобы «Назад» возвращал туда же, откуда пришли, а не всегда в главное меню.
_net_origin: dict[int, int] = {}


def _net_back(viewer_id: int) -> str:
    cid = _net_origin.get(viewer_id, 0)
    return f"u:c:{cid}" if cid else "u:home"


async def view_nets(viewer_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Список сеток: отсюда всё и настраивается."""
    nets = await _my_nets(viewer_id)
    mine = [n for n in nets if n["owner_id"] == viewer_id]
    lines = [
        "<b>🕸 Сетки чатов</b>\n",
        "Сетка — группа ваших чатов, между которыми разъезжаются наказания: "
        "бан в одном применяется во всех остальных. Чат состоит ровно в одной "
        "сетке или ни в одной.\n",
    ]
    b = InlineKeyboardBuilder()
    if not nets:
        lines.append("Пока ни одной сетки.")
    for n in nets:
        chats = await db.net_chats(n["id"])
        tag = ""
        if viewer_id in config.ADMIN_IDS and n["owner_id"] != viewer_id:
            tag = f" · {await db.user_handle(n['owner_id'])}"
        lines.append(f"• <b>{utils.esc(n['title'])}</b> — "
                     f"{len(chats)} {utils.plural(len(chats), 'чат', 'чата', 'чатов')}{tag}")
        b.row(_btn(f"🕸 {n['title'][:26]} ({len(chats)}){tag}",
                   f"u:netv:{n['id']}"))
    if len(mine) < config.NET_LIMIT:
        b.row(InlineKeyboardButton(text="🆕 Создать сетку",
                                   callback_data="u:netnew",
                                   style="success"))
    else:
        lines.append(f"\n<i>Лимит: {config.NET_LIMIT} сетки на человека.</i>")
    b.row(_btn("⬅️ Назад", _net_back(viewer_id)))
    return "\n".join(lines), b.as_markup()


async def view_net(net_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Одна сетка: её чаты и что между ними синхронизируется."""
    net = await db.net_get(net_id)
    b = InlineKeyboardBuilder()
    if net is None:
        b.row(_btn("⬅️ К сеткам", "u:nets"))
        return "Сетка удалена.", b.as_markup()

    chats = await db.net_chats(net_id)
    lines = [
        f"<b>🕸 {utils.esc(net['title'])}</b>\n",
        f"Чатов в сетке: <b>{len(chats)}</b>",
    ]
    if not chats:
        lines.append("\nПока пусто — добавьте чаты кнопкой ниже.")
    elif len(chats) == 1:
        lines.append("\n<i>Пока чат один, рассылать некуда.</i>")

    for c in chats:
        b.row(_btn(f"❌ {(c['title'] or c['chat_id'])}"[:40],
                   f"u:netrm:{net_id}:{c['chat_id']}"))
    b.row(InlineKeyboardButton(text="➕ Добавить чат",
                               callback_data=f"u:netadd:{net_id}:0", style="success"))
    row = []
    for bit, label in config.NET_BITS:
        mark = "✅" if net["sync_mask"] & bit else "🚫"
        row.append(_btn(f"{mark} {label}", f"u:netb:{net_id}:{bit}"))
        if len(row) == 2:
            b.row(*row)
            row = []
    if row:
        b.row(*row)
    b.row(_btn(f"🔓 Снимать может: {_LIFT_LABEL[net['lift_mode']]}", f"u:netl:{net_id}"))
    b.row(_btn("✏️ Переименовать", f"u:netren:{net_id}"))
    if len(chats) > 1:
        b.row(_btn("📥 Разослать активные баны по сетке", f"u:netim:{net_id}"))
    b.row(InlineKeyboardButton(text="🗑 Удалить сетку", callback_data=f"u:netdel:{net_id}",
                               style="danger"))
    b.row(_btn("⬅️ К сеткам", "u:nets"))
    return "\n".join(lines), b.as_markup()


async def view_net_add(net_id: int, viewer_id: int,
                       page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """Какие чаты можно положить в эту сетку."""
    net = await db.net_get(net_id)
    b = InlineKeyboardBuilder()
    if net is None:
        b.row(_btn("⬅️ К сеткам", "u:nets"))
        return "Сетка удалена.", b.as_markup()

    # только чаты того же владельца: чужие в сетку не затащить
    free = [c for c in await db.chats_for(viewer_id)
            if c["owner_id"] == net["owner_id"] and c["net_id"] != net_id]
    pages = max(1, -(-len(free) // NET_CHATS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    lines = [f"<b>➕ В сетку «{utils.esc(net['title'])}»</b>\n"]
    if not free:
        lines.append("Все ваши чаты уже здесь.")
    else:
        lines.append("Выберите чат. Если он состоит в другой сетке, то переедет "
                     "сюда — чат может быть только в одной.")
    for c in free[page * NET_CHATS_PER_PAGE:(page + 1) * NET_CHATS_PER_PAGE]:
        busy = await db.net_get(c["net_id"]) if c["net_id"] else None
        mark = f" · сейчас в «{busy['title'][:14]}»" if busy else ""
        b.row(_btn(f"{(c['title'] or c['chat_id'])}"[:30] + mark,
                   f"u:netput:{net_id}:{c['chat_id']}"))
    if pages > 1:
        b.row(_btn("◀", f"u:netadd:{net_id}:{(page - 1) % pages}"),
              _btn(f"{page + 1}/{pages}", f"u:netadd:{net_id}:{page}"),
              _btn("▶", f"u:netadd:{net_id}:{(page + 1) % pages}"))
    b.row(_btn("⬅️ Назад", f"u:netv:{net_id}"))
    return "\n".join(lines), b.as_markup()


async def _net_redraw(cb: CallbackQuery, net_id: int, note: str = "") -> None:
    text, kb = await view_net(net_id)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer(note)


@router.callback_query(F.data == "u:netsh")
async def cb_nets_home(cb: CallbackQuery, state: FSMContext) -> None:
    """Вход из главного меню: дальше «Назад» ведёт туда же."""
    _net_origin[cb.from_user.id] = 0
    await cb_nets(cb, state)


@router.callback_query(F.data == "u:nets")
async def cb_nets(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, kb = await view_nets(cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:netc:"))
async def cb_net_of_chat(cb: CallbackQuery) -> None:
    """Кнопка из карточки чата: открыть его сетку или общий список."""
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    _net_origin[cb.from_user.id] = cid          # «Назад» вернёт в карточку чата
    net = await db.net_of_chat(cid)
    if net is None:
        text, kb = await view_nets(cb.from_user.id)
    else:
        text, kb = await view_net(net["id"])
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:netv:"))
async def cb_net_view(cb: CallbackQuery, state: FSMContext) -> None:
    net = await _net_guard(cb, int(cb.data.split(":")[2]))
    if net is None:
        return
    await state.clear()
    await _net_redraw(cb, net["id"])


@router.callback_query(F.data.startswith("u:netadd:"))
async def cb_net_add(cb: CallbackQuery) -> None:
    _, _, net_id, page = cb.data.split(":")
    net = await _net_guard(cb, int(net_id))
    if net is None:
        return
    text, kb = await view_net_add(net["id"], cb.from_user.id, int(page))
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:netput:"))
async def cb_net_put(cb: CallbackQuery) -> None:
    _, _, net_id, cid = cb.data.split(":")
    net = await _net_guard(cb, int(net_id))
    if net is None:
        return
    cid = int(cid)
    ch = await db.get_chat(cid)
    if ch is None or ch["owner_id"] != net["owner_id"]:
        await cb.answer("Этот чат не ваш.", show_alert=True)
        return
    await db.net_assign(cid, net["id"])
    text, kb = await view_net_add(net["id"], cb.from_user.id, 0)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("Чат в сетке")


@router.callback_query(F.data.startswith("u:netrm:"))
async def cb_net_remove(cb: CallbackQuery) -> None:
    _, _, net_id, cid = cb.data.split(":")
    net = await _net_guard(cb, int(net_id))
    if net is None:
        return
    await db.net_assign(int(cid), None)
    await _net_redraw(cb, net["id"], "Чат убран из сетки")


@router.callback_query(F.data == "u:netnew")
async def cb_net_new(cb: CallbackQuery, state: FSMContext) -> None:
    if len(await db.nets_of(cb.from_user.id)) >= config.NET_LIMIT:
        await cb.answer(f"Больше {config.NET_LIMIT} сеток нельзя.", show_alert=True)
        return
    await _ask(
        cb, state, Input.net_title,
        "<b>🕸 Новая сетка</b>\n\nПришлите название — по нему вы будете узнавать её "
        "в списке. Например: <code>Основные</code> или <code>Игровые</code>.\n"
        "Чаты добавите следующим шагом.",
        "u:nets", net_id=0,
    )


@router.callback_query(F.data.startswith("u:netren:"))
async def cb_net_rename(cb: CallbackQuery, state: FSMContext) -> None:
    net = await _net_guard(cb, int(cb.data.split(":")[2]))
    if net is None:
        return
    await _ask(
        cb, state, Input.net_title,
        f"<b>🕸 Название сетки</b>\n\nСейчас: <code>{utils.esc(net['title'])}</code>\n"
        f"Пришлите новое.",
        f"u:netv:{net['id']}", net_id=net["id"],
    )


@router.message(StateFilter(Input.net_title))
async def net_title_input(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    net_id = data.get("net_id") or 0
    title = (message.text or "").strip()
    if title == "/cancel" or not title:
        view = await (view_net(net_id) if net_id else view_nets(message.from_user.id))
        await _done(message, bot, state, view)
        return
    if net_id:
        await db.net_set(net_id, "title", title[:40])
        await _done(message, bot, state, await view_net(net_id), "✅ Переименовано.\n\n")
        return
    new_id = await db.net_create(message.from_user.id, title)
    if new_id is None:
        await _done(message, bot, state, await view_nets(message.from_user.id),
                    f"⚠️ Больше {config.NET_LIMIT} сеток нельзя.\n\n")
        return
    await _done(message, bot, state, await view_net(new_id),
                "✅ Сетка создана. Теперь добавьте в неё чаты.\n\n")


@router.callback_query(F.data.startswith("u:netdel:"))
async def cb_net_delete(cb: CallbackQuery) -> None:
    net = await _net_guard(cb, int(cb.data.split(":")[2]))
    if net is None:
        return
    await db.net_delete(net["id"])
    text, kb = await view_nets(cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("Сетка удалена")


@router.callback_query(F.data.startswith("u:netb:"))
async def cb_net_bit(cb: CallbackQuery) -> None:
    _, _, net_id, bit = cb.data.split(":")
    net = await _net_guard(cb, int(net_id))
    if net is None:
        return
    await db.net_set(net["id"], "sync_mask", net["sync_mask"] ^ int(bit))
    await _net_redraw(cb, net["id"])


@router.callback_query(F.data.startswith("u:netl:"))
async def cb_net_lift(cb: CallbackQuery) -> None:
    net = await _net_guard(cb, int(cb.data.split(":")[2]))
    if net is None:
        return
    await db.net_set(net["id"], "lift_mode",
                     "source" if net["lift_mode"] == "any" else "any")
    await _net_redraw(cb, net["id"])


@router.callback_query(F.data.startswith("u:netim:"))
async def cb_net_import(cb: CallbackQuery, bot: Bot) -> None:
    """Свести активные баны сетки: у кого где висит — применить во всех её чатах.

    Только вручную: чаты могли жить своей жизнью, и внезапная пачка чужих банов
    должна быть осознанным решением.
    """
    from ..services import moderation, net as netsvc
    net = await _net_guard(cb, int(cb.data.split(":")[2]))
    if net is None:
        return
    await cb.answer("Свожу баны сетки, это займёт время…")
    chats = await db.net_chats(net["id"])
    seen: dict[int, str] = {}
    for c in chats:
        for p in await db.active_punishments(c["chat_id"], limit=MASS_LIMIT):
            if p["kind"] == "ban" and p["user_id"] > 0:
                seen.setdefault(p["user_id"], p["reason"] or "бан в сетке")
    done = failed = 0
    for uid, reason in list(seen.items())[:MASS_LIMIT]:
        user = await netsvc.user_stub(uid)
        for c in chats:
            if await db.active_punishment_of(c["chat_id"], uid, "ban") is not None:
                continue
            await asyncio.sleep(config.NET_DELAY)
            pid = await moderation.apply_punishment(
                bot, c["chat_id"], user, "ban", 0, f"сетка: {reason}", cb.from_user.id)
            if pid:
                done += 1
            else:
                failed += 1
    for c in chats:
        await db.add_event(c["chat_id"], "manual", "сведение банов сетки")
    text, kb = await view_net(net["id"])
    await cb.message.edit_text(
        text + f"\n\n📥 Заведено банов: <b>{done}</b>"
        + (f" · не удалось: {failed}" if failed else ""),
        reply_markup=kb,
    )


async def view_warned(cid: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """Кто в чате с активными варнами. Кнопка на человека — снять все его варны."""
    rows = await db.warn_users(cid)
    chunk, page, pages = _page_slice(rows, page)
    s = await db.get_settings(cid)
    lines = [
        "<b>⚠️ Варны</b>\n",
        f"Людей с варнами: <b>{len(rows)}</b> · лимит: <b>{s.warns_limit}</b>",
        "",
    ]
    if not rows:
        lines.append("Пока чисто.")
    start = page * LIST_PER_PAGE
    for i, r in enumerate(chunk, start + 1):
        who = f"@{r['username']}" if r["username"] else utils.esc(r["name"] or r["user_id"])
        lines.append(f"{i}. {who} — <b>{r['cnt']}</b>/{s.warns_limit} · "
                     f"{utils.rel_time(r['last_ts'])}")

    b = InlineKeyboardBuilder()
    for i, r in enumerate(chunk, start + 1):
        who = r["username"] or r["name"] or r["user_id"]
        b.row(_btn(f"{i}. 🧹 Снять варны: {str(who)[:22]}", f"u:wnr:{cid}:{r['user_id']}"))
    _pager(b, cid, "u:wn", page, pages)
    b.row(_btn("⬅️ Назад", f"u:s:{cid}:warns"))
    return "\n".join(lines), b.as_markup()


@router.callback_query(F.data.startswith("u:wn:"))
async def cb_warned(cb: CallbackQuery, state: FSMContext) -> None:
    _, _, cid, page = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    await state.clear()
    text, kb = await view_warned(cid, int(page))
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:wnr:"))
async def cb_warns_reset(cb: CallbackQuery) -> None:
    _, _, cid, uid = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    await db.warn_reset(cid, int(uid))
    text, kb = await view_warned(cid, 0)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("Варны сняты")


ACTIVE_PER_PAGE = 5
_KIND_WORD = {"ban": "бан", "mute": "мут", "banchan": "бан канала"}


async def view_punishments(cid: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """Главная страница раздела: сводка и действия. Сам список — за кнопкой,
    иначе при десятке наказаний экран превращался в лес кнопок «Снять…»."""
    rows = await db.active_punishments(cid, limit=1000)
    ch = await db.get_chat(cid)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
    lines = [
        f"<b>🚫 Наказания</b> · {utils.esc(ch['title'] if ch else str(cid))}\n",
        f"Активных сейчас: <b>{len(rows)}</b>",
    ]
    if counts:
        lines.append(", ".join(f"{_KIND_WORD.get(k, k)} — {n}" for k, n in sorted(counts.items())))
    else:
        lines.append("Все чисты.")
    lines.append("\nМассовые действия принимают список id или @username одним сообщением.")

    b = InlineKeyboardBuilder()
    b.row(_btn(f"📋 Активные: {len(rows)}", f"u:pa:{cid}:0"))
    b.row(_btn("🔓 Массовый разбан", f"u:mub:{cid}"),
          _btn("👢 Массовый кик", f"u:mkick:{cid}"))
    b.row(_btn("⛔ Массовый бан", f"u:mban:{cid}"))
    b.row(_btn("⚙️ Настройки", f"u:s:{cid}:punish_cfg"))
    b.row(_btn("⬅️ Назад", f"u:c:{cid}"))
    return "\n".join(lines), b.as_markup()


async def view_active(cid: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """Список активных наказаний: текстом с номерами, кнопки — только цифры."""
    total = await db.active_punishments_count(cid)
    pages = max(1, -(-total // ACTIVE_PER_PAGE))
    page = max(0, min(page, pages - 1))
    rows = await db.active_punishments(cid, limit=ACTIVE_PER_PAGE,
                                       offset=page * ACTIVE_PER_PAGE)
    lines = [
        "<b>📋 Активные наказания</b>\n",
        "Кнопка с номером снимает наказание.",
        f"\nВсего: <b>{total}</b>" + (f" · страница {page + 1} из {pages}" if pages > 1 else ""),
        "",
    ]
    if not rows:
        lines.append("Пусто — все чисты.")
    start = page * ACTIVE_PER_PAGE
    b = InlineKeyboardBuilder()
    for i, r in enumerate(rows, start + 1):
        who = r["name"] or await db.user_label(r["user_id"], r["username"])
        until = ("навсегда" if not r["until_ts"]
                 else f"до {utils.fmt_ts(r['until_ts'])}")
        lines.append(
            f"{i}. <b>{utils.esc(utils.chunk(who, 40))}</b> — "
            f"{_KIND_WORD.get(r['kind'], r['kind'])} {until}\n"
            f"    <i>{utils.esc(utils.chunk(r['reason'] or '—', 60))}</i>"
        )
        b.row(_btn(f"{i}. 🔓 Снять: {utils.chunk(who, 24)}",
                   f"u:pu:{cid}:{r['id']}:{page}"))
    if pages > 1:
        prev_p = page - 1 if page else pages - 1
        next_p = page + 1 if page + 1 < pages else 0
        b.row(_btn("◀", f"u:pa:{cid}:{prev_p}"),
              _btn(f"{page + 1}/{pages}", f"u:pa:{cid}:{page}"),
              _btn("▶", f"u:pa:{cid}:{next_p}"))
    b.row(_btn("⬅️ Назад", f"u:p:{cid}:0"))
    return "\n".join(lines), b.as_markup()


# ---------- входные точки ----------

async def _drop_reply_kb(message: Message) -> None:
    """Снять «залипшую» reply-клавиатуру пикера, если юзер бросил выбор чата.
    Убрать её можно только вместе с сообщением — шлём пустышку и сразу удаляем."""
    try:
        m = await message.answer("⌛", reply_markup=ReplyKeyboardRemove())
        await m.delete()
    except Exception:
        pass


@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext, bot: Bot) -> None:
    if await db.is_bot_banned(message.from_user.id):
        return
    # клавиатуру пикера снимаем только если она реально могла остаться —
    # иначе на каждый /start мелькала бы пустышка
    if await state.get_state() == Input.pick_log.state:
        await _drop_reply_kb(message)
    await state.clear()
    text, kb = await view_home(message.from_user.id, bot)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "u:home")
@router.callback_query(F.data == "a:home")  # алиас: админ-разделы возвращают сюда же
async def cb_home(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    was_picking = await state.get_state() in (Input.pick_log.state,)
    await state.clear()
    if was_picking:
        await _drop_reply_kb(cb.message)
    text, kb = await view_home(cb.from_user.id, bot)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:chats"))
async def cb_chats(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    was_picking = await state.get_state() in (Input.pick_log.state,)
    await state.clear()
    if was_picking:
        await _drop_reply_kb(cb.message)
    parts = cb.data.split(":")
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    text, kb = await view_chats(bot, cb.from_user.id, page)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data == "u:close")
async def cb_close(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await cb.message.delete()
    except Exception:
        # старше 48 часов Telegram удалять не даёт — просто гасим меню
        try:
            await cb.message.edit_text("Меню закрыто.", reply_markup=None)
        except Exception:
            pass
    await cb.answer()


# ---------- доступ к боту (только владелец бота) ----------

@router.callback_query(F.data == "u:acc")
async def cb_access(cb: CallbackQuery, state: FSMContext) -> None:
    if cb.from_user.id not in config.ADMIN_IDS:
        await cb.answer("Нет доступа.", show_alert=True)
        return
    await state.clear()
    text, kb = await view_access()
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data == "u:acca")
async def cb_access_add(cb: CallbackQuery, state: FSMContext) -> None:
    if cb.from_user.id not in config.ADMIN_IDS:
        await cb.answer("Нет доступа.", show_alert=True)
        return
    await _ask(
        cb, state, Input.access,
        "<b>👥 Доступ к боту</b>\n\nПришлите числовой <b>id</b> или <b>@username</b> "
        "того, кому открыть доступ.",
        "u:acc",
    )


@router.message(StateFilter(Input.access))
async def access_input(message: Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or "").strip()
    if text == "/cancel":
        await _done(message, bot, state, await view_access())
        return
    user_id, username = None, None
    if text.lstrip("-").isdigit():
        user_id = int(text)
    elif text.startswith("@") and len(text) > 3:
        username = text
        user_id, _ = await resolve.by_username(bot, text)   # закрепляем id, если нашли
    else:
        await _retry(message, bot, state,
                     "<b>👥 Доступ к боту</b>\n\n⚠️ Нужен числовой id или @username.")
        return
    await db.access_add(user_id, username)
    note = "✅ Добавлено.\n\n" if user_id else "✅ Добавлено (id узнать не вышло — сверяю по нику).\n\n"
    await _done(message, bot, state, await view_access(), note)


@router.callback_query(F.data.startswith("u:accd:"))
async def cb_access_del(cb: CallbackQuery) -> None:
    if cb.from_user.id not in config.ADMIN_IDS:
        await cb.answer("Нет доступа.", show_alert=True)
        return
    await db.access_remove(int(cb.data.split(":")[2]))
    text, kb = await view_access()
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("Удалено")


def _home_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ К чатам", callback_data="u:chats")
    return b.as_markup()


async def _guard(cb: CallbackQuery, cid: int) -> bool:
    """Чат свой? Владелец бота может всё, остальные — только свои чаты.

    Проверяем на каждое действие, а не только при открытии карточки: id чата
    лежит в callback, и его несложно подставить руками.
    """
    if await db.owns_chat(cb.from_user.id, cid):
        return True
    await cb.answer("Это не ваш чат.", show_alert=True)
    return False


@router.callback_query(F.data.startswith("u:c:"))
async def cb_chat(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    await state.clear()
    if await needs_setup(cid, cb.from_user.id):
        text, kb = await view_setup(cid)       # свежий чат — сперва развилка
    else:
        text, kb = await view_chat(cid, cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:cpn:"))
async def cb_setup_skip(cb: CallbackQuery, state: FSMContext) -> None:
    """«Настроить с нуля» — просто помечаем чат настроенным."""
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    await state.clear()
    await db.kv_set(setup_key(cid), "1")
    text, kb = await view_chat(cid, cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:cp:"))
async def cb_copy_from(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    await state.clear()
    text, kb = await view_copy_from(cid, cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:cps:"))
async def cb_copy_pick(cb: CallbackQuery, state: FSMContext) -> None:
    """Источник выбран — показываем галочки разделов, по умолчанию все."""
    from ..services import transfer
    _, _, cid, src = cb.data.split(":")
    cid, src = int(cid), int(src)
    if cid == src or not await _guard(cb, cid) or not await _guard(cb, src):
        return
    await state.update_data(copy_groups=list(transfer.ALL_GROUPS))
    text, kb = await view_copy_pick(cid, src, set(transfer.ALL_GROUPS))
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:cpg:"))
async def cb_copy_toggle(cb: CallbackQuery, state: FSMContext) -> None:
    """Галочка раздела: отметить, снять, всё, ничего."""
    from ..services import transfer
    _, _, cid, src, key = cb.data.split(":")
    cid, src = int(cid), int(src)
    if not await _guard(cb, cid) or not await _guard(cb, src):
        return
    data = await state.get_data()
    picked = set(data.get("copy_groups", transfer.ALL_GROUPS))
    if key == "__all":
        picked = set(transfer.ALL_GROUPS)
    elif key == "__none":
        picked = set()
    elif key in transfer.GROUPS:
        picked.symmetric_difference_update({key})
    await state.update_data(copy_groups=sorted(picked))
    text, kb = await view_copy_pick(cid, src, picked)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:cpd:"))
async def cb_copy_do(cb: CallbackQuery, state: FSMContext) -> None:
    from ..services import transfer
    _, _, cid, src = cb.data.split(":")
    cid, src = int(cid), int(src)
    if cid == src or not await _guard(cb, cid) or not await _guard(cb, src):
        return
    data = await state.get_data()
    picked = set(data.get("copy_groups", transfer.ALL_GROUPS))
    await state.clear()
    if not picked:
        await cb.answer("Ничего не отмечено.", show_alert=True)
        return
    await cb.answer("Переношу…")
    try:
        stats = await transfer.copy_chat(src, cid, picked)
    except Exception:
        logger.warning("перенос %s -> %s не удался", src, cid, exc_info=True)
        await cb.answer("Не вышло перенести, посмотрите «🐞 Ошибки».", show_alert=True)
        return
    await db.kv_set(setup_key(cid), "1")
    ch = await db.get_chat(src)
    moved = ", ".join(f"{k}: {v}" for k, v in stats.items() if v)
    note = (f"✅ Настройки перенесены из «{utils.esc(ch['title'] if ch else src)}».\n"
            f"{moved or 'нечего было копировать'}.\n\n")
    text, kb = await view_chat(cid, cb.from_user.id)
    await cb.message.edit_text(note + text, reply_markup=kb)


@router.callback_query(F.data.startswith("u:s:"))
async def cb_section(cb: CallbackQuery, state: FSMContext) -> None:
    _, _, cid, sec = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    await state.clear()
    if sec == "digest":
        from ..services import digest
        if digest.tracked_chat() != cid:
            await cb.answer(
                "Подробная статистика ведётся только для профильного чата. "
                "Здесь доступна базовая — раздел «📈 Статистика».",
                show_alert=True,
            )
            return
        # состав чата знает только юзербот — обновляем перед показом,
        # иначе в молчунах будут давно вышедшие
        from .. import userbot
        await cb.answer("Обновляю состав…")
        await userbot.refresh_members()
    text, kb = await view_section(cid, sec)
    await cb.message.edit_text(text, reply_markup=kb)
    if sec != "digest":
        await cb.answer()


# ---------- переключатели и селекторы ----------

async def _rerender(cb: CallbackQuery, cid: int, sec: str) -> None:
    if sec == "chat":
        text, kb = await view_chat(cid, cb.from_user.id)
    else:
        text, kb = await view_section(cid, sec)
    await cb.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("u:t:"))
async def cb_toggle(cb: CallbackQuery) -> None:
    _, _, cid, field = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    if field not in schema.TOGGLE_FIELDS:
        await cb.answer("?", show_alert=True)
        return
    s = await db.get_settings(cid)
    new = 0 if getattr(s, field) else 1
    await db.set_setting(cid, field, new)
    await _rerender(cb, cid, schema.FIELD_SECTION[field])
    await cb.answer("Включено" if new else "Выключено")


@router.callback_query(F.data.startswith("u:y:"))
async def cb_cycle(cb: CallbackQuery) -> None:
    _, _, cid, field, direction = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    values = schema.CYCLE_FIELDS.get(field)
    if not values:
        await cb.answer("?", show_alert=True)
        return
    s = await db.get_settings(cid)
    cur = getattr(s, field)
    try:
        idx = values.index(cur)
    except ValueError:
        idx = 0
    idx = (idx + (1 if direction == "+" else -1)) % len(values)
    await db.set_setting(cid, field, values[idx])
    await _rerender(cb, cid, schema.FIELD_SECTION[field])
    await cb.answer()


@router.callback_query(F.data.startswith("u:tb:"))
async def cb_trust_bit(cb: CallbackQuery) -> None:
    """Галочка «что смягчать» — тот же принцип, что у битов карточек."""
    _, _, cid, bit = cb.data.split(":")
    cid, bit = int(cid), int(bit)
    if not await _guard(cb, cid):
        return
    s = await db.get_settings(cid)
    await db.set_setting(cid, "trust_mask", s.trust_mask ^ bit)
    text, kb = await view_section(cid, "trust_soft")
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:cb:"))
async def cb_card_bit(cb: CallbackQuery) -> None:
    _, _, cid, bit = cb.data.split(":")
    cid, bit = int(cid), int(bit)
    if not await _guard(cb, cid):
        return
    s = await db.get_settings(cid)
    await db.set_setting(cid, "card_mask", s.card_mask ^ bit)
    await _rerender(cb, cid, "cards")
    await cb.answer()


# ---------- медиа-биты ----------

@router.callback_query(F.data.startswith("u:mb:"))
async def cb_media_bit(cb: CallbackQuery) -> None:
    _, _, cid, bit = cb.data.split(":")
    cid, bit = int(cid), int(bit)
    if not await _guard(cb, cid):
        return
    s = await db.get_settings(cid)
    await db.set_setting(cid, "media_mask", s.media_mask ^ bit)
    await _rerender(cb, cid, "media")
    await cb.answer()


# ---------- статистика чата ----------

@router.callback_query(F.data.startswith("u:st:"))
async def cb_stats(cb: CallbackQuery) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    st = await db.chat_stats(cid)
    ch = await db.get_chat(cid)
    lines = [
        f"<b>📈 Статистика</b> · {utils.esc(ch['title'] if ch else str(cid))}\n",
        f"💬 Сообщений: сегодня <b>{st['d1']}</b> · за 7д <b>{st['d7']}</b> · всего <b>{st['total']}</b>",
        f"👥 За 7 дней: пришло <b>{st['joins']}</b> · ушло <b>{st['leaves']}</b>",
        f"🔨 Наказаний за 7д: <b>{st['pun7']}</b>",
    ]
    if st["top"]:
        lines.append("\n<b>🏆 Топ за неделю:</b>")
        for i, (uid, cnt) in enumerate(st["top"], 1):
            u = await db.get_user(uid)
            # имя + ник, если знаем; голый id — только пока юзер ни разу не писал
            name = (u["first_name"] if u else None) or ""
            uname = f"@{u['username']}" if u and u["username"] else ""
            who = " ".join(x for x in (name, uname) if x) or str(uid)
            lines.append(f"{i}. {utils.esc(who)} — {cnt}")
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data=f"u:c:{cid}")
    await cb.message.edit_text("\n".join(lines), reply_markup=b.as_markup())
    await cb.answer()


# ---------- приветствие и правила (FSM) ----------

@router.callback_query(F.data.startswith("u:wtxt:"))
async def cb_welcome_text(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    s = await db.get_settings(cid)
    cur = f"\n\nСейчас:\n{utils.esc(s.welcome_text)}" if s.welcome_text else ""
    await _ask(
        cb, state, Input.welcome,
        "<b>👋 Текст приветствия</b>\n\nПришлите текст. <code>{name}</code> заменится "
        "на имя новичка.\nУбрать приветствие — пришлите <code>-</code>." + cur,
        f"u:s:{cid}:welcome", cid=cid,
    )


@router.message(StateFilter(Input.welcome))
async def welcome_input(message: Message, state: FSMContext, bot: Bot) -> None:
    # html_text сохраняет разметку и премиум-эмодзи так, как их набрали
    text = (message.html_text if message.text else "").strip()
    data = await state.get_data()
    cid = data["cid"]
    note = ""
    if text != "/cancel":
        await db.set_setting(cid, "welcome_text", None if text == "-" else text)
        if utils.has_premium_emoji(text):
            note = "✨ Премиум-эмодзи сохранены.\n\n"
    await _done(message, bot, state, await view_section(cid, "welcome"), note)




# ---------- триггеры (FSM: фраза -> ответ) ----------

@router.callback_query(F.data.startswith("u:tga:"))
async def cb_trig_add(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    if len(await db.trig_list(cid)) >= config.TRIG_LIMIT:
        await cb.answer(f"Лимит {config.TRIG_LIMIT} триггеров.", show_alert=True)
        return
    await _ask(
        cb, state, Input.trig_phrase,
        "<b>🎯 Новый триггер</b>\n\nПришлите ключевую фразу (от 3 символов).\n"
        "Срабатывает целиком: <code>донат</code> не поймает «донатный». "
        "Нужны окончания — добавьте звёздочку: <code>донат*</code>.",
        f"u:tgl:{cid}:0", cid=cid,
    )


@router.message(StateFilter(Input.trig_phrase))
async def trig_phrase_input(message: Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or "").strip()
    data = await state.get_data()
    if text == "/cancel":
        await _done(message, bot, state, await view_section(data["cid"], "triggers"))
        return
    if len(text) < 3:
        await _retry(message, bot, state,
                     "<b>🎯 Новый триггер</b>\n\n⚠️ Слишком коротко — нужно от 3 символов.")
        return
    await state.set_state(Input.trig_reply)
    await state.update_data(phrase=text.lower())
    await _retry(
        message, bot, state,
        f"<b>🎯 Новый триггер</b>\n\nФраза: <code>{utils.esc(text)}</code>\n\n"
        "Теперь пришлите ответ бота — текст или медиа (фото, стикер, гифка, видео, войс).",
    )


@router.message(StateFilter(Input.trig_reply))
async def trig_reply_input(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    cid, phrase = data["cid"], data["phrase"]
    if (message.text or "").strip() == "/cancel":
        await _done(message, bot, state, await view_trigs(cid, 0))
        return
    if message.text:
        # html_text сохраняет жирный/курсив/ссылки, которые человек набрал в Telegram
        await db.trig_add(cid, phrase, message.html_text)
    else:
        media = triggers.extract_media(message)
        if media is None:
            await _retry(
                message, bot, state,
                "<b>🎯 Новый триггер</b>\n\n⚠️ Не понял. Пришлите текст или медиа "
                "(фото, стикер, гифка, видео, войс).",
            )
            return
        # файл скачиваем сразу — триггер не зависит от сохранности этой переписки
        path = await triggers.save_media(bot, media.file_id, cid, media.kind)
        await db.trig_add(cid, phrase, message.html_text or None, path, media.kind)
    note = "✅ Триггер добавлен.\n\n"
    if not (await db.get_settings(cid)).trig_on:
        # добавили триггер — значит хотят, чтобы он работал; иначе «добавил, а тишина»
        await db.set_setting(cid, "trig_on", 1)
        note = "✅ Триггер добавлен, раздел включён.\n\n"
    await _done(message, bot, state, await view_trigs(cid, 0), note)


# ---------- недельная сводка ----------

@router.callback_query(F.data.startswith("u:dig:"))
async def cb_digest_to(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    await _ask(
        cb, state, Input.digest_to,
        "<b>📊 Получатель сводки</b>\n\nПришлите числовой <b>id</b> того, кому слать "
        "недельную сводку. Он должен хотя бы раз написать боту в личку, иначе "
        "доставить не выйдет.",
        f"u:s:{cid}:digest", cid=cid,
    )


@router.message(StateFilter(Input.digest_to))
async def digest_to_input(message: Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or "").strip()
    data = await state.get_data()
    cid = data["cid"]
    if text == "/cancel":
        await _done(message, bot, state, await view_section(cid, "digest"))
        return
    if not text.lstrip("-").isdigit():
        await _retry(message, bot, state,
                     "<b>📊 Получатель сводки</b>\n\n⚠️ Нужен числовой id.")
        return
    await db.set_setting(cid, "digest_to", int(text))
    await _done(message, bot, state, await view_section(cid, "digest"), "✅ Получатель задан.\n\n")


@router.callback_query(F.data.startswith("u:digoff:"))
async def cb_digest_off(cb: CallbackQuery) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    await db.set_setting(cid, "digest_to", 0)
    await _rerender(cb, cid, "digest")
    await cb.answer("Получатель убран")


@router.callback_query(F.data.startswith("u:dignow:"))
async def cb_digest_now(cb: CallbackQuery, bot: Bot) -> None:
    from ..services import digest
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    if digest.tracked_chat() != cid:
        await cb.answer("Сводка ведётся только для профильного чата.", show_alert=True)
        return
    s = await db.get_settings(cid)
    ok = await digest.send_digest(bot, cid, s.digest_to)
    await cb.answer("Отправлено" if ok else "Не вышло: нет базы статистики или юзер недоступен",
                    show_alert=not ok)


@router.callback_query(F.data == "d:x")
async def cb_close_message(cb: CallbackQuery) -> None:
    """Закрыть присланный список: просто убираем сообщение."""
    try:
        await cb.message.delete()
    except Exception:
        await cb.answer("Не смог удалить — уберите вручную.", show_alert=True)
        return
    await cb.answer()


@router.callback_query(F.data.startswith("d:close:"))
async def cb_close_digest(cb: CallbackQuery) -> None:
    """Закрыть сводку. Забываем её id, иначе следующая попробует править удалённое."""
    cid = int(cb.data.split(":")[2])
    await db.kv_set(f"digest_msg:{cid}", None)
    try:
        await cb.message.delete()
    except Exception:
        await cb.answer("Не смог удалить — уберите вручную.", show_alert=True)
        return
    await cb.answer()


@router.callback_query(F.data.startswith("d:silent:"))
async def cb_digest_silent(cb: CallbackQuery, bot: Bot) -> None:
    """Список молчунов блоком кода: такой копируется одним нажатием."""
    from ..services import digest
    cid = int(cb.data.split(":")[2])
    if digest.tracked_chat() != cid:
        await cb.answer("Сводка есть только по профильному чату.", show_alert=True)
        return
    d = await asyncio.to_thread(digest.collect, config.STATS_DB)
    rows = (d or {}).get("silent_raw") or []
    if not rows:
        await cb.answer("Молчунов нет — писали все.", show_alert=True)
        return
    await cb.answer()
    # шлём отдельными сообщениями, а не правим сводку: её удобно держать перед глазами
    head = f"<b>🤐 Не писали на этой неделе ({len(rows)}):</b>\n"
    chunk: list[str] = []
    size = 0
    for row in rows + [None]:                    # None — сигнал «долить остаток»
        line = utils.esc(row) if row else ""
        if row is not None and size + len(line) < 3500:
            chunk.append(line)
            size += len(line) + 1
            continue
        close = InlineKeyboardBuilder()
        close.button(text="✖️ Закрыть", callback_data="d:x")
        await cb.message.answer(head + "<pre>" + "\n".join(chunk) + "</pre>",
                                reply_markup=close.as_markup())
        head, chunk, size = "", [line], len(line) + 1
        if row is None:
            break


@router.callback_query(F.data.startswith("d:file:"))
async def cb_digest_file(cb: CallbackQuery, bot: Bot) -> None:
    """Кнопка под сводкой: собрать и прислать полный HTML-отчёт."""
    from ..services import digest
    import asyncio
    cid = int(cb.data.split(":")[2])
    if digest.tracked_chat() != cid:
        await cb.answer("Отчёт есть только по профильному чату.", show_alert=True)
        return
    await cb.answer("Собираю отчёт…")
    try:
        data = await asyncio.to_thread(digest.build_html, config.STATS_DB)
    except Exception as e:
        await cb.message.answer(f"Не удалось собрать отчёт: {e}")
        return
    await cb.message.answer_document(
        BufferedInputFile(data, filename="chat_report.html"),
        caption="Полный отчёт по чату",
    )


# ---------- счётчики (создание и правка) ----------

async def view_cmd(cid: int, rid: int) -> tuple[str, InlineKeyboardMarkup]:
    r = await db.cmd_get(rid)
    b = InlineKeyboardBuilder()
    if r is None:
        b.button(text="⬅️ Назад", callback_data=f"u:cml:{cid}:0")
        return "Счётчик не найден.", b.as_markup()
    cd = f"{r['cooldown']} сек" if r["cooldown"] else "без кулдауна"
    answers = await db.ans_list("cmd", rid)
    text = (
        f"<b>🔢 {utils.esc(r['cmd'])}</b>\n\n"
        f"Ответ: {_ans_preview(answers)} <i>+ [{r['count']}]</i>\n"
        f"Кулдаун: <b>{cd}</b>\n"
        f"Вызовов: <b>{r['count']}</b>"
    )
    b.row(_btn(f"🎲 Варианты ответа: {len(answers)}", f"u:an:{cid}:c:{rid}:0"))
    b.row(
        _btn("◀", f"u:cmc:{cid}:{rid}:-"),
        _btn(f"⏱ {cd}", f"u:cmc:{cid}:{rid}:+"),
        _btn("▶", f"u:cmc:{cid}:{rid}:+"),
    )
    b.row(_btn("🔄 Сбросить счётчик", f"u:cmr:{cid}:{rid}"))
    b.row(_btn("❌ Удалить счётчик", f"u:cmd:{cid}:{rid}"))
    b.row(_btn("⬅️ Назад", f"u:cml:{cid}:0"))
    return text, b.as_markup()


# ---------- массовый разбан ----------

MASS_LIMIT = 100
MASS_DELAY = 1.0     # пауза между людьми: лимиты Telegram важнее скорости


@router.callback_query(F.data.startswith("u:mub:"))
async def cb_mass_unban(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    await _ask(
        cb, state, Input.mass_unban,
        "<b>🔓 Массовый разбан</b>\n\nПришлите список одним сообщением: id или "
        "@username через пробел, запятую или с новой строки. Отрицательный id — "
        f"канал-отправитель.\nЗа раз обрабатываю до {MASS_LIMIT} штук, "
        "по одному в секунду — чтобы Telegram не выдал лимит.",
        f"u:p:{cid}:0", cid=cid,
    )


# Ответы Telegram человеку ни о чём не говорят — переводим знакомые.
_ERRORS_RU = (
    ("PARTICIPANT_ID_INVALID", "неверный id — это не человек из этого чата "
                               "(возможно, id канала или опечатка)"),
    ("USER_ID_INVALID", "такого пользователя не существует"),
    ("USER_NOT_PARTICIPANT", "в чате не состоит"),
    ("CHAT_ADMIN_REQUIRED", "у бота нет прав банить в этом чате"),
    ("CHANNEL_INVALID", "чат недоступен боту"),
    ("CHAT_NOT_FOUND", "чат не найден"),
    ("USER_NOT_FOUND", "пользователь не найден"),
    ("PEER_ID_INVALID", "неверный id"),
    ("Too Many Requests", "Telegram просит подождать — попробуйте позже"),
)


def _human_error(e: Exception) -> str:
    text = str(e)
    for needle, human in _ERRORS_RU:
        if needle.lower() in text.lower():
            return human
    return utils.esc(text[:80])


@router.callback_query(F.data.startswith("u:mban:"))
async def cb_mass_ban(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    await _ask(
        cb, state, Input.mass_ban,
        "<b>⛔ Массовый бан</b>\n\nПришлите список одним сообщением: id или "
        "@username через пробел, запятую или с новой строки. Отрицательный id — "
        "канал-отправитель.\n"
        "Банить можно и тех, кого в чате нет — тогда бан сработает на входе. "
        "Админов чата и владельца бота не трону.\n"
        f"За раз обрабатываю до {MASS_LIMIT} штук, по одному в секунду.",
        f"u:p:{cid}:0", cid=cid,
    )


@router.callback_query(F.data.startswith("u:mkick:"))
async def cb_mass_kick(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    await _ask(
        cb, state, Input.mass_kick,
        "<b>👢 Массовый кик</b>\n\nПришлите список одним сообщением: id или "
        "@username через пробел, запятую или с новой строки.\n"
        "Кик = бан и сразу разбан: человек вылетает из чата, но может вернуться "
        "по ссылке. Админов чата и владельца бота не трону.\n"
        f"За раз обрабатываю до {MASS_LIMIT} штук, по одному в секунду.",
        f"u:p:{cid}:0", cid=cid,
    )


async def _unban_channel(bot: Bot, cid: int, sid: int) -> tuple[str, str]:
    """Снять бан отправителя-канала. Забанен он или нет, Telegram не скажет,
    поэтому смотрим в свою базу: там записаны баны, которые ставил бот."""
    known = await db.active_punishment_of(cid, sid, "banchan")
    try:
        await bot.unban_chat_sender_chat(cid, sid)
    except Exception as e:
        return "fail", f"канал <code>{sid}</code> — {_human_error(e)}"
    await db.deactivate_user_punishments(cid, sid)
    if known is None:
        return "skip", f"канал <code>{sid}</code> — в моих банах не числился, бан снят на всякий случай"
    return "done", f"канал <code>{sid}</code>"


async def _unban_one(bot: Bot, cid: int, token: str, by_id: int | None = None) -> tuple[str, str]:
    """Разобрать одну запись списка. Вернуть (итог, строка для отчёта).

    Итог: done — сняли, skip — не был забанен, fail — не получилось.
    """
    uid, label = None, token
    if token.lstrip("-").isdigit():
        uid = int(token)
    elif token.startswith("@") and len(token) > 3:
        uid, name = await resolve.by_username(bot, token)
        label = name or token
        if uid is None:
            return "fail", f"{utils.esc(token)} — не удалось определить id"
    else:
        return "fail", f"{utils.esc(token)} — не похоже на id или @username"

    if uid < 0:
        return await _unban_channel(bot, cid, uid)

    try:
        member = await bot.get_chat_member(cid, uid)
    except Exception as e:
        # Каналы часто записывают без префикса -100: как участник такой id
        # невалиден, а как отправитель-канал — вполне рабочий. Пробуем так,
        # но если и там мимо — показываем исходную ошибку про участника.
        for variant in db.id_variants(uid):
            if variant < 0:
                result, line = await _unban_channel(bot, cid, variant)
                if result != "fail":
                    return result, line
                break
        return "fail", f"<code>{uid}</code> — {_human_error(e)}"
    who = label if label != token else await db.user_label(uid)
    # user_label для незнакомых возвращает сам id — не дублируем его в строке
    tag = f"{utils.esc(who)} (<code>{uid}</code>)" if who != str(uid) else f"<code>{uid}</code>"
    if member.status != "kicked":
        return "skip", f"{tag} — не забанен"
    try:
        await bot.unban_chat_member(cid, uid, only_if_banned=True)
    except Exception as e:
        return "fail", f"{tag} — {_human_error(e)}"
    await db.deactivate_user_punishments(cid, uid)
    return "done", tag


async def _kick_one(bot: Bot, cid: int, token: str, by_id: int | None = None) -> tuple[str, str]:
    """Выгнать одного. «Кика» в Bot API нет: баним и тут же снимаем бан —
    человек вылетает из чата, но может вернуться по ссылке."""
    uid, label = None, token
    if token.lstrip("-").isdigit():
        uid = int(token)
    elif token.startswith("@") and len(token) > 3:
        uid, name = await resolve.by_username(bot, token)
        label = name or token
        if uid is None:
            return "fail", f"{utils.esc(token)} — не удалось определить id"
    else:
        return "fail", f"{utils.esc(token)} — не похоже на id или @username"

    if uid < 0 or uid > 1_000_000_000_000:
        return "fail", f"<code>{uid}</code> — это канал, кикнуть нельзя (только забанить)"
    if uid in config.ADMIN_IDS:
        return "skip", f"<code>{uid}</code> — владелец бота, не трогаю"

    try:
        member = await bot.get_chat_member(cid, uid)
    except Exception as e:
        return "fail", f"<code>{uid}</code> — {_human_error(e)}"
    who = label if label != token else await db.user_label(uid)
    tag = f"{utils.esc(who)} (<code>{uid}</code>)" if who != str(uid) else f"<code>{uid}</code>"
    if member.status in ("creator", "administrator"):
        return "skip", f"{tag} — админ чата, не трогаю"
    if member.status in ("left", "kicked"):
        return "skip", f"{tag} — в чате не состоит"
    try:
        await bot.ban_chat_member(cid, uid)
        await bot.unban_chat_member(cid, uid, only_if_banned=True)   # бан снят = кик
    except Exception as e:
        return "fail", f"{tag} — {_human_error(e)}"
    return "done", tag        # в журнал не пишем: отчёт показывается в этом же окне


async def _ban_one(bot: Bot, cid: int, token: str,
                   by_id: int | None = None) -> tuple[str, str]:
    """Забанить одного. Отрицательный id — канал-отправитель, его баним отдельным
    методом: людей и каналов Telegram блокирует по-разному."""
    uid, label = None, token
    if token.lstrip("-").isdigit():
        uid = int(token)
    elif token.startswith("@") and len(token) > 3:
        uid, name = await resolve.by_username(bot, token)
        label = name or token
        if uid is None:
            return "fail", f"{utils.esc(token)} — не удалось определить id"
    else:
        return "fail", f"{utils.esc(token)} — не похоже на id или @username"

    if uid in config.ADMIN_IDS:
        return "skip", f"<code>{uid}</code> — владелец бота, не трогаю"

    if uid < 0:                       # канал: спрашивать статус не у чего
        try:
            await bot.ban_chat_sender_chat(cid, uid)
        except Exception as e:
            return "fail", f"канал <code>{uid}</code> — {_human_error(e)}"
        await db.add_punishment(cid, uid, None, None, "banchan",
                                "массовый бан", None, by_id, was_member=False)
        return "done", f"канал <code>{uid}</code>"

    who, status = await db.user_label(uid), None
    try:
        status = (await bot.get_chat_member(cid, uid)).status
    except Exception:
        pass                          # не участник — банить всё равно можно, «на входе»
    tag = (f"{utils.esc(label if label != token else who)} (<code>{uid}</code>)"
           if (label != token or who != str(uid)) else f"<code>{uid}</code>")
    if status in ("creator", "administrator"):
        return "skip", f"{tag} — админ чата, не трогаю"
    if status == "kicked":
        return "skip", f"{tag} — уже забанен"
    try:
        await bot.ban_chat_member(cid, uid)
    except Exception as e:
        return "fail", f"{tag} — {_human_error(e)}"
    await db.add_punishment(cid, uid, None, label if label != token else None,
                            "ban", "массовый бан", None, by_id,
                            was_member=status in ("member", "restricted"))
    return "done", tag


async def _mass_run(message: Message, bot: Bot, state: FSMContext, cid: int,
                    title: str, worker, labels: tuple[str, str, str],
                    empty_hint: str) -> None:
    """Общий движок массовых операций: разбор списка, пауза, прогресс, отчёт."""
    text = (message.text or "").strip()
    by_id = message.from_user.id if message.from_user else None
    if text == "/cancel":
        await _done(message, bot, state, await view_punishments(cid, 0))
        return
    tokens = [t for t in re.split(r"[\s,;]+", text) if t]
    if not tokens:
        await _retry(message, bot, state, f"<b>{title}</b>\n\n⚠️ {empty_hint}")
        return
    cut = len(tokens) > MASS_LIMIT
    tokens = tokens[:MASS_LIMIT]

    ch = await db.get_chat(cid)
    head = f"<b>{title}</b> · {utils.esc(ch['title'] if ch else str(cid))}\n\n"
    await _edit_menu(message, bot, state, head + f"Обрабатываю: 0 из {len(tokens)}…", None)

    # короткий список обновляем на каждом шаге, длинный — раз в десяток,
    # чтобы не долбить Telegram правками
    step = 1 if len(tokens) <= 20 else 10
    done, skip, fail = [], [], []
    for i, token in enumerate(tokens, 1):
        result, line = await worker(bot, cid, token, by_id)
        {"done": done, "skip": skip, "fail": fail}[result].append(line)
        if i % step == 0 and i < len(tokens):   # показываем, что не завис
            await _edit_menu(message, bot, state,
                             head + f"Обрабатываю: {i} из {len(tokens)}…", None)
        if i < len(tokens):
            await asyncio.sleep(MASS_DELAY)

    parts = [head.rstrip("\n")]
    for label, rows in zip(labels, (done, skip, fail)):
        if rows:
            parts.append(f"\n<b>{label} ({len(rows)}):</b>")
            parts.extend(f"• {r}" for r in rows)
    if cut:
        parts.append(f"\n<i>Обработал первые {MASS_LIMIT}, остальных пришлите "
                     f"следующим списком.</i>")
    b = InlineKeyboardBuilder()
    b.row(_btn("⬅️ К наказаниям", f"u:p:{cid}:0"))
    # сначала правим сообщение (в состоянии лежит его id), потом сбрасываем состояние
    await _edit_menu(message, bot, state, utils.chunk("\n".join(parts)), b.as_markup())
    await state.clear()


@router.message(StateFilter(Input.mass_unban))
async def mass_unban_input(message: Message, state: FSMContext, bot: Bot) -> None:
    await _mass_run(
        message, bot, state, (await state.get_data())["cid"], "🔓 Массовый разбан",
        _unban_one, ("✅ Разбанены", "➖ Не были забанены", "⚠️ Не вышло"),
        "Не нашёл ни одного id.",
    )


@router.message(StateFilter(Input.mass_ban))
async def mass_ban_input(message: Message, state: FSMContext, bot: Bot) -> None:
    await _mass_run(
        message, bot, state, (await state.get_data())["cid"], "⛔ Массовый бан",
        _ban_one, ("✅ Забанены", "➖ Пропущены", "⚠️ Не вышло"),
        "Не нашёл ни одного id.",
    )


@router.message(StateFilter(Input.mass_kick))
async def mass_kick_input(message: Message, state: FSMContext, bot: Bot) -> None:
    await _mass_run(
        message, bot, state, (await state.get_data())["cid"], "👢 Массовый кик",
        _kick_one, ("✅ Кикнуты", "➖ Пропущены", "⚠️ Не вышло"),
        "Не нашёл ни одного id.",
    )


@router.callback_query(F.data.startswith("u:an:"))
async def cb_answers(cb: CallbackQuery, state: FSMContext) -> None:
    _, _, cid, code, oid, page = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid) or code not in ANS_OWNER:
        return
    await state.clear()
    text, kb = await view_answers(cid, code, int(oid), int(page))
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:and:"))
async def cb_answer_del(cb: CallbackQuery) -> None:
    _, _, cid, code, oid, aid = cb.data.split(":")
    cid, oid = int(cid), int(oid)
    if not await _guard(cb, cid) or code not in ANS_OWNER:
        return
    await db.ans_remove(int(aid))
    text, kb = await view_answers(cid, code, oid, 0)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("Вариант удалён")


@router.callback_query(F.data.startswith("u:ana:"))
async def cb_answer_add(cb: CallbackQuery, state: FSMContext) -> None:
    _, _, cid, code, oid = cb.data.split(":")
    cid, oid = int(cid), int(oid)
    if not await _guard(cb, cid) or code not in ANS_OWNER:
        return
    if len(await db.ans_list(ANS_OWNER[code], oid)) >= ANS_LIMIT:
        await cb.answer(f"Лимит {ANS_LIMIT} вариантов.", show_alert=True)
        return
    hint = ("Пришлите текст, медиа или медиа с подписью.\n"
            "Форматирование и премиум-эмодзи сохраняются; вставить премиум-эмодзи "
            "может только человек с Telegram Premium."
            if code in ("t", "r") else "Пришлите текст ответа. Число в скобках бот допишет сам.")
    await _ask(
        cb, state, Input.ans_new,
        f"<b>🎲 Новый вариант ответа</b>\n\n{hint}",
        f"u:an:{cid}:{code}:{oid}:0", cid=cid, code=code, oid=oid,
    )


@router.message(StateFilter(Input.ans_new))
async def ans_new_input(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    cid, code, oid = data["cid"], data["code"], data["oid"]
    raw = (message.text or message.caption or "").strip()
    text = message.html_text.strip()          # с разметкой, как её набрали
    if raw == "/cancel":
        await _done(message, bot, state, await view_answers(cid, code, oid, 0))
        return

    media = triggers.extract_media(message) if code in ("t", "r") else None
    if media is None and not raw:
        hint = ("⚠️ Нужен текст или медиа." if code in ("t", "r")
                else "⚠️ Нужен текст: у счётчиков ответы только текстовые.")
        await _retry(message, bot, state, f"<b>🎲 Новый вариант ответа</b>\n\n{hint}")
        return

    path = None
    if media is not None:
        try:
            path = await triggers.save_media(bot, media.file_id, cid, media.kind)
        except Exception:
            await _retry(message, bot, state,
                         "<b>🎲 Новый вариант ответа</b>\n\n⚠️ Не смог скачать файл, попробуйте ещё раз.")
            return
    await db.ans_add(ANS_OWNER[code], oid, text or None, path, media.kind if media else None)
    note = "✅ Вариант добавлен."
    if utils.has_premium_emoji(text):
        note += " ✨ Премиум-эмодзи сохранены."
    await _done(message, bot, state, await view_answers(cid, code, oid, 0), note + "\n\n")


@router.callback_query(F.data.startswith("u:cml:"))
async def cb_cmds_page(cb: CallbackQuery, state: FSMContext) -> None:
    _, _, cid, page = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    await state.clear()
    text, kb = await view_cmds(cid, int(page))
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:tgl:"))
async def cb_trigs_page(cb: CallbackQuery, state: FSMContext) -> None:
    _, _, cid, page = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    await state.clear()
    text, kb = await view_trigs(cid, int(page))
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:cma:"))
async def cb_cmd_add(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    if len(await db.cmd_list(cid)) >= config.CMD_LIMIT:
        await cb.answer(f"Лимит {config.CMD_LIMIT} счётчиков.", show_alert=True)
        return
    await _ask(
        cb, state, Input.cmd_name,
        "<b>🔢 Новый счётчик</b>\n\nПришлите команду, например <code>!черви</code>.\n"
        "Забудете <code>!</code> — допишу сам.",
        f"u:cml:{cid}:0", cid=cid,
    )


_CMD_NAME_HEAD = "<b>🔢 Новый счётчик</b>\n\n"


@router.message(StateFilter(Input.cmd_name))
async def cmd_name_input(message: Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or "").strip().lower()
    data = await state.get_data()
    cid = data["cid"]
    if text == "/cancel":
        await _done(message, bot, state, await view_cmds(cid, 0))
        return
    if not text.startswith("!"):
        text = "!" + text
    if len(text) < 2 or " " in text:
        await _retry(message, bot, state, _CMD_NAME_HEAD + "⚠️ Команда должна быть одним словом.")
        return
    if text.lstrip("!") in _RESERVED_CMDS:
        await _retry(message, bot, state,
                     _CMD_NAME_HEAD + "⚠️ Это системная команда бота, её занять нельзя.")
        return
    if await db.cmd_find(cid, text):
        await _retry(message, bot, state, _CMD_NAME_HEAD + "⚠️ Такой счётчик уже есть.")
        return
    await state.set_state(Input.cmd_template)
    await state.update_data(cmd=text)
    await _retry(
        message, bot, state,
        f"{_CMD_NAME_HEAD}Команда: <code>{utils.esc(text)}</code>\n\n"
        "Теперь пришлите заготовку ответа. Например <code>кузнечики</code> — бот будет "
        "отвечать «кузнечики [1]», «кузнечики [2]»… Счётчик в скобках дописывается сам.",
    )


@router.message(StateFilter(Input.cmd_template))
async def cmd_template_input(message: Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or "").strip()
    data = await state.get_data()
    cid = data["cid"]
    if text == "/cancel":
        await _done(message, bot, state, await view_cmds(cid, 0))
        return
    if not text:
        await _retry(message, bot, state, _CMD_NAME_HEAD + "⚠️ Нужен текст заготовки.")
        return
    await db.cmd_add(cid, data["cmd"], message.html_text.strip(), 30)
    note = "✅ Счётчик создан.\n\n"
    if not (await db.get_settings(cid)).cmds_on:
        await db.set_setting(cid, "cmds_on", 1)
        note = "✅ Счётчик создан, раздел включён.\n\n"
    await _done(message, bot, state, await view_cmds(cid, 0), note)


@router.callback_query(F.data.startswith("u:cmv:"))
async def cb_cmd_view(cb: CallbackQuery, state: FSMContext) -> None:
    _, _, cid, rid = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    await state.clear()
    text, kb = await view_cmd(cid, int(rid))
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:cmc:"))
async def cb_cmd_cooldown(cb: CallbackQuery) -> None:
    _, _, cid, rid, direction = cb.data.split(":")
    cid, rid = int(cid), int(rid)
    if not await _guard(cb, cid):
        return
    r = await db.cmd_get(rid)
    if r is None:
        await cb.answer("Счётчик не найден.", show_alert=True)
        return
    values = list(config.CMD_COOLDOWN_PRESETS)
    try:
        idx = values.index(r["cooldown"])
    except ValueError:
        idx = 0
    idx = (idx + (1 if direction == "+" else -1)) % len(values)
    await db.cmd_set(rid, "cooldown", values[idx])
    text, kb = await view_cmd(cid, rid)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:cmr:"))
async def cb_cmd_reset(cb: CallbackQuery) -> None:
    _, _, cid, rid = cb.data.split(":")
    cid, rid = int(cid), int(rid)
    if not await _guard(cb, cid):
        return
    await db.cmd_set(rid, "count", 0)
    text, kb = await view_cmd(cid, rid)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("Счётчик сброшен")


@router.callback_query(F.data.startswith("u:cmd:"))
async def cb_cmd_del(cb: CallbackQuery) -> None:
    _, _, cid, rid = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    await db.cmd_remove(int(rid))
    text, kb = await view_cmds(cid, 0)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("Удалено")


@router.callback_query(F.data.startswith("u:tgd:"))
async def cb_trig_del(cb: CallbackQuery) -> None:
    _, _, cid, rid = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    await db.trig_remove(int(rid))
    text, kb = await view_trigs(cid, 0)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("Удалено")


# ---------- выбор лог-чата: нативный пикер Telegram (request_chat) ----------

@router.callback_query(F.data.startswith("u:logsel:"))
async def cb_log_select(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    # вопрос — в самом меню; отдельным сообщением идёт только носитель reply-кнопки
    # (request_chat живёт лишь на reply-клавиатуре), его потом удаляем
    await _ask(
        cb, state, Input.pick_log,
        "<b>📍 Лог-чат</b>\n\nНажмите кнопку «Выбрать чат» внизу экрана. Если бота "
        "в чате нет — Telegram предложит добавить.\n"
        "Убрать лог-чат — пришлите <code>-</code>.",
        f"u:s:{cid}:cards", cid=cid,
    )
    kb_msg = await cb.message.answer("👇", reply_markup=utils.request_chat_kb())
    await state.update_data(kb_msg_id=kb_msg.message_id)


@router.callback_query(F.data == "u:glog")
async def cb_global_log(cb: CallbackQuery, state: FSMContext) -> None:
    """Общий лог со всех чатов — только для владельца бота."""
    if cb.from_user.id not in config.ADMIN_IDS:
        await cb.answer("Нет доступа.", show_alert=True)
        return
    await _ask(
        cb, state, Input.pick_log,
        "<b>🌍 Глобальный лог</b>\n\nСюда копией летят все карточки со всех чатов — "
        "с теми же кнопками, что и в логе самого чата, так что модерировать можно "
        "прямо отсюда.\nНастройки карточек отдельных чатов на него не влияют.\n\n"
        "Нажмите «Выбрать чат» внизу экрана. Убрать — пришлите <code>-</code>.",
        "u:chats", cid=0,
    )
    kb_msg = await cb.message.answer("👇", reply_markup=utils.request_chat_kb())
    await state.update_data(kb_msg_id=kb_msg.message_id)


async def _finish_log_pick(message: Message, bot: Bot, state: FSMContext,
                           cid: int, note: str) -> None:
    """Убрать носитель клавиатуры и вернуть раздел на место."""
    data = await state.get_data()
    kb_msg_id = data.get("kb_msg_id")
    if kb_msg_id:
        try:
            await bot.delete_message(message.chat.id, kb_msg_id)
        except Exception:
            pass
    # снять саму клавиатуру у клиента
    try:
        tmp = await message.answer("⌛", reply_markup=ReplyKeyboardRemove())
        await tmp.delete()
    except Exception:
        pass
    view = (await view_chats(bot, message.from_user.id) if cid == 0
            else await view_section(cid, "cards"))
    await _done(message, bot, state, view, note)


@router.message(StateFilter(Input.pick_log), F.chat_shared)
async def log_chat_shared(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    cid = data["cid"]
    picked = message.chat_shared.chat_id
    if cid == 0:                                   # глобальный лог владельца бота
        if message.from_user.id not in config.ADMIN_IDS:
            return
        await db.set_global_log(picked)
        await db.add_event(None, "bot", f"глобальный лог: {picked}")
        await _finish_log_pick(message, bot, state, cid,
                               "✅ Глобальный лог обновлён.\n\n")
        return
    # чужой рабочий чат логом быть не может: туда полетели бы карточки с чужими
    # сообщениями. Свои и незнакомые боту чаты — пожалуйста.
    if not await db.owns_chat(message.from_user.id, picked) and await db.get_chat(picked):
        await _finish_log_pick(message, bot, state, cid,
                               "⚠️ Этот чат принадлежит другому владельцу.\n\n")
        return
    await db.set_setting(cid, "log_chat_id", picked)
    await db.add_event(cid, "bot", f"лог-чат установлен: {picked}")
    await _finish_log_pick(message, bot, state, cid, "✅ Лог-чат обновлён.\n\n")


@router.message(StateFilter(Input.pick_log))
async def log_pick_text(message: Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or "").strip()
    data = await state.get_data()
    cid = data["cid"]
    if text == "-":
        if cid == 0:
            if message.from_user.id not in config.ADMIN_IDS:
                return
            await db.set_global_log(None)
            await _finish_log_pick(message, bot, state, cid,
                                   "✅ Глобальный лог убран.\n\n")
            return
        await db.set_setting(cid, "log_chat_id", None)
        await _finish_log_pick(message, bot, state, cid, "✅ Лог-чат убран.\n\n")
    elif text == "/cancel":
        await _finish_log_pick(message, bot, state, cid, "")
    else:
        try:
            await message.delete()   # прочее просто убираем, вопрос остаётся на месте
        except Exception:
            pass


# ---------- наказания ----------

@router.callback_query(F.data.startswith("u:p:"))
async def cb_punishments(cb: CallbackQuery) -> None:
    _, _, cid, page = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    text, kb = await view_punishments(cid, int(page))
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:pa:"))
async def cb_active(cb: CallbackQuery, state: FSMContext) -> None:
    _, _, cid, page = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    await state.clear()
    text, kb = await view_active(cid, int(page))
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:pu:"))
async def cb_lift(cb: CallbackQuery, bot: Bot) -> None:
    from ..services import moderation
    parts = cb.data.split(":")
    cid, pid = int(parts[2]), int(parts[3])
    page = int(parts[4]) if len(parts) > 4 else 0
    if not await _guard(cb, cid):
        return
    # ссылку на возврат не делаем: она нужна только в карточке лог-чата
    p = await db.get_punishment(pid)
    ok, msg, _ = await moderation.lift_punishment(bot, pid, invite=False)
    if ok and p is not None:
        from ..services import net
        asyncio.create_task(net.lift(bot, p["chat_id"], p["user_id"]))
    text, kb = await view_active(cid, page)      # остаёмся на той же странице
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer(msg, show_alert=not ok)


@router.callback_query(F.data.startswith("a:clog:"))
async def cb_chat_log(cb: CallbackQuery) -> None:
    """Журнал конкретного чата. Доступен владельцу чата, не только владельцу бота."""
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    ch = await db.get_chat(cid)
    rows = await db.recent_events(20, chat_id=cid)
    title = utils.esc(ch["title"] if ch else str(cid))
    lines = [f"<b>📜 Лог чата</b> · {title}\n"]
    lines += [utils.event_line(r["kind"], await db.names_in(r["text"]), r["ts"])
              for r in rows] or ["Пока пусто."]
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data=f"u:c:{cid}")
    await cb.message.edit_text(utils.chunk("\n".join(lines)), reply_markup=b.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("a:leave:"))
async def cb_leave(cb: CallbackQuery) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    b = InlineKeyboardBuilder()
    b.button(text="✅ Да, выйти", callback_data=f"a:leave_yes:{cid}")
    b.button(text="⬅️ Отмена", callback_data=f"u:c:{cid}")
    b.adjust(2)
    await cb.message.edit_text(
        f"Точно покинуть чат <code>{cid}</code>?", reply_markup=b.as_markup()
    )
    await cb.answer()


@router.callback_query(F.data.startswith("a:leave_yes:"))
async def cb_leave_yes(cb: CallbackQuery, bot: Bot) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    try:
        await bot.leave_chat(cid)
    except Exception as e:
        await cb.answer(f"Не вышло: {e}", show_alert=True)
        return
    await db.set_chat_active(cid, False)
    await cb.message.edit_text("✅ Бот покинул чат.", reply_markup=_home_kb())
    await cb.answer()


# ---------- удаление из списков ----------

@router.callback_query(F.data.startswith("u:wld:"))
async def cb_wl_del(cb: CallbackQuery) -> None:
    """Убрать объект из вайтлиста целиком — со всеми его уровнями."""
    _, _, cid, rid = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    e = await db.wl_entry(cid, int(rid))
    if e is not None:
        await db.wl_set_scopes(cid, e["user_id"], e["username"], e["title"], set())
    await _rerender(cb, cid, "wl")
    await cb.answer("Удалено")


@router.callback_query(F.data.startswith("u:wle:"))
async def cb_wl_entry(cb: CallbackQuery, state: FSMContext) -> None:
    _, _, cid, rid = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    await state.clear()
    view = await view_wl_entry(cid, int(rid))
    if view is None:
        await _rerender(cb, cid, "wl")
        await cb.answer("Запись не найдена", show_alert=True)
        return
    await cb.message.edit_text(view[0], reply_markup=view[1])
    await cb.answer()


@router.callback_query(F.data.startswith("u:wlt:"))
async def cb_wl_toggle(cb: CallbackQuery, bot: Bot) -> None:
    """Галочка уровня. «Полный игнор» — тумблер над всеми остальными."""
    from ..services import moderation
    _, _, cid, rid, scope = cb.data.split(":")
    cid, rid = int(cid), int(rid)
    if not await _guard(cb, cid):
        return
    e = await db.wl_entry(cid, rid)
    if e is None:
        await _rerender(cb, cid, "wl")
        await cb.answer("Запись не найдена", show_alert=True)
        return

    on = _wl_effective(e["scopes"])
    if scope == "all":
        on = set() if "all" in e["scopes"] else set(WL_PARTS)
    elif scope in on:
        on.discard(scope)                 # снимаем галочку, в т.ч. «раскрывая» полный игнор
    else:
        on.add(scope)

    await db.wl_set_scopes(cid, e["user_id"], e["username"], e["title"], _wl_pack(on))
    if not on:                            # ни одной галочки — записи больше нет
        await _rerender(cb, cid, "wl")
        await cb.answer("Убран из вайтлиста")
        return

    note = ""
    if e["user_id"] and "anon" in on:     # разрешили анонимные — снимаем старый бан канала
        p = await db.active_punishment_of(cid, e["user_id"], "banchan")
        if p is not None:
            ok, msg, _ = await moderation.lift_punishment(bot, p["id"], invite=False)
            note = " · бан канала снят" if ok else f" · бан снять не вышло: {msg}"
            if ok:
                await db.add_event(
                    cid, "anon", f"разбан канала по вайтлисту: {e['title'] or e['user_id']}"
                )
    # уровни перезаписаны — у строк новые id, запись ищем по самому объекту
    fresh = await db.wl_entry_by_key(cid, e["user_id"], e["username"])
    view = await view_wl_entry(cid, fresh["row_id"]) if fresh else None
    if view is None:
        await _rerender(cb, cid, "wl")
    else:
        await cb.message.edit_text(view[0], reply_markup=view[1])
    await cb.answer((config.WL_SCOPE_LABELS[scope] + (" ✅" if scope in on or (
        scope == "all" and on == set(WL_PARTS)) else " ☐")) + note)


@router.callback_query(F.data.startswith("u:wd:"))
async def cb_words_page(cb: CallbackQuery) -> None:
    _, _, cid, page = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    text, kb = await view_words(cid, int(page))
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:wdd:"))
async def cb_word_del(cb: CallbackQuery) -> None:
    _, _, cid, page, rid = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    await db.words_remove(int(rid))
    flt.invalidate_words(cid)
    text, kb = await view_words(cid, int(page))
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("Удалено")


@router.callback_query(F.data.startswith("u:wdc:"))
async def cb_words_clear_ask(cb: CallbackQuery) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    n = len(await db.words_list(cid))
    b = InlineKeyboardBuilder()
    b.row(_btn(f"🗑 Да, удалить {n}", f"u:wdcy:{cid}"))
    b.row(_btn("⬅️ Отмена", f"u:wd:{cid}:0"))
    await cb.message.edit_text(
        f"<b>🧨 Очистить список?</b>\n\nБудет удалено слов: <b>{n}</b>. Отменить нельзя.",
        reply_markup=b.as_markup(),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("u:wdcy:"))
async def cb_words_clear(cb: CallbackQuery) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    n = await db.words_clear(cid)
    flt.invalidate_words(cid)
    text, kb = await view_words(cid, 0)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer(f"Удалено: {n}")




# ---------- добавление: вайтлист (FSM) ----------

@router.callback_query(F.data.startswith("u:wla:"))
async def cb_wl_add(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    await _ask(
        cb, state, Input.wl_target,
        "<b>🕊 Вайтлист</b>\n\nПришлите <b>id</b> или <b>@username</b> — юзера либо канала. "
        "Можно просто переслать сюда его сообщение.",
        f"u:s:{cid}:wl", cid=cid,
    )


@router.message(StateFilter(Input.wl_target))
async def wl_target_input(message: Message, state: FSMContext, bot: Bot) -> None:
    from ..services import moderation
    text = (message.text or "").strip()
    data = await state.get_data()
    cid = data["cid"]
    if text == "/cancel":
        await _done(message, bot, state, await view_section(cid, "wl"))
        return

    user_id, username, title = None, None, None
    origin = message.forward_origin
    origin_chat = getattr(origin, "chat", None) if origin else None
    origin_user = getattr(origin, "sender_user", None) if origin else None
    if origin_chat is not None:                    # переслали пост канала
        user_id, username, title = origin_chat.id, origin_chat.username, origin_chat.title
    elif origin_user is not None:                  # переслали сообщение человека
        user_id, username = origin_user.id, origin_user.first_name and origin_user.username
        title = origin_user.first_name
    elif text.lstrip("-").isdigit():
        user_id = int(text)
        for probe in db.id_variants(user_id):      # канал — подтянем название
            try:
                ch = await bot.get_chat(probe)
                if getattr(ch, "title", None):
                    user_id, title, username = probe, ch.title, ch.username
                    break
            except Exception:
                continue
    elif text.startswith("@") and len(text) > 3:
        username = text
        # ник могут сменить — сразу закрепляем постоянный id, если удаётся узнать
        user_id, title = await resolve.by_username(bot, text)
    else:
        await _retry(message, bot, state,
                     "<b>🕊 Вайтлист</b>\n\n⚠️ Нужен id, @username или пересланное сообщение.")
        return

    uname = (username or None) and username.lower().lstrip("@")
    exists = await db.wl_entry_by_key(cid, user_id, uname)
    if exists is None:
        # заводим сразу с полным игнором и открываем карточку: там галочками
        # снимают лишнее. Так не нужен отдельный шаг выбора одного уровня.
        await db.wl_set_scopes(cid, user_id, uname, title, {"all"})
        exists = await db.wl_entry_by_key(cid, user_id, uname)
        note = "✅ Добавлен с полным игнором. Снимите лишние галочки.\n\n"
        if user_id:                     # был забанен как анонимный отправитель — снимаем
            p = await db.active_punishment_of(cid, user_id, "banchan")
            if p is not None:
                ok, msg, _ = await moderation.lift_punishment(bot, p["id"], invite=False)
                note += ("✅ Бан канала снят.\n\n" if ok
                         else f"⚠️ Бан канала снять не вышло: {msg}\n\n")
                if ok:
                    await db.add_event(
                        cid, "anon", f"разбан канала по вайтлисту: {title or user_id}"
                    )
    else:
        note = "ℹ️ Он уже в вайтлисте — вот его настройки.\n\n"
    view = await view_wl_entry(cid, exists["row_id"])
    await _done(message, bot, state, view, note)


# ---------- карточка триггера ----------

async def view_trig(cid: int, rid: int) -> tuple[str, InlineKeyboardMarkup]:
    r = await db.trig_get(rid)
    b = InlineKeyboardBuilder()
    if r is None:
        b.button(text="⬅️ Назад", callback_data=f"u:tgl:{cid}:0")
        return "Триггер не найден.", b.as_markup()
    cd = f"{r['cooldown']} сек" if r["cooldown"] else "без кулдауна"
    answers = await db.ans_list("trig", rid)
    text = (
        f"<b>🎯 Триггер</b>\n\n"
        f"Фраза: <code>{utils.esc(r['phrase'])}</code>\n"
        f"Ответ: {_ans_preview(answers)}\n"
        f"Кулдаун: <b>{cd}</b>"
    )
    b.row(_btn("✏️ Изменить фразу", f"u:tgp:{cid}:{rid}"))
    b.row(_btn(f"🎲 Варианты ответа: {len(answers)}", f"u:an:{cid}:t:{rid}:0"))
    b.row(
        _btn("◀", f"u:tgc:{cid}:{rid}:-"),
        _btn(f"⏱ {cd}", f"u:tgc:{cid}:{rid}:+"),
        _btn("▶", f"u:tgc:{cid}:{rid}:+"),
    )
    b.row(_btn("❌ Удалить триггер", f"u:tgd:{cid}:{rid}"))
    b.row(_btn("⬅️ Назад", f"u:tgl:{cid}:0"))
    return text, b.as_markup()


@router.callback_query(F.data.startswith("u:tgv:"))
async def cb_trig_view(cb: CallbackQuery, state: FSMContext) -> None:
    _, _, cid, rid = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    await state.clear()
    text, kb = await view_trig(cid, int(rid))
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:tgc:"))
async def cb_trig_cooldown(cb: CallbackQuery) -> None:
    _, _, cid, rid, direction = cb.data.split(":")
    cid, rid = int(cid), int(rid)
    if not await _guard(cb, cid):
        return
    r = await db.trig_get(rid)
    if r is None:
        await cb.answer("Триггер не найден.", show_alert=True)
        return
    values = list(config.CMD_COOLDOWN_PRESETS)   # те же пресеты, что у счётчиков
    try:
        idx = values.index(r["cooldown"])
    except ValueError:
        idx = 0
    idx = (idx + (1 if direction == "+" else -1)) % len(values)
    await db.trig_set(rid, "cooldown", values[idx])
    text, kb = await view_trig(cid, rid)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:tgp:"))
async def cb_trig_edit_phrase(cb: CallbackQuery, state: FSMContext) -> None:
    _, _, cid, rid = cb.data.split(":")
    cid, rid = int(cid), int(rid)
    if not await _guard(cb, cid):
        return
    r = await db.trig_get(rid)
    if r is None:
        await cb.answer("Триггер не найден.", show_alert=True)
        return
    await _ask(
        cb, state, Input.trig_edit_phrase,
        f"<b>🎯 Триггер</b>\n\nПришлите новую ключевую фразу (от 3 символов). "
        f"Со звёздочкой на конце ловит окончания.\n"
        f"Сейчас: <code>{utils.esc(r['phrase'])}</code>",
        f"u:tgv:{cid}:{rid}", cid=cid, rid=rid,
    )


@router.message(StateFilter(Input.trig_edit_phrase))
async def trig_edit_phrase_input(message: Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or "").strip()
    data = await state.get_data()
    cid, rid = data["cid"], data["rid"]
    if text == "/cancel":
        await _done(message, bot, state, await view_trig(cid, rid))
        return
    if len(text) < 3:
        await _retry(message, bot, state,
                     "<b>🎯 Триггер</b>\n\n⚠️ Слишком коротко — нужно от 3 символов.")
        return
    await db.trig_set(rid, "phrase", text.lower())
    await _done(message, bot, state, await view_trig(cid, rid), "✅ Фраза обновлена.\n\n")


# ---------- разрешённые для ссылок чаты и каналы ----------

async def view_link_wl(cid: int) -> tuple[str, InlineKeyboardMarkup]:
    rows = await db.link_wl_list(cid)
    b = InlineKeyboardBuilder()
    text = (
        "<b>🔓 Разрешённые чаты и каналы</b>\n\n"
        "Ссылки на них бот не трогает. Этот чат и привязанный к нему канал "
        "разрешены всегда — их добавлять не нужно.\n"
        f"Записей: <b>{len(rows)}</b>"
    )
    b.row(_btn("➕ Добавить", f"u:lwa:{cid}"))
    for r in rows:
        label = r["title"] or (f"@{r['username']}" if r["username"] else str(r["target_id"]))
        extra = f" ({r['target_id']})" if r["target_id"] and r["title"] else ""
        b.row(_btn(f"❌ {label}{extra}", f"u:lwd:{cid}:{r['id']}"))
    b.row(_btn("⬅️ Назад", f"u:s:{cid}:links"))
    return text, b.as_markup()


@router.callback_query(F.data.startswith("u:lw:"))
async def cb_link_wl(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    await state.clear()
    text, kb = await view_link_wl(cid)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:lwa:"))
async def cb_link_wl_add(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    await _ask(
        cb, state, Input.link_wl,
        "<b>🔓 Разрешить чат или канал</b>\n\nПришлите <b>@username</b> или <b>id</b>. "
        "Можно переслать сюда сообщение оттуда — тогда возьму и id, и название.",
        f"u:lw:{cid}", cid=cid,
    )


@router.message(StateFilter(Input.link_wl))
async def link_wl_input(message: Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or "").strip()
    data = await state.get_data()
    cid = data["cid"]
    if text == "/cancel":
        await _done(message, bot, state, await view_link_wl(cid))
        return

    target_id, uname, title = None, None, None
    origin = message.forward_origin
    origin_chat = getattr(origin, "chat", None) if origin else None
    if origin_chat is not None:
        target_id, uname, title = origin_chat.id, origin_chat.username, origin_chat.title
    elif text.lstrip("-").isdigit():
        target_id = int(text)
        for probe in db.id_variants(target_id):
            try:
                ch = await bot.get_chat(probe)
                target_id, title, uname = probe, getattr(ch, "title", None), ch.username
                break
            except Exception:
                continue
    elif text.startswith("@") and len(text) > 3:
        uname = text.lstrip("@")
        target_id, title = await resolve.by_username(bot, text)
    else:
        await _retry(
            message, bot, state,
            "<b>🔓 Разрешить чат или канал</b>\n\n⚠️ Нужен @username, id или пересланное сообщение.",
        )
        return

    await db.link_wl_add(cid, target_id, uname, title)
    who = title or (f"@{uname}" if uname else str(target_id))
    note = (f"✅ {utils.esc(who)} разрешён.\n\n" if target_id
            else f"✅ {utils.esc(who)} разрешён (id не определился — сверяю по нику).\n\n")
    await _done(message, bot, state, await view_link_wl(cid), note)


@router.callback_query(F.data.startswith("u:lwd:"))
async def cb_link_wl_del(cb: CallbackQuery) -> None:
    _, _, cid, rid = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    await db.link_wl_remove(int(rid))
    text, kb = await view_link_wl(cid)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("Удалено")


# ---------- разрешённые инлайн-боты ----------

@router.callback_query(F.data.startswith("u:ila:"))
async def cb_inline_wl_add(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    await _ask(
        cb, state, Input.inline_wl,
        "<b>🤖 Разрешённый инлайн-бот</b>\n\nПришлите <b>@username</b> бота, вызовы "
        "которого в этом чате трогать не надо (например <code>@gif</code>).\n"
        "Можно и переслать сюда сообщение, отправленное через этого бота.",
        f"u:s:{cid}:inline", cid=cid,
    )


@router.message(StateFilter(Input.inline_wl))
async def inline_wl_input(message: Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or "").strip()
    data = await state.get_data()
    cid = data["cid"]
    if text == "/cancel":
        await _done(message, bot, state, await view_section(cid, "inline"))
        return

    uname, bot_id = None, None
    if message.via_bot is not None:            # переслали сообщение от инлайн-бота
        uname, bot_id = message.via_bot.username, message.via_bot.id
    elif text.startswith("@") and len(text) > 3:
        uname = text.lstrip("@")
        bot_id, _ = await resolve.by_username(bot, text)
    if not uname:
        await _retry(
            message, bot, state,
            "<b>🤖 Разрешённый инлайн-бот</b>\n\n⚠️ Нужен @username бота или сообщение, "
            "отправленное через него.",
        )
        return
    if await db.inline_wl_allowed(cid, uname, bot_id):
        await _retry(message, bot, state,
                     "<b>🤖 Разрешённый инлайн-бот</b>\n\n⚠️ Этот бот уже в списке.")
        return
    await db.inline_wl_add(cid, uname, bot_id)
    await _done(message, bot, state, await view_section(cid, "inline"),
                f"✅ @{utils.esc(uname)} разрешён.\n\n")


@router.callback_query(F.data.startswith("u:ild:"))
async def cb_inline_wl_del(cb: CallbackQuery) -> None:
    _, _, cid, rid = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    await db.inline_wl_remove(int(rid))
    await _rerender(cb, cid, "inline")
    await cb.answer("Удалено")


# ---------- добавление: стоп-слова (FSM) ----------

@router.callback_query(F.data.startswith("u:wda:"))
async def cb_words_add(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    await _ask(
        cb, state, Input.words,
        "<b>🧨 Стоп-слова</b>\n\nПришлите слова через запятую или с новой строки.\n"
        "<code>слово</code> — точное совпадение, <code>слово*</code> — с любыми окончаниями.",
        f"u:wd:{cid}:0", cid=cid,
    )


@router.message(StateFilter(Input.words))
async def words_input(message: Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or "").strip()
    data = await state.get_data()
    cid = data["cid"]
    if text == "/cancel":
        await _done(message, bot, state, await view_words(cid, 0))
        return
    added, dupes = 0, 0
    for raw in text.replace("\n", ",").split(","):
        w = raw.strip().lower()
        if not w:
            continue
        mode = "stem" if w.endswith("*") else "strict"
        w = w.rstrip("*")
        if w:
            if await db.words_add(cid, w, mode):
                added += 1
            else:
                dupes += 1
    if not added and not dupes:
        await _retry(message, bot, state, "<b>🧨 Стоп-слова</b>\n\n⚠️ Не нашёл ни одного слова.")
        return
    flt.invalidate_words(cid)
    note = f"✅ Добавлено слов: {added}."
    if dupes:
        note += f" Уже были в списке: {dupes}."
    await _done(message, bot, state, await view_words(cid, 0), note + "\n\n")
