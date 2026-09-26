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
    Message, ReplyKeyboardRemove, WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import config, db, runtime, schema, utils
from ..services import filters as flt, media, nn, resolve, triggers

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
    phrase = State()            # ждём фразу-образец для смысловых стоп-слов
    seed_q = State()            # ждём слово для поиска по стартовому набору
    sub_chat = State()          # ждём канал для проверки подписки
    prof_words = State()        # ждём слова для списка профилей
    status = State()            # ждём id/@username/пересылку для проверки статуса
    chat_admin = State()        # ждём id/@username админа чата для доступа к боту
    import_file = State()       # ждём zip с выгрузкой настроек


_HOME_TEXT = "<b>🧌 Gremlin</b>\n\nМодерация и мониторинг чатов."
# подсказка про панель: сама кнопка живёт рядом с полем ввода, но клиент
# подхватывает её не сразу — команда работает всегда
_PANEL_HINT = "\n\n🖥 Панель со всеми настройками: /panel"

# ---------- вьюхи (текст + клавиатура) ----------

_ADD_RIGHTS = "delete_messages+restrict_members+invite_users+pin_messages+manage_chat"

# имена системных команд бота — занимать их пользовательскими нельзя
_RESERVED_CMDS = {
    "mute", "мут", "ban", "бан", "warn", "варн", "пред", "unwarn", "снятьварн",
    "report", "репорт", "жалоба", "unmute", "размут", "unban", "разбан",
    "kick", "кик",
}


async def view_home(user_id: int, bot: Bot) -> tuple[str, InlineKeyboardMarkup]:
    """Единое меню — одинаковое для всех допущенных."""
    # панель открывается кнопкой рядом с полем ввода (её ставит app.py),
    # дублировать её ещё и здесь незачем
    b = InlineKeyboardBuilder()
    b.button(text="💬 Чаты", callback_data="u:chats")
    b.button(text="🕸 Сетки чатов", callback_data="u:netsh")
    if user_id in config.ADMIN_IDS:
        # служебные разделы и управление доступом — только владельцу бота
        b.button(text="📜 Лог событий", callback_data="a:log")
        b.button(text="🐞 Ошибки", callback_data="a:errors")
        b.button(text="⚙️ Состояние", callback_data="a:health")
        b.button(text="👥 Доступ к боту", callback_data="u:acc")
        b.button(text="🌱 Стартовый набор", callback_data="u:seed")
        b.button(text="🎪 Приколы", callback_data="f:home")
    b.button(text="✖️ Закрыть", callback_data="u:close")
    b.adjust(1, 1, 2, 1, 1, 1, 1, 1)
    text = _HOME_TEXT + (_PANEL_HINT if runtime.webapp_url() else "")
    return text, b.as_markup()


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
            # несколько, и по их именам не понять, к чему они прицеплены.
            # Берём из базы: спрашивать Telegram на каждую строку списка дорого
            linked = c["linked_title"] if "linked_title" in c.keys() else None
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
        # имя, записанное при добавлении, — запасной вариант: человек мог
        # так и не написать боту, и в users его нет
        stored = r["name"] if "name" in r.keys() else None
        who = await db.user_label(r["user_id"], r["username"], fallback=stored)
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
    """Чат ещё не настраивали — покажем короткую настройку вместо карточки.

    Раньше её показывали, только если было откуда перенести настройки. Владелец
    первого чата не видел её вовсе и не узнавал про лог-чат, без которого бот
    работает молча. viewer_id остаётся в сигнатуре: им пользуются вызывающие.
    """
    return not await db.kv_get(setup_key(cid))


async def view_setup(cid: int, viewer_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Короткая настройка свежего чата: лог-чат, потом перенос настроек."""
    ch = await db.get_chat(cid)
    s = await db.get_settings(cid)
    others = [c for c in await db.chats_for(viewer_id) if c["chat_id"] != cid]
    text = (
        f"<b>🆕 {utils.esc(ch['title'] if ch else str(cid))}</b>\n\n"
        "Бот на месте. Осталось главное — лог-чат.\n\n"
        "📍 <b>Лог-чат</b> — куда бот пишет, что произошло: кого наказал и за "
        "что, кто просится в чат, на кого пожаловались. Там же кнопки: снять "
        "наказание, забанить, впустить. Без него бот работает молча, и "
        "проверить его решения негде.\n"
        "Заведите под это отдельную группу и добавьте бота туда "
        "администратором: в рабочем чате такие сообщения никому не нужны.\n\n"
        + (f"Сейчас: {await _log_chat_label(s.log_chat_id)}."
           if s.log_chat_id else "Сейчас <b>не выбран</b>.")
    )
    if others:
        text += ("\n\n📥 <b>Перенос настроек</b> — забрать правила из другого "
                 "вашего чата: фильтры, стоп-слова, вайтлисты, триггеры и "
                 "счётчики целиком, вместе с медиа. Не переносятся лог-чат, "
                 "получатель сводки и счёт вызовов у команд.")
    b = InlineKeyboardBuilder()
    b.row(_btn("✅ Лог-чат выбран, сменить" if s.log_chat_id else "📍 Выбрать лог-чат",
               f"u:logsel:{cid}"))
    if others:
        b.row(_btn("📥 Перенести настройки", f"u:cp:{cid}"))
    b.row(_btn("🛠 Дальше настрою сам", f"u:cpn:{cid}"))
    b.row(_btn("⬅️ Назад", "u:chats"))
    return text, b.as_markup()


async def view_copy_from(cid: int, viewer_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Выбор чата-источника. Только свои чаты: иначе через перенос можно было бы
    вытащить чужие стоп-слова, вайтлист и триггеры."""
    others = [c for c in await db.chats_for(viewer_id) if c["chat_id"] != cid]
    b = InlineKeyboardBuilder()
    text = ("<b>📥 Откуда перенести</b>\n\nВыберите чат — его настройки скопируются сюда.\n\n"
            "Или файлом: «Выгрузить» пришлёт архив со всеми настройками этого чата, "
            "списками и медиа триггеров. Его можно хранить как резервную копию или "
            "загрузить в другой чат — даже в другого бота Гремлина.")
    if not others:
        text = ("<b>📥 Откуда перенести</b>\n\nДругих чатов у бота нет, но настройки "
                "можно выгрузить файлом и загрузить обратно.")
    b.row(_btn("📤 Выгрузить в файл", f"u:exp:{cid}"),
          _btn("📥 Загрузить из файла", f"u:imp:{cid}"))
    for c in others:
        b.row(_btn(c["title"] or str(c["chat_id"]), f"u:cps:{cid}:{c['chat_id']}"))
    b.row(_btn("⬅️ Назад", f"u:c:{cid}"))
    return text, b.as_markup()


FROM_FILE = 0     # «источник» в callback, когда настройки едут из файла


def _copy_scope(user_id: int, cid: int, src: int) -> tuple[str, ...] | None:
    """Какие разделы можно отметить. None — загруженный файл уже забыт."""
    from ..services import transfer
    if src != FROM_FILE:
        return transfer.shown_groups()
    got = transfer.stashed(user_id, cid)
    if got is None:
        return None
    return tuple(g for g in transfer.shown_groups() if g in got[0]["groups"])


async def view_copy_pick(cid: int, src: int, picked: set[str],
                         user_id: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """Галочки: какие разделы переносим — из другого чата или из файла."""
    from ..services import transfer
    scope = _copy_scope(user_id, cid, src) or ()
    if src == FROM_FILE:
        snap = (transfer.stashed(user_id, cid) or ({},))[0]
        inside = ", ".join(f"{k} {v}" for k, v in transfer.describe(snap).items())
        title = snap.get("chat_title") or "файл"
        text = (
            f"<b>📥 Загрузка из выгрузки «{utils.esc(title)}»</b>\n\n"
            "Отметьте, что загрузить. Настройки раздела заменятся, списки "
            "дополнятся: что уже есть в чате, останется.\n"
            + (f"В файле: {utils.esc(inside)}.\n" if inside else "")
            + f"Выбрано: <b>{len(picked)}</b> из {len(scope)}"
        )
    else:
        ch = await db.get_chat(src)
        text = (
            f"<b>📥 Перенос из «{utils.esc(ch['title'] if ch else str(src))}»</b>\n\n"
            "Отметьте, что перенести. Вместе с настройками едут и списки раздела: "
            "стоп-слова, вайтлист, разрешённые чаты и боты, триггеры с медиа, счётчики.\n"
            f"Выбрано: <b>{len(picked)}</b> из {len(scope)}"
        )
    b = InlineKeyboardBuilder()
    row = []
    for key in scope:
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


async def view_chat(cid: int, viewer_id: int,
                    bot: Bot | None = None) -> tuple[str, InlineKeyboardMarkup]:
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
    # Статус бота видят все, кто настраивает чат: без прав модерация молчит,
    # и понять это можно только по тому, что спам почему-то остаётся
    # Без лог-чата бот молчит: ни карточек, ни кнопок «снять наказание».
    # Это первое, что стоит настроить, поэтому строка отдельная и заметная
    log_warn = ("" if s.log_chat_id else
                "⚠️ Лог-чат не выбран: бот работает молча, карточек и кнопок нет\n")
    bot_line = ""
    if bot is not None and not (ch and ch["kind"] == "channel"):
        from ..services import adm_cache
        bot_line = f"🤖 Бот: {(await adm_cache.bot_status(bot, cid))['text']}\n"
    text = (
        f"<b>⚙️ {utils.esc(ch['title'] if ch else str(cid))}</b>\n"
        f"<code>{cid}</code>\n"
        f"{owner_line}\n"
        f"💬 Сообщений: сегодня <b>{st['d1']}</b> · за 7д <b>{st['d7']}</b>\n"
        f"👥 За 7д: пришло <b>{st['joins']}</b> · ушло <b>{st['leaves']}</b>\n"
        f"🔨 Наказаний: активных <b>{pun}</b> · за 7д <b>{st['pun7']}</b>\n"
        f"🪪 Лог-чат: {await _log_chat_label(s.log_chat_id)}\n"
        f"{bot_line}{log_warn}\n"
        f"{' · '.join(marks[:half])}\n{' · '.join(marks[half:])}"
    )
    # что человеку показывать, зависит от уровня: админ «наказаний» пришёл
    # снимать муты, разделы настроек ему только мешают
    level = await db.chat_access(viewer_id, cid)
    full = level in ("owner", "settings")
    sections: list[tuple[str, str]] = []
    if full:
        sections += [
            ("🤖 Инлайн-боты", f"u:s:{cid}:inline"),
            ("🔗 Ссылки", f"u:s:{cid}:links"),
            ("📛 Анонимы", f"u:s:{cid}:anon"),
            ("🧨 Стоп-слова", f"u:s:{cid}:words"),
            ("🌊 Антифлуд", f"u:s:{cid}:flood"),
            ("👁 Наблюдение", f"u:s:{cid}:watch"),
            ("🔍 Распознавание", f"u:s:{cid}:read"),
            ("🧪 Нейрофильтр", f"u:s:{cid}:nn"),
            ("🤖 Капча", f"u:s:{cid}:captcha"),
            ("📣 Вход по подписке", f"u:s:{cid}:sub"),
            ("🎖 Доверие", f"u:s:{cid}:trust"),
            ("⚠️ Варны", f"u:s:{cid}:warns"),
            ("🚨 Жалобы", f"u:s:{cid}:report"),
            ("🛡 Набеги", f"u:s:{cid}:raid"),
            ("⌨️ Команды чата", f"u:s:{cid}:modcmds"),
            ("🕊 Вайтлист", f"u:s:{cid}:wl"),
            ("👋 Приветствие", f"u:s:{cid}:welcome"),
            ("🎯 Триггеры", f"u:s:{cid}:triggers"),
            ("🔢 Счётчики", f"u:s:{cid}:cmds"),
            ("💱 Курс валют", f"u:s:{cid}:rates"),
            ("🎪 Приколы", f"u:games:{cid}"),
        ]
        if "media" not in schema.hidden_sections():
            sections.append(("🖼 Медиа-фильтры", f"u:s:{cid}:media"))
        from ..services import digest as _dg
        if _dg.tracked_chat() == cid:      # подробная статистика — только этот чат
            sections.append(("📊 Недельная сводка", f"u:s:{cid}:digest"))
        sections += [
            ("📜 Правила в постах", f"u:s:{cid}:rules"),
            ("🧹 Системные", f"u:s:{cid}:service"),
            ("🪪 Карточки и лог", f"u:s:{cid}:cards"),
        ]
    sections += [
        ("🚫 Наказания", f"u:p:{cid}:0"),
        ("📈 Статистика", f"u:st:{cid}"),
    ]
    if full:
        sections.append(("📜 Лог чата", f"a:clog:{cid}"))
    b = InlineKeyboardBuilder()
    for label, data in sections:
        b.button(text=label, callback_data=data)
    if level == "owner":
        log_ch = await db.get_chat(s.log_chat_id) if s.log_chat_id else None
        log_name = (log_ch["title"] if log_ch and log_ch["title"]
                    else (str(s.log_chat_id) if s.log_chat_id else "не задан"))
        b.button(text=f"📍 Лог-чат: {log_name}", callback_data=f"u:logsel:{cid}")
        net = await db.net_of_chat(cid)
        b.button(text=f"🕸 Сетка: {net['title'][:18] if net else 'нет'}",
                 callback_data=f"u:netc:{cid}")
        admins = await db.chat_admin_list(cid)
        b.button(text=f"👮 Админы в боте: {len(admins) or 'нет'}",
                 callback_data=f"u:ca:{cid}")
        b.button(text="📥 Перенести настройки", callback_data=f"u:cp:{cid}")
        b.button(text="🚪 Убрать бота из чата", callback_data=f"a:leave:{cid}")
    b.button(text="⬅️ Назад", callback_data="u:chats")
    # разделы по двое, всё остальное — по одному на строку: лишние кнопки
    # aiogram раскладывает по последнему размеру
    rows = [2] * (len(sections) // 2)
    if len(sections) % 2:
        rows.append(1)
    b.adjust(*rows, 1)
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
    if sec == "sub":
        text += await _sub_state(cid, s)

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
        # список отдельной страницей: при десятке записей раздел превращался
        # в лес кнопок, за которым не видно самих настроек
        n = len(await db.wl_entries(cid))
        b.row(_btn(f"📋 Список: {n}", f"u:wll:{cid}:0"))
        b.row(_btn("➕ Добавить", f"u:wla:{cid}"))

    elif widget == "logsel":
        log_str = str(s.log_chat_id) if s.log_chat_id else "не задан"
        b.row(_btn(f"📍 Лог-чат: {log_str}", f"u:logsel:{cid}"))

    elif widget == "prof_words":
        n = len(await db.words_list(cid, "prof"))
        b.row(_btn(f"📝 Слова для профилей: {n}", f"u:pw:{cid}:0"))

    elif widget == "sub_chat":
        b.row(_btn("🔎 Проверить доступ к каналу", f"u:subwhy:{cid}"))
        # у бота тут нет под рукой, поэтому название берём из своей базы;
        # незнакомый канал покажем номером — этого хватает, чтобы узнать его
        if s.sub_chat_id:
            row = await db.get_chat(s.sub_chat_id)
            label = (row["title"] if row and row["title"] else str(s.sub_chat_id))
        else:
            label = "привязанный к чату"
        b.row(_btn(f"📣 Канал: {label}", f"u:subch:{cid}"))

    elif widget == "sub_text":
        # в режиме отказа письма нет, и заготовка к нему тоже ни к чему
        if s.sub_action == "hold":
            rows = await db.ans_list("sub", cid)
            mark = "✏️" if rows else "⚠️"
            b.row(_btn(f"{mark} Сообщение в личку: {len(rows)}",
                       f"u:an:{cid}:s:{cid}:0"))

    elif widget == "phrases":
        rows = await db.phrases_list(cid)
        b.row(_btn(f"📝 Фразы-образцы: {len(rows)}", f"u:ph:{cid}"))
        b.row(_btn("➕ Добавить фразу", f"u:pha:{cid}"))

    elif widget == "read_stats":
        ocr = media.status()
        b.row(_btn(f"🖼 Картинки: {'tesseract готов' if ocr == 'ok' else ocr}",
                   f"u:s:{cid}:read"))
        asr = media.asr_status()
        b.row(_btn(f"🔊 Голосовые: {'служба подключена' if asr == 'ok' else asr}",
                   f"u:s:{cid}:read"))

    elif widget == "nn_stats":
        st = await db.samples_stats(cid)
        b.row(_btn(f"📊 Улик: {st['total']} · для сравнения: {st['profile']}",
                   f"u:s:{cid}:nn"))
        b.row(_btn(f"💬 Сообщения: ⛔ {st['spam']} · 🕊 {st['ok']} · "
                   f"✋ {st['unknown']}", f"u:s:{cid}:nn"))
        b.row(_btn(f"🧪 Профили спамеров: {st['faces_spam']}", f"u:pf:{cid}:0"))
        state = nn.status()
        b.row(_btn(f"🧠 Модель: {'загружена' if state == 'ok' else state}",
                   f"u:s:{cid}:nn"))
        way = ("регрессия по всей копилке" if st["profile"] >= config.NN_LOGREG_MIN
               else f"голоса соседей (регрессия с {config.NN_LOGREG_MIN})")
        b.row(_btn(f"📐 Как считает: {way}", f"u:s:{cid}:nn"))
        if st["profile"] < config.NN_MIN_SAMPLES:
            b.row(_btn(f"⏳ Для сравнения нужно хотя бы {config.NN_MIN_SAMPLES}",
                       f"u:s:{cid}:nn"))
        b.row(_btn("🗂 Кучки похожих улик", f"u:nnc:{cid}:unknown"))
        b.row(_btn("🤔 Разметить спорное", f"u:nnd:{cid}"))
        hint = await nn.suggest_threshold(cid)
        if hint:
            mark = "✅" if hint <= s.nn_threshold else "⚠️"
            b.row(_btn(f"{mark} Рекомендованный порог: {hint}%",
                       f"u:nnt:{cid}:{hint}"))

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
        rows = await db.ans_list("welcome", cid)
        b.row(_btn(f"✏️ Заготовки: {len(rows)}", f"u:an:{cid}:w:{cid}:0"))
        if s.welcome_text and not rows:
            b.row(_btn("⤴️ Перенести старый текст в заготовки", f"u:wmig:{cid}"))

    elif widget == "nn_shadow":
        import os as _os
        from ..services import nn as _nn
        path = _nn.shadow_path(cid)
        size = _os.path.getsize(path) if _os.path.exists(path) else 0
        if s.nn_mode > 1:
            note = (f"📄 Теневой журнал: {size // 1024} КБ" if size
                    else "📄 Теневой журнал пуст")
        else:
            note = "📄 Теневой журнал: режим не включён"
        b.row(_btn(note, f"u:s:{cid}:nn"))

    elif widget == "nn_subs":
        # смысловые фразы и рассылки — тот же нейрофильтр, только с другой
        # копилкой: держим их внутри него, а не отдельными пунктами меню
        n = len(await db.phrases_list(cid))
        b.row(_btn(f"{'✅' if s.sem_on else '🚫'} 🧠 Смысловые стоп-слова: {n}",
                   f"u:s:{cid}:sem"))
        b.row(_btn(f"{'✅' if s.burst_on else '🚫'} 📡 Рассылки",
                   f"u:s:{cid}:burst"))

    elif widget == "watch_subs":
        b.row(_btn(f"{'✅' if s.prof_on else '🚫'} 🪪 Проверка профиля",
                   f"u:s:{cid}:prof"))
        b.row(_btn(f"{'✅' if s.cas_on else '🚫'} 🌐 Общий список спамеров",
                   f"u:s:{cid}:cas"))
        b.row(_btn(f"🧪 Спам-профили: {len(await db.spam_profiles(cid))}",
                   f"u:pf:{cid}:0"))

    elif widget == "cas_stats":
        from ..services import cas as cas_svc
        st = await db.cas_stats()
        b.row(_btn(f"🌐 Сервис: {cas_svc.status()}", f"u:s:{cid}:cas"))
        b.row(_btn(f"📇 Запомнено ответов: {st['listed']} в списке, "
                   f"{st['clean']} чистых", f"u:s:{cid}:cas"))

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


WL_PER_PAGE = 8


async def view_wl(cid: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """Кто в вайтлисте. Нажатие на запись открывает её уровни игнора."""
    rows = await db.wl_entries(cid)
    pages = max(1, -(-len(rows) // WL_PER_PAGE))
    page = max(0, min(page, pages - 1))
    chunk = rows[page * WL_PER_PAGE:(page + 1) * WL_PER_PAGE]

    lines = [
        "<b>🕊 Вайтлист</b>\n",
        "Для этих людей и каналов проверки не работают. Нажмите на запись, "
        "чтобы выбрать, что именно им прощать.",
        f"\nВсего: <b>{len(rows)}</b>"
        + (f" · страница {page + 1} из {pages}" if pages > 1 else ""),
        "",
    ]
    if not rows:
        lines.append("Пусто.")
    b = InlineKeyboardBuilder()
    for i, e in enumerate(chunk, page * WL_PER_PAGE + 1):
        who = e["title"] or await db.user_label(e["user_id"], e["username"])
        lines.append(f"{i}. {utils.esc(who)} — <i>{_wl_scopes_label(e['scopes'])}</i>")
        b.row(_btn(f"{i}. 👤 {str(who)[:28]}", f"u:wle:{cid}:{e['row_id']}:{page}"))
    _pager(b, cid, "u:wll", page, pages)
    b.row(_btn("➕ Добавить", f"u:wla:{cid}"))
    b.row(_btn("⬅️ Назад", f"u:s:{cid}:wl"))
    return "\n".join(lines), b.as_markup()


_CLUSTER_SCOPES = {
    "unknown": ("✋ без оценки",
                "Это сообщения, за которые наказали вручную. Бот не знает, был "
                "там спам или личные счёты, поэтому в сравнении они не "
                "участвуют.\n\n"
                "Бот раскладывает их на кучки по смыслу текста. Кнопка "
                "размечает в кучке только сообщения без оценки — уже "
                "размеченные не меняются."),
    "profile": ("📚 что знает бот",
                "Всё, на чём бот уже учится: спам и обычные сообщения.\n\n"
                "Кучки собраны по смыслу текста, а не по пометкам, поэтому "
                "это скорее темы разговоров, чем виды спама: в одной кучке "
                "лежат и реклама, и обычные сообщения. Оптом их не "
                "разметить — «Разобрать» открывает кучку, и оценку можно "
                "поправить у каждого сообщения отдельно."),
}


def _cluster_state(g: dict) -> str:
    """Как размечены улики внутри кучки.

    Кучка — это просто похожие друг на друга улики, собранные вместе;
    своей метки у неё нет, метки есть у каждой улики. Голые числа
    «спам 2 · норма 7» рядом с «9 шт» читались как непонятно что,
    поэтому пишем предложением.
    """
    spam, ok, unknown = g.get("spam", 0), g.get("ok", 0), g.get("unknown", 0)
    if not spam and not ok:
        return "ни одна улика ещё не размечена"
    parts = []
    if spam:
        share = round(spam / max(1, g.get("size", spam + ok + unknown)) * 100)
        parts.append(f"⛔ спамом — {spam} ({share}%)")
    if ok:
        parts.append(f"🕊 нормой — {ok}")
    if unknown:
        parts.append(f"✋ без оценки — {unknown}")
    return "из них помечено: " + ", ".join(parts)


async def view_clusters(cid: int, scope: str) -> tuple[str, InlineKeyboardMarkup]:
    """Копилка, разложенная по кучкам похожего. Разметка идёт кучкой целиком."""
    ch = await db.get_chat(cid)
    title = utils.esc(ch["title"] if ch else str(cid))
    label, hint = _CLUSTER_SCOPES.get(scope, _CLUSTER_SCOPES["unknown"])
    lines = [f"<b>🗂 Кучки похожих улик</b> · {title}\n", hint,
             f"\nПоказаны: {label}", ""]

    b = InlineKeyboardBuilder()
    groups = await nn.clusters(cid, scope)
    if not groups:
        state = nn.status()
        if state != "ok":
            lines.append(f"Модель не загружена: {utils.esc(state)}")
        else:
            lines.append(f"Улик пока мало — нужно хотя бы {config.NN_MIN_SAMPLES}.")
    for i, g in enumerate(groups):
        words = ", ".join(g["words"]) or "—"
        sample = utils.esc(utils.chunk(" ".join(g["sample"].split()), 110))
        lines.append(f"<b>{i + 1}.</b> {g['size']} шт · <i>{utils.esc(words)}</i>")
        lines.append(f"<blockquote>{sample}</blockquote>")
        lines.append(f"<i>{_cluster_state(g)}</i>")
        if scope == "unknown":
            if g["unknown"]:
                b.row(_btn(f"{i + 1}. ⛔ спамом: {g['unknown']}",
                           f"u:nnl:{cid}:{i}:spam:{scope}"),
                      _btn(f"🕊 нормой: {g['unknown']}",
                           f"u:nnl:{cid}:{i}:ok:{scope}"))
        else:
            b.row(_btn(f"{i + 1}. 🔍 Разобрать ({g['size']})", f"u:nni:{cid}:{i}:0"))

    other = "profile" if scope == "unknown" else "unknown"
    b.row(_btn(f"🔀 Показать {_CLUSTER_SCOPES[other][0]}", f"u:nnc:{cid}:{other}"))
    b.row(_btn("⬅️ Назад", f"u:s:{cid}:nn"))
    return "\n".join(lines), b.as_markup()


@router.callback_query(F.data.startswith("u:nnc:"))
async def cb_clusters(cb: CallbackQuery) -> None:
    _, _, cid, scope = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    # разбивка считается не мгновенно: на тысяче улик это секунда-другая
    await cb.answer("Считаю…")
    text, kb = await view_clusters(cid, scope)
    await cb.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("u:nnl:"))
async def cb_cluster_label(cb: CallbackQuery) -> None:
    _, _, cid, index, label, scope = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    moved = await nn.label_cluster(cid, int(index), label, cb.from_user.id)
    if moved:
        await db.add_event(cid, "nn", f"кучка размечена как {label}: {moved} улик "
                                      f"by {cb.from_user.id}")
    await cb.answer(f"Размечено: {moved}" if moved
                    else "Размечать нечего или разбивка устарела", show_alert=False)
    text, kb = await view_clusters(cid, scope)
    await cb.message.edit_text(text, reply_markup=kb)


CLUSTER_PAGE = 5
_LABEL_MARK = {"spam": "⛔", "ok": "🕊", "unknown": "✋"}


async def view_cluster_items(cid: int, index: int,
                             page: int = 0) -> tuple[str, InlineKeyboardMarkup] | None:
    """Одна кучка по сообщению: оценку правят точечно, а не всей кучкой."""
    ids = nn.cluster_ids(cid, index)
    if ids is None:
        return None
    rows = await db.samples_by_ids(cid, ids)
    pages = max(1, -(-len(rows) // CLUSTER_PAGE))
    page = max(0, min(page, pages - 1))
    chunk = rows[page * CLUSTER_PAGE:(page + 1) * CLUSTER_PAGE]
    spam = sum(1 for r in rows if r["label"] == "spam")
    lines = [
        f"<b>🔍 Кучка {index + 1}</b> · {len(rows)} шт, спамом помечено {spam}\n",
        "⛔ — спам, 🕊 — норма. Нажмите номер с другой пометкой, чтобы поправить.",
        f"Страница {page + 1} из {pages}\n" if pages > 1 else "",
    ]
    b = InlineKeyboardBuilder()
    start = page * CLUSTER_PAGE
    for n, r in enumerate(chunk, start + 1):
        text = utils.esc(utils.chunk(" ".join(r["text"].split()), 140))
        lines.append(f"{n}. {_LABEL_MARK.get(r['label'], '?')} {text}")
        b.row(_btn(f"{n}. ⛔ спам" + (" ✓" if r["label"] == "spam" else ""),
                   f"u:nns:{cid}:{index}:{r['id']}:spam:{page}"),
              _btn(f"🕊 норма" + (" ✓" if r["label"] == "ok" else ""),
                   f"u:nns:{cid}:{index}:{r['id']}:ok:{page}"))
    if pages > 1:
        b.row(_btn("◀", f"u:nni:{cid}:{index}:{page - 1 if page else pages - 1}"),
              _btn(f"{page + 1}/{pages}", f"u:nni:{cid}:{index}:{page}"),
              _btn("▶", f"u:nni:{cid}:{index}:{page + 1 if page + 1 < pages else 0}"))
    b.row(_btn("⬅️ К кучкам", f"u:nnc:{cid}:profile"))
    return "\n".join(lines), b.as_markup()


@router.callback_query(F.data.startswith("u:nni:"))
async def cb_cluster_items(cb: CallbackQuery) -> None:
    _, _, cid, index, page = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    view = await view_cluster_items(cid, int(index), int(page))
    if view is None:
        await cb.answer("Разбивка устарела, откройте кучки заново.", show_alert=True)
        return
    await cb.message.edit_text(view[0], reply_markup=view[1])
    await cb.answer()


@router.callback_query(F.data.startswith("u:nns:"))
async def cb_cluster_item_label(cb: CallbackQuery) -> None:
    _, _, cid, index, sid, label, page = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid) or label not in ("spam", "ok"):
        return
    if await db.sample_set_label(cid, int(sid), label, cb.from_user.id):
        await db.add_event(cid, "nn", f"улика #{sid} размечена как {label} "
                                      f"by {cb.from_user.id}")
        # счётчики кучки берём из разбивки, поэтому сбрасываем только модель,
        # а саму разбивку оставляем: иначе после каждой правки её пришлось бы
        # пересчитывать и номера кучек уезжали бы
        nn._profile.clear()          # модель одна на все чаты
    view = await view_cluster_items(cid, int(index), int(page))
    if view is None:
        await cb.answer("Разбивка устарела, откройте кучки заново.", show_alert=True)
        return
    await cb.message.edit_text(view[0], reply_markup=view[1])
    await cb.answer("Поправлено")


async def _show_wl(cb: CallbackQuery, cid: int, page: int) -> None:
    text, kb = await view_wl(cid, page)
    await cb.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("u:wll:"))
async def cb_wl_list(cb: CallbackQuery, state: FSMContext) -> None:
    _, _, cid, page = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    await state.clear()
    text, kb = await view_wl(cid, int(page))
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


async def view_wl_entry(cid: int, row_id: int,
                        page: int = 0) -> tuple[str, InlineKeyboardMarkup] | None:
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
               f"u:wlt:{cid}:{row_id}:all:{page}"))
    row = []
    for scope in WL_PARTS:
        mark = "✅" if scope in on else "☐"
        row.append(_btn(f"{mark} {config.WL_SCOPE_LABELS[scope]}",
                        f"u:wlt:{cid}:{row_id}:{scope}:{page}"))
        if len(row) == 2:
            b.row(*row)
            row = []
    if row:
        b.row(*row)
    b.row(_btn("🗑 Убрать из вайтлиста", f"u:wld:{cid}:{row_id}:{page}"))
    b.row(_btn("⬅️ Назад", f"u:wll:{cid}:{page}"))
    return text, b.as_markup()


# ---------- варианты ответов (триггеры и счётчики) ----------
#
# Ответов у объекта может быть несколько, бот берёт случайный. Владельца в
# callback пишем одной буквой: t — триггер, c — счётчик (лимит 64 байта).

ANS_OWNER = {"t": "trig", "c": "cmd", "r": "rules", "w": "welcome",
             "s": "sub", "p": "paste"}
# owner совпадает с названием папки медиа — это же и назначение файла
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
    if code == "w":
        return f"u:s:{cid}:welcome"
    if code == "s":
        return f"u:s:{cid}:sub"
    if code == "p":
        return f"u:games:{cid}"
    return f"u:cmv:{cid}:{oid}"


async def view_answers(cid: int, code: str, oid: int,
                       page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    owner = ANS_OWNER[code]
    rows = await db.ans_list(owner, oid)
    chunk, page, pages = _page_slice(rows, page)
    title = {"t": "🎯 Триггер", "r": "📜 Правила", "w": "👋 Приветствие",
             "s": "📣 Сообщение о подписке",
             "p": "📜 Ответ на пасты"}.get(code, "🔢 Счётчик")
    lines = [
        f"<b>{title} · варианты ответа</b>\n",
        "Вариантов несколько — бот отвечает случайным. "
        + ("Можно текст, медиа или медиа с подписью."
           if code != "c" else "Только текст: число в скобках дописывается само."),
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


def _weight_of(row) -> int:
    """Вес слова. Записи, добавленные до появления колонки, считаем сильными."""
    try:
        return int(row["weight"])
    except (IndexError, KeyError, TypeError, ValueError):
        return config.UNI_W_STOPWORD


def _weight_mark(row) -> str:
    """Коротко для кнопки: чем слово весит меньше, тем бледнее значок."""
    return {15: "▁", 30: "▄", 45: "█"}.get(_weight_of(row), "█")


WEIGHT_HELP = (
    "\n\n⚖️ Вес — сколько слово значит для будущей единой оценки. "
    "На нынешние наказания он не влияет: там совпало — сработало.\n"
    "█ сильная — в живой речи не встречается («онлифанс», «п0драб0ткa»)\n"
    "▄ средняя — чаще у спама, но бывает и у людей\n"
    "▁ слабая — обычное слово («оплата», «пиши»): одной не хватит даже "
    "на подозрение"
)


async def _cycle_weight(cb: CallbackQuery, kind: str) -> None:
    """Перебрать вес слова по кругу и перерисовать список."""
    _, _, cid, page, rid = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    row = await db.words_get(int(rid))
    if row is None:
        await cb.answer("Слово уже удалили.", show_alert=True)
        return
    order = list(config.WORD_WEIGHTS)
    now = _weight_of(row)
    nxt = order[(order.index(now) + 1) % len(order)] if now in order else order[-1]
    await db.words_set_weight(int(rid), nxt)
    view = view_prof_words if kind == "prof" else view_words
    text, kb = await view(cid, int(page))
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer(f"«{row['word']}»: {config.WORD_WEIGHT_LABELS[nxt]} улика")


@router.callback_query(F.data.startswith("u:wdw:"))
async def cb_word_weight(cb: CallbackQuery) -> None:
    await _cycle_weight(cb, "msg")


@router.callback_query(F.data.startswith("u:pww:"))
async def cb_prof_word_weight(cb: CallbackQuery) -> None:
    await _cycle_weight(cb, "prof")


async def view_prof_words(cid: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """Слова, которые ищем в описании профиля.

    Список свой, а не общий с сообщениями: в сообщениях запрещают темы, а в
    описании то же слово ловит и того, кто тему осуждает. На этом мы уже
    забанили живого человека за «не переношу темы про изнасилования».
    """
    rows = await db.words_list(cid, "prof")
    pages = max(1, -(-len(rows) // WORDS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    start = page * WORDS_PER_PAGE
    chunk = rows[start:start + WORDS_PER_PAGE]

    lines = [
        "<b>📝 Слова для профилей</b>\n",
        "Ищутся в «о себе», названии канала и его описании. Сюда идут "
        "рекламные метки — «в лс», «онлифанс», «18+», — а не темы разговора: "
        "в описании они ловят и тех, кто тему осуждает." + WEIGHT_HELP,
        f"\nВсего: <b>{len(rows)}</b>"
        + (f" · страница {page + 1} из {pages}" if pages > 1 else ""),
        "",
    ]
    b = InlineKeyboardBuilder()
    if not rows:
        lines.append("Пусто — по словам профиль не проверяется.")
    for i, r in enumerate(chunk, start + 1):
        lines.append(f"{i}. {_weight_mark(r)} "
                     f"<code>{utils.esc(_word_label(r['word'], r['mode']))}</code>")

    for i, r in enumerate(chunk, start + 1):
        label = _word_label(r["word"], r["mode"])
        b.row(_btn(f"{_weight_mark(r)} {config.WORD_WEIGHT_LABELS[_weight_of(r)]}",
                   f"u:pww:{cid}:{page}:{r['id']}"),
              _btn(f"❌ {i}. {label[:16]}", f"u:pwd:{cid}:{page}:{r['id']}"))
    if pages > 1:
        b.row(
            _btn("⬅️", f"u:pw:{cid}:{page - 1 if page else pages - 1}"),
            _btn(f"{page + 1}/{pages}", f"u:pw:{cid}:{page}"),
            _btn("➡️", f"u:pw:{cid}:{page + 1 if page + 1 < pages else 0}"),
        )
    b.row(_btn("➕ Добавить", f"u:pwa:{cid}"))
    b.row(_btn("⬅️ Назад", f"u:s:{cid}:prof"))
    return "\n".join(lines), b.as_markup()


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
        "Слово со звёздочкой ловит любые окончания. Кнопка с номером удаляет "
        "слово, кнопка со значком меняет его вес." + WEIGHT_HELP,
        f"\nВсего: <b>{len(rows)}</b>" + (f" · страница {page + 1} из {pages}" if pages > 1 else ""),
        "",
    ]
    b = InlineKeyboardBuilder()
    if not rows:
        lines.append("Пусто — ни одного слова.")
    for i, r in enumerate(chunk, start + 1):
        lines.append(f"{i}. {_weight_mark(r)} "
                     f"<code>{utils.esc(_word_label(r['word'], r['mode']))}</code>")

    for i, r in enumerate(chunk, start + 1):
        label = _word_label(r["word"], r["mode"])
        b.row(_btn(f"{_weight_mark(r)} {config.WORD_WEIGHT_LABELS[_weight_of(r)]}",
                   f"u:wdw:{cid}:{page}:{r['id']}"),
              _btn(f"❌ {i}. {label[:16]}", f"u:wdd:{cid}:{page}:{r['id']}"))

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


async def view_phrases(cid: int) -> tuple[str, InlineKeyboardMarkup]:
    """Фразы-образцы: список с числом срабатываний и кнопками удаления."""
    rows = await db.phrases_list(cid)
    s = await db.get_settings(cid)
    lines = [
        "<b>🧠 Смысловые стоп-слова</b>\n",
        "Бот ловит сообщения, похожие по смыслу на эти фразы, даже если ни одно "
        "слово не совпало.",
        f"\nПохожесть: <b>{s.sem_threshold}%</b> · фраз: <b>{len(rows)}</b> "
        f"из {config.SEM_LIMIT}",
        "",
    ]
    b = InlineKeyboardBuilder()
    if not rows:
        lines.append("Пусто. Добавьте пару фраз из того, что уже ловили руками.")
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {utils.esc(utils.chunk(r['text'], 90))} "
                     f"— <i>поймала {r['hits']}</i>")
        b.row(_btn(f"❌ {i}. {utils.chunk(r['text'], 28)}", f"u:phd:{cid}:{r['id']}"))
    b.row(_btn("➕ Добавить фразу", f"u:pha:{cid}"))
    b.row(_btn("⬅️ Назад", f"u:s:{cid}:sem"))
    return "\n".join(lines), b.as_markup()


async def view_doubt(cid: int) -> tuple[str, InlineKeyboardMarkup]:
    """Улики, на которых фильтр колеблется. Ответы здесь дороже всего."""
    ch = await db.get_chat(cid)
    title = utils.esc(ch["title"] if ch else str(cid))
    lines = [
        f"<b>🤔 Спорное</b> · {title}\n",
        "Это ручные наказания, о которых бот не знает, спам там был или нет, "
        "и его оценка близка к «не пойму». Разметьте — десяток таких ответов "
        "двигает качество сильнее сотни очевидных.",
        "",
    ]
    b = InlineKeyboardBuilder()
    items = await nn.doubtful(cid)
    if not items:
        state = nn.status()
        lines.append(f"Модель не загружена: {utils.esc(state)}" if state != "ok"
                     else "Спорного нет — либо копилка пуста, либо всё однозначно.")
    for i, it in enumerate(items, 1):
        lines.append(f"<b>{i}.</b> оценка {it['score']}%")
        lines.append(f"<blockquote>{utils.esc(utils.chunk(it['text'], 160))}</blockquote>")
        b.row(_btn(f"{i}. ⛔ спам", f"u:nnm:{cid}:{it['id']}:spam"),
              _btn("🕊 норма", f"u:nnm:{cid}:{it['id']}:ok"))
    b.row(_btn("🔄 Ещё", f"u:nnd:{cid}"))
    b.row(_btn("⬅️ Назад", f"u:s:{cid}:nn"))
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
    paste_n = len(await db.ans_list("paste", cid))
    for bit, label, how, about in config.GAME_BITS:
        on = bool(s.games_on & bit)
        adm = bool(s.games_adm & bit)
        # приз и «кому можно» есть только у того, что зовут командой: титулы
        # бот публикует сам, ответ на пасту тоже никто не вызывает руками
        by_hand = bit in config.GAME_FIELDS
        prize_line = ""
        if by_hand:
            kind = getattr(s, config.GAME_FIELDS[bit][0])
            minutes = getattr(s, config.GAME_FIELDS[bit][1])
            prize_line = ("бан" if kind == "ban"
                          else f"мут на {utils.fmt_minutes(minutes)}")
            prize_line = f" · приз: <b>{prize_line}</b>"
        if bit == config.GAME_PASTE:
            prize_line = f" · от <b>{s.paste_min}</b> знаков"
            if s.paste_cd:
                prize_line += f" · не чаще раза в {utils.fmt_minutes(s.paste_cd)}"
        if bit == config.GAME_VANISH:
            prize_line = f" · стирает <b>{s.vanish_n}</b>"
        lines.append(
            f"{'✅' if on else '🚫'} <b>{label}</b> · <code>{how}</code>"
            + (" · только админы" if on and adm and bit in config.GAME_CALLED else "")
            + prize_line
            + f"\n<i>{about}</i>\n"
        )
        if bit == config.GAME_PASTE and on and not paste_n:
            lines.append("⚠️ Заготовок нет — отвечать нечем, бот промолчит.\n")
        toggle = _btn(f"{'✅' if on else '🚫'} {label}", f"u:gb:{cid}:{bit}")
        if by_hand:
            kind, minutes = getattr(s, config.GAME_FIELDS[bit][0]), \
                getattr(s, config.GAME_FIELDS[bit][1])
            prize = "бан" if kind == "ban" else utils.fmt_minutes(minutes)
            b.row(toggle, _btn("🛡 админы" if adm else "👥 все", f"u:ga:{cid}:{bit}"),
                  _btn(f"🔨 {prize}", f"u:gp:{cid}:{bit}"))
        elif bit == config.GAME_PASTE:
            b.row(toggle,
                  _btn(f"📏 {s.paste_min}", f"u:pl:{cid}"),
                  _btn("⏰ " + (utils.fmt_minutes(s.paste_cd) if s.paste_cd
                               else "без паузы"), f"u:pcd:{cid}"))
            b.row(_btn(f"{'✏️' if paste_n else '⚠️'} Заготовки ответов: {paste_n}",
                       f"u:an:{cid}:p:{cid}:0"))
        elif bit == config.GAME_VANISH:
            b.row(toggle, _btn("🛡 админы" if adm else "👥 все", f"u:ga:{cid}:{bit}"),
                  _btn(f"🧹 {s.vanish_n}", f"u:vn:{cid}"))
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


async def _paste_cycle(cb: CallbackQuery, field: str, presets) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    s = await db.get_settings(cid)
    vals = list(presets)
    cur = vals.index(getattr(s, field)) if getattr(s, field) in vals else 0
    await db.set_setting(cid, field, vals[(cur + 1) % len(vals)])
    text, kb = await view_games(cid)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:pl:"))
async def cb_paste_min(cb: CallbackQuery) -> None:
    await _paste_cycle(cb, "paste_min", config.PASTE_MIN_PRESETS)


@router.callback_query(F.data.startswith("u:pcd:"))
async def cb_paste_cd(cb: CallbackQuery) -> None:
    await _paste_cycle(cb, "paste_cd", config.PASTE_CD_PRESETS)


@router.callback_query(F.data.startswith("u:vn:"))
async def cb_vanish_n(cb: CallbackQuery) -> None:
    await _paste_cycle(cb, "vanish_n", config.VANISH_PRESETS)


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
    if not await _guard(cb, cid, "owner"):
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


async def net_import_run(bot: Bot, net_id: int, by_id: int | None) -> tuple[int, int]:
    """Свести активные баны сетки: у кого где висит — применить во всех её чатах.

    Только вручную: чаты могли жить своей жизнью, и внезапная пачка чужих банов
    должна быть осознанным решением. Возвращает (заведено, не вышло).
    """
    from ..services import moderation, net as netsvc
    chats = await db.net_chats(net_id)
    seen: dict[int, str] = {}
    for c in chats:
        for p in await db.active_punishments(c["chat_id"], limit=MASS_LIMIT):
            if p["kind"] == "ban" and p["user_id"] > 0:
                seen.setdefault(p["user_id"], p["reason"] or "бан в сетке")
    done = failed = 0
    for uid, reason in list(seen.items())[:MASS_LIMIT]:
        user = await netsvc.user_stub(uid, bot, chats[0]["chat_id"] if chats else None)
        for c in chats:
            if await db.active_punishment_of(c["chat_id"], uid, "ban") is not None:
                continue
            await asyncio.sleep(config.NET_DELAY)
            pid = await moderation.apply_punishment(
                bot, c["chat_id"], user, "ban", 0, f"сетка: {reason}", by_id)
            if pid:
                done += 1
            else:
                failed += 1
    for c in chats:
        await db.add_event(c["chat_id"], "manual", "сведение банов сетки")
    return done, failed


@router.callback_query(F.data.startswith("u:netim:"))
async def cb_net_import(cb: CallbackQuery, bot: Bot) -> None:
    net = await _net_guard(cb, int(cb.data.split(":")[2]))
    if net is None:
        return
    await cb.answer("Свожу баны сетки, это займёт время…")
    done, failed = await net_import_run(bot, net["id"], cb.from_user.id)
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


async def view_punishments(cid: int, page: int = 0,
                           full: bool = True) -> tuple[str, InlineKeyboardMarkup]:
    """Главная страница раздела: сводка и действия. Сам список — за кнопкой,
    иначе при десятке наказаний экран превращался в лес кнопок «Снять…»."""
    rows = await db.active_punishments(cid, limit=1000)
    ch = await db.get_chat(cid)
    counts: dict[str, int] = {}
    for r in rows:
        k = utils.shown_kind(r["kind"], r["reason"])
        counts[k] = counts.get(k, 0) + 1
    lines = [
        f"<b>🚫 Наказания</b> · {utils.esc(ch['title'] if ch else str(cid))}\n",
        f"Активных сейчас: <b>{len(rows)}</b>",
    ]
    if counts:
        lines.append(", ".join(f"{_KIND_WORD.get(k, k)} — {n}" for k, n in sorted(counts.items())))
    else:
        lines.append("Все чисты.")
    lines.append("\nМассовые действия принимают список id или @username одним сообщением.")

    # сверху короткое действие, ниже длинные списки: так же, как в панели
    b = InlineKeyboardBuilder()
    b.row(_btn("🔎 Проверка статуса", f"u:ps:{cid}"))
    b.row(_btn("🔓 Массовый разбан", f"u:mub:{cid}"),
          _btn("👢 Массовый кик", f"u:mkick:{cid}"))
    b.row(_btn("⛔ Массовый бан", f"u:mban:{cid}"))
    b.row(_btn(f"📋 Активные: {len(rows)}", f"u:pa:{cid}:0"))
    forgiven = await db.forgiven_count(cid)
    if forgiven:
        b.row(_btn(f"🕊 Прощённые: {forgiven}", f"u:fg:{cid}:0"))
    if full:                      # админу «наказаний» настройки не открыты
        b.row(_btn("⚙️ Настройки", f"u:s:{cid}:punish_cfg"))
    b.row(_btn("⬅️ Назад", f"u:c:{cid}"))
    return "\n".join(lines), b.as_markup()


async def view_spam_profiles(cid: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """База спам-профилей: что записано и кнопка убрать."""
    rows = await db.spam_profiles(cid)
    chunk, page, pages = _page_slice(rows, page)
    lines = [
        "<b>🧪 Спам-профили</b>\n",
        "С этими профилями бот сравнивает новых людей, когда включено "
        "«Сравнивать профили с забаненными». Сюда попадают профили, записанные "
        "кнопкой «Спам-профиль», и те, кого бот забанил сам.",
        "\nЗаписали по ошибке — уберите номером ниже.",
        f"\nВсего: <b>{len(rows)}</b>"
        + (f" · страница {page + 1} из {pages}" if pages > 1 else ""),
        "",
    ]
    if not rows:
        lines.append("Пусто.")
    start = page * LIST_PER_PAGE
    for i, r in enumerate(chunk, start + 1):
        who = await db.user_handle(r["user_id"]) if r["user_id"] else "—"
        lines.append(
            f"{i}. <b>{utils.esc(who)}</b> · {utils.fmt_ts(r['ts'])}\n"
            f"    <i>{utils.esc(utils.chunk(r['text'], 90))}</i>")
    b = InlineKeyboardBuilder()
    row = []
    for i, r in enumerate(chunk, start + 1):
        row.append(_btn(f"❌ {i}", f"u:pfd:{cid}:{r['id']}:{page}"))
        if len(row) == 5:
            b.row(*row)
            row = []
    if row:
        b.row(*row)
    _pager(b, cid, "u:pf", page, pages)
    b.row(_btn("⬅️ Назад", f"u:s:{cid}:watch"))
    return "\n".join(lines), b.as_markup()


@router.callback_query(F.data.startswith("u:pf:"))
async def cb_spam_profiles(cb: CallbackQuery) -> None:
    _, _, cid, page = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    text, kb = await view_spam_profiles(cid, int(page))
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:pfd:"))
async def cb_spam_profile_del(cb: CallbackQuery) -> None:
    from ..services import nn
    _, _, cid, rid, page = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    row = await db.spam_profile_delete(cid, int(rid))
    if row is not None:
        nn.invalidate(cid)
        await db.add_event(cid, "card", f"спам-профиль убран из базы: "
                                        f"{row['user_id']} by {cb.from_user.id}")
    text, kb = await view_spam_profiles(cid, int(page))
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("Убран" if row is not None else "Его уже нет")


async def view_forgiven(cid: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """Кого фильтр наказал зря и кому за это выдали освобождение.

    Список рождается сам — кнопкой на карточке снятого наказания. Смотреть на
    него стоит не как на вайтлист, а как на список ошибок: если тут десяток
    человек по одному правилу, дело не в людях, а в настройке правила.
    """
    rows = await db.forgiven_list(cid)
    chunk, page, pages = _page_slice(rows, page)
    lines = [
        "<b>🕊 Прощённые</b>\n",
        "Эти люди попали под правило зря — вы сняли наказание и решили больше "
        "их этим правилом не трогать. Остальные проверки для них работают.",
        f"\nВсего: <b>{len(rows)}</b>"
        + (f" · страница {page + 1} из {pages}" if pages > 1 else ""),
        "",
    ]
    if not rows:
        lines.append("Пусто — фильтр пока никого зря не тронул.")
    start = page * LIST_PER_PAGE
    for i, r in enumerate(chunk, start + 1):
        who = r["name"] or (f"@{r['username']}" if r["username"] else str(r["user_id"]))
        label = config.WL_SCOPE_LABELS.get(r["scope"], r["scope"])
        why, _swapped = utils.short_reason(r["reason"])
        lines.append(
            f"{i}. <b>{utils.name_link(r['user_id'], utils.chunk(who, 40), r['username'])}</b>"
            f" — {label} · {utils.fmt_ts(r['created'])}\n"
            f"    <i>{utils.esc(utils.chunk(why, 70))}</i>")

    b = InlineKeyboardBuilder()
    row = []
    for i, r in enumerate(chunk, start + 1):
        row.append(_btn(f"❌ {i}", f"u:fgd:{cid}:{r['id']}:{page}"))
        if len(row) == 4:
            b.row(*row)
            row = []
    if row:
        b.row(*row)
    _pager(b, cid, "u:fg", page, pages)
    b.row(_btn("⬅️ Назад", f"u:p:{cid}:0"))
    return "\n".join(lines), b.as_markup()


@router.callback_query(F.data.startswith("u:fg:"))
async def cb_forgiven(cb: CallbackQuery) -> None:
    _, _, cid, page = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid, "punish"):
        return
    text, kb = await view_forgiven(cid, int(page))
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:fgd:"))
async def cb_forgiven_del(cb: CallbackQuery) -> None:
    """Вернуть человека под правило: ошибку признали зря или он изменился."""
    _, _, cid, rid, page = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid, "punish"):
        return
    row = await db.forgiven_get(int(rid))
    await db.forgiven_remove(int(rid))
    if row is not None:
        await db.add_event(cid, "card",
                           f"прощение снято: {row['user_id']} "
                           f"({row['scope']}) by {cb.from_user.id}")
    text, kb = await view_forgiven(cid, int(page))
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("Правило снова работает")


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
        # имя — ссылка на профиль: ник виден по нажатию и не занимает строку
        who_link = utils.name_link(r["user_id"], utils.chunk(who, 40),
                                   r["username"])
        until = ("навсегда" if not r["until_ts"]
                 else f"до {utils.fmt_ts(r['until_ts'])}")
        # дата выдачи нужна не меньше срока: по «навсегда» непонятно, вчера
        # это было или полгода назад
        since = f" · выдан {utils.fmt_ts(r['created'])}" if r["created"] else ""
        # причина без «сетка · чат:» и без приписки про подмену мута: строка
        # одна, и место в ней нужно самой причине
        why, _swapped = utils.short_reason(r["reason"])
        shown = utils.shown_kind(r["kind"], r["reason"])
        lines.append(
            f"{i}. <b>{who_link}</b> — "
            f"{_KIND_WORD.get(shown, shown)} {until}{since}\n"
            f"    <i>{utils.esc(utils.chunk(why, 70))}</i>"
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
    # клавиатуру пикера снимаем только если она реально могла остаться —
    # иначе на каждый /start мелькала бы пустышка
    if await state.get_state() == Input.pick_log.state:
        await _drop_reply_kb(message)
    await state.clear()
    text, kb = await view_home(message.from_user.id, bot)
    await message.answer(text, reply_markup=kb)


@router.message(Command("panel"))
async def cmd_panel(message: Message) -> None:
    """Открыть панель кнопкой в сообщении.

    Кнопка рядом с полем ввода ставится один раз при старте, и клиенты
    подхватывают её только когда перечитают данные бота — то есть после
    переоткрытия чата. Эта команда отдаёт свежий адрес сразу.
    """
    url = runtime.webapp_url()
    if not url:
        await message.answer(
            "Панель сейчас недоступна: публичный адрес не получен. "
            "Проверьте туннель в логах или задайте WEBAPP_URL.")
        return
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🖥 Открыть панель", web_app=WebAppInfo(url=url)))
    await message.answer("Панель — то же меню, но страницей.", reply_markup=b.as_markup())


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
    user_id, username, name = None, None, None
    if text.lstrip("-").isdigit():
        user_id = int(text)
    elif text.startswith("@") and len(text) > 3:
        username = text
        # закрепляем и id, и имя: про человека, который боту ещё не писал,
        # мы больше ничего не знаем, и в списке висел голый ник
        user_id, name = await resolve.by_username(bot, text)
    else:
        await _retry(message, bot, state,
                     "<b>👥 Доступ к боту</b>\n\n⚠️ Нужен числовой id или @username.")
        return
    if user_id and not name:
        row = await db.get_user(user_id)
        name = row["first_name"] if row else None
    await db.access_add(user_id, username, name)
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


async def _guard(cb: CallbackQuery, cid: int, need: str = "settings") -> bool:
    """Хватает ли прав на действие в этом чате.

    Уровни: punish (наказания, статус, жалобы) < settings (разделы модерации)
    < owner (лог-чат, сетки, перенос, удаление бота, список админов).
    Проверяем на каждое действие, а не только при открытии карточки: id чата
    лежит в callback, и его несложно подставить руками.
    """
    if await db.may(cb.from_user.id, cid, need):
        return True
    level = await db.chat_access(cb.from_user.id, cid)
    if level:
        await cb.answer("Это может только владелец чата."
                        if need == "owner" else
                        "Вам открыты только наказания этого чата.",
                        show_alert=True)
    else:
        await cb.answer("Это не ваш чат.", show_alert=True)
    return False


@router.callback_query(F.data.startswith("u:c:"))
async def cb_chat(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid, "punish"):
        return
    await state.clear()
    if await needs_setup(cid, cb.from_user.id):
        # свежий чат — сперва короткая настройка
        text, kb = await view_setup(cid, cb.from_user.id)
    else:
        text, kb = await view_chat(cid, cb.from_user.id, cb.bot)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


_IMPORT_PROMPT = ("<b>📥 Загрузка настроек из файла</b>\n\n"
                  "Пришлите архив, который бот отдал кнопкой «📤 Выгрузить в файл». "
                  "Перед загрузкой покажу, что в нём, и дам выбрать разделы.")


@router.callback_query(F.data.startswith("u:exp:"))
async def cb_export(cb: CallbackQuery, bot: Bot) -> None:
    """Выгрузка: архив приходит сюда же, в личку, отдельным сообщением."""
    from aiogram.types import BufferedInputFile
    from ..services import transfer
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid, "owner"):
        return
    await cb.answer("Собираю файл…")
    try:
        data, stats = await transfer.export_chat(cid)
    except Exception:
        logger.warning("выгрузка настроек %s не удалась", cid, exc_info=True)
        await cb.message.answer("Не получилось собрать файл, посмотрите «🐞 Ошибки».")
        return
    ch = await db.get_chat(cid)
    inside = ", ".join(f"{k} {v}" for k, v in stats.items() if v)
    await bot.send_document(
        cb.from_user.id,
        BufferedInputFile(data, filename=transfer.export_name(cid)),
        caption=(f"📤 Настройки «{utils.esc(ch['title'] if ch else cid)}»"
                 + (f"\n{utils.esc(inside)}" if inside else "")),
    )
    await db.add_event(cid, "card", f"настройки выгружены в файл by {cb.from_user.id}")


@router.callback_query(F.data.startswith("u:imp:"))
async def cb_import(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid, "owner"):
        return
    await _ask(cb, state, Input.import_file, _IMPORT_PROMPT, f"u:cp:{cid}", cid=cid)


@router.message(StateFilter(Input.import_file))
async def import_file_input(message: Message, state: FSMContext, bot: Bot) -> None:
    from ..services import transfer
    cid = (await state.get_data())["cid"]
    # право проверяем заново: между кнопкой и файлом его могли отнять
    if not await db.may(message.from_user.id, cid, "owner"):
        await state.clear()
        return
    doc = message.document
    if doc is None:
        await _retry(message, bot, state,
                     f"{_IMPORT_PROMPT}\n\n⚠️ Нужен файл, а не текст.")
        return
    if (doc.file_size or 0) > transfer.MAX_ARCHIVE:
        await _retry(message, bot, state,
                     f"{_IMPORT_PROMPT}\n\n⚠️ Файл больше 20 МБ.")
        return
    await _edit_menu(message, bot, state, "📥 Читаю файл…", None)
    try:
        buf = await bot.download(doc)
        snap, media = transfer.parse_archive(buf.read())
    except transfer.BadArchive as e:
        await _retry(message, bot, state, f"{_IMPORT_PROMPT}\n\n⚠️ {utils.esc(str(e))}")
        return
    except Exception:
        logger.warning("файл настроек не прочитан", exc_info=True)
        await _retry(message, bot, state,
                     f"{_IMPORT_PROMPT}\n\n⚠️ Не получилось скачать или прочитать файл.")
        return
    if not snap["groups"]:
        await _retry(message, bot, state,
                     f"{_IMPORT_PROMPT}\n\n⚠️ В файле нет ни одного раздела.")
        return
    transfer.stash(message.from_user.id, cid, snap, media)
    picked = set(_copy_scope(message.from_user.id, cid, FROM_FILE) or ())
    await state.set_state(None)               # данные оставляем: там галочки
    await state.update_data(copy_groups=sorted(picked))
    text, kb = await view_copy_pick(cid, FROM_FILE, picked, message.from_user.id)
    await _edit_menu(message, bot, state, text, kb)


@router.callback_query(F.data.startswith("u:cpn:"))
async def cb_setup_skip(cb: CallbackQuery, state: FSMContext) -> None:
    """«Настроить с нуля» — просто помечаем чат настроенным."""
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid, "owner"):
        return
    await state.clear()
    await db.kv_set(setup_key(cid), "1")
    text, kb = await view_chat(cid, cb.from_user.id, cb.bot)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:cp:"))
async def cb_copy_from(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid, "owner"):
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
    if cid == src or not await _guard(cb, cid, "owner") or not await _guard(cb, src, "owner"):
        return
    await state.update_data(copy_groups=list(transfer.shown_groups()))
    text, kb = await view_copy_pick(cid, src, set(transfer.shown_groups()))
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:cpg:"))
async def cb_copy_toggle(cb: CallbackQuery, state: FSMContext) -> None:
    """Галочка раздела: отметить, снять, всё, ничего."""
    from ..services import transfer
    _, _, cid, src, key = cb.data.split(":")
    cid, src = int(cid), int(src)
    if not await _guard(cb, cid, "owner"):
        return
    if src != FROM_FILE and not await _guard(cb, src, "owner"):
        return
    scope = _copy_scope(cb.from_user.id, cid, src)
    if scope is None:
        await cb.answer("Файл уже забыт — пришлите его ещё раз.", show_alert=True)
        return
    data = await state.get_data()
    picked = set(data.get("copy_groups", scope)) & set(scope)
    if key == "__all":
        picked = set(scope)
    elif key == "__none":
        picked = set()
    elif key in scope:
        picked.symmetric_difference_update({key})
    await state.update_data(copy_groups=sorted(picked))
    text, kb = await view_copy_pick(cid, src, picked, cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:cpd:"))
async def cb_copy_do(cb: CallbackQuery, state: FSMContext) -> None:
    from ..services import transfer
    _, _, cid, src = cb.data.split(":")
    cid, src = int(cid), int(src)
    if cid == src or not await _guard(cb, cid, "owner"):
        return
    if src != FROM_FILE and not await _guard(cb, src, "owner"):
        return
    got = transfer.stashed(cb.from_user.id, cid) if src == FROM_FILE else None
    if src == FROM_FILE and got is None:
        await cb.answer("Файл уже забыт — пришлите его ещё раз.", show_alert=True)
        return
    data = await state.get_data()
    picked = (set(data.get("copy_groups", transfer.shown_groups()))
              & set(transfer.shown_groups()))
    await state.clear()
    if not picked:
        await cb.answer("Ничего не отмечено.", show_alert=True)
        return
    await cb.answer("Переношу…")
    try:
        if src == FROM_FILE:
            snap, media = got
            stats = await transfer.apply(cid, snap, picked, media.get)
            transfer.unstash(cb.from_user.id, cid)
        else:
            stats = await transfer.copy_chat(src, cid, picked)
    except Exception:
        logger.warning("перенос %s -> %s не удался", src, cid, exc_info=True)
        await cb.answer("Не вышло перенести, посмотрите «🐞 Ошибки».", show_alert=True)
        return
    await db.kv_set(setup_key(cid), "1")
    moved = ", ".join(f"{k}: {v}" for k, v in stats.items() if v)
    if src == FROM_FILE:
        await db.add_event(cid, "card", f"настройки загружены из файла by "
                                        f"{cb.from_user.id}: {moved or 'пусто'}")
        whence = "из файла"
    else:
        ch = await db.get_chat(src)
        whence = f"из «{utils.esc(ch['title'] if ch else src)}»"
    note = (f"✅ Настройки перенесены {whence}.\n"
            f"{moved or 'нечего было копировать'}.\n\n")
    text, kb = await view_chat(cid, cb.from_user.id, cb.bot)
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
        text, kb = await view_chat(cid, cb.from_user.id, cb.bot)
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

def _trend(now: int, before: int) -> str:
    """Насколько неделя отличается от прошлой. Пусто — сравнивать не с чем."""
    if not before:
        return ""
    diff = round((now - before) / before * 100)
    if abs(diff) < 3:
        return " (как на прошлой неделе)"
    return f" ({'+' if diff > 0 else ''}{diff}% к прошлой)"


@router.callback_query(F.data.startswith("u:st:"))
async def cb_stats(cb: CallbackQuery) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid, "punish"):
        return
    st = await db.chat_stats(cid)
    ch = await db.get_chat(cid)
    lines = [
        f"<b>📈 Статистика</b> · {utils.esc(ch['title'] if ch else str(cid))}\n",
        f"💬 Сообщений: сегодня <b>{st['d1']}</b> · вчера <b>{st['y1']}</b>",
        f"      за 7д <b>{st['d7']}</b>{_trend(st['d7'], st['p7'])} · "
        f"за 30д <b>{st['d30']}</b> · всего <b>{st['total']}</b>",
        f"🗣 Писали за 7д: <b>{st['people7']}</b> "
        f"{utils.plural(st['people7'], 'человек', 'человека', 'человек')}",
        f"👥 За 7 дней: пришло <b>{st['joins']}</b> · ушло <b>{st['leaves']}</b>",
        f"🔨 Наказаний: за 7д <b>{st['pun7']}</b> · за 30д <b>{st['pun30']}</b>",
    ]
    if st["since"]:
        lines.append(f"📅 Считаем с {utils.fmt_ts(st['since'])[:10]}")
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

@router.callback_query(F.data.startswith("u:wmig:"))
async def cb_welcome_migrate(cb: CallbackQuery) -> None:
    """Старое приветствие одним текстом переносим в список заготовок."""
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    s = await db.get_settings(cid)
    if s.welcome_text:
        await db.ans_add("welcome", cid, s.welcome_text)
        await db.set_setting(cid, "welcome_text", None)
    text, kb = await view_section(cid, "welcome")
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("Перенесено")


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
        path = await triggers.save_media(bot, media.file_id, cid, media.kind,
                                         "trig")
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
    cd = utils.fmt_seconds(r["cooldown"]) if r["cooldown"] else "без кулдауна"
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
    if not await _guard(cb, cid, "punish"):
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
    if not await _guard(cb, cid, "punish"):
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
    if not await _guard(cb, cid, "punish"):
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
        # Полсекунды паузы: разбан, отправленный вплотную к бану, иногда
        # приходит раньше, чем бан записан, и с only_if_banned просто ничего
        # не делает — человек остаётся забаненным вместо того, чтобы быть кикнутым
        await asyncio.sleep(0.5)
        await bot.unban_chat_member(cid, uid, only_if_banned=True)   # бан снят = кик
    except Exception as e:
        return "fail", f"{tag} — {_human_error(e)}"

    # проверяем, что получилось: «сделал вид» вместо результата хуже отказа
    try:
        after = await bot.get_chat_member(cid, uid)
    except Exception:
        return "done", tag
    if after.status == "kicked":
        try:                       # разбан не прошёл — человек в бане, доснимаем
            await bot.unban_chat_member(cid, uid, only_if_banned=True)
        except Exception:
            return "fail", f"{tag} — остался в бане, снимите вручную"
    elif after.status not in ("left", "kicked"):
        return "fail", f"{tag} — Telegram не выпустил из чата"
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
            if code != "c" else "Пришлите текст ответа. Число в скобках бот допишет сам.")
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

    media = triggers.extract_media(message) if code != "c" else None
    if media is None and not raw:
        hint = ("⚠️ Нужен текст или медиа." if code != "c"
                else "⚠️ Нужен текст: у счётчиков ответы только текстовые.")
        await _retry(message, bot, state, f"<b>🎲 Новый вариант ответа</b>\n\n{hint}")
        return

    path = None
    if media is not None:
        try:
            path = await triggers.save_media(bot, media.file_id, cid, media.kind,
                                             ANS_OWNER[code])
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


# ---------- админы чата в боте ----------
#
# Владелец пускает своих админов в меню. Уровень «Наказания» — списки, снятие,
# проверка статуса и массовые действия; «Настройки» — ещё и разделы модерации.
# Владельческое остаётся владельцу, иначе доступ раздавался бы по кругу.

_LEVEL_HINT = {
    "punish": "наказания, проверка статуса, массовые действия",
    "settings": "всё, кроме владельческого",
}


def _admin_name(row) -> str:
    return (row["name"] or (f"@{row['username']}" if row["username"]
                            else str(row["user_id"])))


async def view_chat_admins(cid: int) -> tuple[str, InlineKeyboardMarkup]:
    ch = await db.get_chat(cid)
    rows = await db.chat_admin_list(cid)
    lines = [
        f"<b>👮 Админы в боте</b> · {utils.esc(ch['title'] if ch else str(cid))}\n",
        "Кого пустить в меню бота по этому чату. Добавлять можно только админов "
        "самого чата — бот проверяет это при добавлении.\n",
        "<b>Наказания</b> — списки, снятие, проверка статуса, массовые действия.",
        "<b>Настройки</b> — то же плюс все разделы модерации.",
        "Лог-чат, сетки, перенос настроек, удаление бота и этот список "
        "остаются только у вас.\n",
    ]
    if not rows:
        lines.append("Пока никого.")
    for i, r in enumerate(rows, 1):
        lines.append(
            f"{i}. <b>{utils.name_link(r['user_id'], utils.chunk(_admin_name(r), 40), r['username'])}</b>"
            f" — {db.LEVEL_NAMES[r['level']]}\n"
            f"    <i>{_LEVEL_HINT[r['level']]} · с {utils.fmt_ts(r['created'])}</i>")

    b = InlineKeyboardBuilder()
    for r in rows:
        b.row(_btn(f"🔁 {utils.chunk(_admin_name(r), 20)}: "
                   f"{db.LEVEL_NAMES[r['level']]}", f"u:cal:{cid}:{r['user_id']}"),
              _btn("🗑", f"u:cad:{cid}:{r['user_id']}"))
    b.row(_btn("➕ Добавить", f"u:caa:{cid}"))
    b.row(_btn("⬅️ Назад", f"u:c:{cid}"))
    return "\n".join(lines), b.as_markup()


_CA_PROMPT = ("Кого пустить в меню по этому чату?\n\n"
              "Пришлите id, @username или перешлите его сообщение. "
              "Человек должен быть админом чата.\n"
              "Уровень по умолчанию — «Наказания», поменять можно кнопкой.")


@router.callback_query(F.data.startswith("u:ca:"))
async def cb_chat_admins(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid, "owner"):
        return
    await state.clear()
    text, kb = await view_chat_admins(cid)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:caa:"))
async def cb_chat_admin_add(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid, "owner"):
        return
    await _ask(cb, state, Input.chat_admin, _CA_PROMPT, f"u:ca:{cid}", cid=cid)


@router.message(StateFilter(Input.chat_admin))
async def chat_admin_input(message: Message, state: FSMContext, bot: Bot) -> None:
    from ..services import adm_cache, status as status_svc
    cid = (await state.get_data())["cid"]
    uid, err = await status_svc.parse_target(
        bot, message.text or message.caption, message)
    if uid is None:
        await _retry(message, bot, state, f"{_CA_PROMPT}\n\n⚠️ {utils.esc(err)}")
        return
    if uid == message.from_user.id:
        await _retry(message, bot, state,
                     f"{_CA_PROMPT}\n\n⚠️ Это вы, у вас и так все права.")
        return
    # доступ к чужому чату не должен появляться из ниоткуда: пускаем только
    # тех, кому владелец уже доверил админку в самом Telegram
    if uid not in await adm_cache.chat_admin_ids(bot, cid):
        await _retry(message, bot, state,
                     f"{_CA_PROMPT}\n\n⚠️ Этот человек не админ чата. "
                     f"Сначала выдайте права в самом Telegram.")
        return
    name = username = None
    try:
        member = await bot.get_chat_member(cid, uid)
        name = member.user.full_name
        username = member.user.username
    except Exception:
        logger.debug("имя админа %s не узнать", uid, exc_info=True)
    await db.chat_admin_add(cid, uid, "punish", username, name,
                            message.from_user.id)
    await db.add_event(cid, "card",
                       f"доступ в бот: {uid} (наказания) by {message.from_user.id}")
    await _done(message, bot, state, await view_chat_admins(cid))


@router.callback_query(F.data.startswith("u:cal:"))
async def cb_chat_admin_level(cb: CallbackQuery) -> None:
    """Переключить уровень: наказания ⇄ настройки."""
    _, _, cid, uid = cb.data.split(":")
    cid, uid = int(cid), int(uid)
    if not await _guard(cb, cid, "owner"):
        return
    now = await db.chat_admin_level(cid, uid)
    if now is None:
        await cb.answer("Его уже нет в списке.", show_alert=True)
    else:
        new = "settings" if now == "punish" else "punish"
        await db.chat_admin_add(cid, uid, new, None, None, cb.from_user.id)
        await db.add_event(cid, "card",
                           f"доступ в бот: {uid} ({new}) by {cb.from_user.id}")
        await cb.answer(f"Теперь: {db.LEVEL_NAMES[new]}")
    text, kb = await view_chat_admins(cid)
    await cb.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("u:cad:"))
async def cb_chat_admin_del(cb: CallbackQuery) -> None:
    _, _, cid, uid = cb.data.split(":")
    cid, uid = int(cid), int(uid)
    if not await _guard(cb, cid, "owner"):
        return
    await db.chat_admin_remove(cid, uid)
    await db.add_event(cid, "card", f"доступ в бот снят: {uid} by {cb.from_user.id}")
    text, kb = await view_chat_admins(cid)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("Доступ снят")

@router.callback_query(F.data.startswith("u:logsel:"))
async def cb_log_select(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid, "owner"):
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
    if cid == 0:
        view = await view_chats(bot, message.from_user.id)
    elif await needs_setup(cid, message.from_user.id):
        # человек ещё в короткой настройке — вернём его туда, а не в раздел
        view = await view_setup(cid, message.from_user.id)
    else:
        view = await view_section(cid, "cards")
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
    if not await _guard(cb, cid, "punish"):
        return
    full = await db.may(cb.from_user.id, cid)
    text, kb = await view_punishments(cid, int(page), full)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:ps:"))
async def cb_status_check(cb: CallbackQuery, state: FSMContext) -> None:
    """Проверка статуса: ждём, кого смотреть. Ответ придёт в это же сообщение."""
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid, "punish"):
        return
    from ..services import status as status_svc
    await _ask(cb, state, Input.status, status_svc.PROMPT, f"u:p:{cid}:0", cid=cid)


@router.message(StateFilter(Input.status))
async def status_input(message: Message, state: FSMContext, bot: Bot) -> None:
    from ..services import status as status_svc
    cid = (await state.get_data())["cid"]
    uid, err = await status_svc.parse_target(
        bot, message.text or message.caption, message)
    if uid is None:
        await _retry(message, bot, state,
                     f"{status_svc.PROMPT}\n\n⚠️ {utils.esc(err)}")
        return
    # по запросу в Telegram на чат, плюс юзербот — это пара секунд, и без
    # пометки кажется, что бот ввод проглотил
    await _edit_menu(message, bot, state, "🔎 Смотрю…", None)
    # только чаты спрашивающего: чужие владельцу показывать нельзя
    chats = await db.chats_for(message.from_user.id)
    d = await status_svc.collect(bot, uid, chats, first=cid)
    b = InlineKeyboardBuilder()
    # база спам-профилей своя у каждого чата, а проверка смотрит все —
    # пишем в кнопке, куда именно ляжет запись
    here = await db.get_chat(cid)
    title = (here["title"] if here is not None else "") or ""
    title = title if len(title) <= 24 else title[:23] + "…"
    b.row(_btn(f"🧪 Спам-профиль в «{title}»" if title else "🧪 Спам-профиль",
               f"u:spp:{cid}:{uid}"))
    b.row(_btn("🔎 Проверить другого", f"u:ps:{cid}"))
    b.row(_btn("⬅️ Назад", f"u:p:{cid}:0"))
    await _edit_menu(message, bot, state, status_svc.render(d), b.as_markup())
    await state.clear()


@router.callback_query(F.data.startswith("u:spp:"))
async def cb_status_spam(cb: CallbackQuery, bot: Bot) -> None:
    """«Спам-профиль» в карточке проверки: профиль — в базу этого чата."""
    _, _, cid, uid = cb.data.split(":")
    cid, uid = int(cid), int(uid)
    if not await _guard(cb, cid, "punish"):
        return
    from ..services import nn
    ok, note = await nn.remember_spam_profile(bot, cid, uid, cb.from_user.id)
    if ok:
        await db.add_event(cid, "card", f"спам-профиль в базу: {uid} "
                                        f"by {cb.from_user.id} (проверка статуса)")
        markup = cb.message.reply_markup
        if markup is not None:
            rows = [row for row in markup.inline_keyboard
                    if not any(btn.callback_data == cb.data for btn in row)]
            try:
                await cb.message.edit_reply_markup(
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
            except Exception:
                pass
    await cb.answer(note, show_alert=True)


@router.callback_query(F.data.startswith("u:pa:"))
async def cb_active(cb: CallbackQuery, state: FSMContext) -> None:
    _, _, cid, page = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid, "punish"):
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
    if not await _guard(cb, cid, "punish"):
        return
    # ссылку на возврат не делаем: она нужна только в карточке лог-чата
    p = await db.get_punishment(pid)
    ok, msg, _ = await moderation.lift_punishment(bot, pid, invite=False)
    if ok and p is not None:
        from ..services import net
        runtime.spawn(net.lift(bot, p["chat_id"], p["user_id"]))
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
    if not await _guard(cb, cid, "owner"):
        return
    b = InlineKeyboardBuilder()
    b.button(text="✅ Да, выйти", callback_data=f"a:leave_yes:{cid}")
    b.button(text="⬅️ Отмена", callback_data=f"u:c:{cid}")
    b.adjust(2)
    s = await db.get_settings(cid)
    tail = ""
    if s.log_chat_id:
        reason = await db.log_chat_still_needed(s.log_chat_id, cid)
        tail = (f"\nЛог-чат <code>{s.log_chat_id}</code> оставлю: {reason}." if reason
                else f"\nИз лог-чата <code>{s.log_chat_id}</code> бот выйдет тоже.")
    await cb.message.edit_text(
        f"Точно покинуть чат <code>{cid}</code>?" + tail, reply_markup=b.as_markup()
    )
    await cb.answer()


@router.callback_query(F.data.startswith("a:leave_yes:"))
async def cb_leave_yes(cb: CallbackQuery, bot: Bot) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid, "owner"):
        return
    from ..services import moderation
    ok, note = await moderation.leave_chat(bot, cid)
    if not ok:
        await cb.answer(f"Не вышло: {note}", show_alert=True)
        return
    await cb.message.edit_text("✅ Бот покинул чат." + note, reply_markup=_home_kb())
    await cb.answer()


# ---------- удаление из списков ----------

@router.callback_query(F.data.startswith("u:wld:"))
async def cb_wl_del(cb: CallbackQuery) -> None:
    """Убрать объект из вайтлиста целиком — со всеми его уровнями."""
    parts = cb.data.split(":")
    cid, rid = int(parts[2]), int(parts[3])
    page = int(parts[4]) if len(parts) > 4 else 0
    if not await _guard(cb, cid):
        return
    e = await db.wl_entry(cid, rid)
    if e is not None:
        await db.wl_set_scopes(cid, e["user_id"], e["username"], e["title"], set())
    text, kb = await view_wl(cid, page)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("Удалено")


@router.callback_query(F.data.startswith("u:wle:"))
async def cb_wl_entry(cb: CallbackQuery, state: FSMContext) -> None:
    parts = cb.data.split(":")
    cid, rid = int(parts[2]), int(parts[3])
    page = int(parts[4]) if len(parts) > 4 else 0
    if not await _guard(cb, cid):
        return
    await state.clear()
    view = await view_wl_entry(cid, rid, page)
    if view is None:
        text, kb = await view_wl(cid, page)
        await cb.message.edit_text(text, reply_markup=kb)
        await cb.answer("Запись не найдена", show_alert=True)
        return
    await cb.message.edit_text(view[0], reply_markup=view[1])
    await cb.answer()


@router.callback_query(F.data.startswith("u:wlt:"))
async def cb_wl_toggle(cb: CallbackQuery, bot: Bot) -> None:
    """Галочка уровня. «Полный игнор» — тумблер над всеми остальными."""
    from ..services import moderation
    parts = cb.data.split(":")
    cid, rid, scope = int(parts[2]), int(parts[3]), parts[4]
    page = int(parts[5]) if len(parts) > 5 else 0
    if not await _guard(cb, cid):
        return
    e = await db.wl_entry(cid, rid)
    if e is None:
        await _show_wl(cb, cid, page)
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
        await _show_wl(cb, cid, page)
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
    view = await view_wl_entry(cid, fresh["row_id"], page) if fresh else None
    if view is None:
        await _show_wl(cb, cid, page)
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
        # запись могли завести по нику, когда id был неизвестен: привязываем его,
        # иначе после смены ника игнор перестанет действовать
        if user_id and exists["user_id"] is None and exists["username"]:
            await db.wl_attach_id(cid, exists["username"], user_id, title)
            exists = await db.wl_entry_by_key(cid, user_id, uname)
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
    cd = utils.fmt_seconds(r["cooldown"]) if r["cooldown"] else "без кулдауна"
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

    added = await db.link_wl_add(cid, target_id, uname, title)
    who = title or (f"@{uname}" if uname else str(target_id))
    if not added:
        note = f"ℹ️ {utils.esc(who)} уже в списке разрешённых.\n\n"
    elif target_id:
        note = f"✅ {utils.esc(who)} разрешён.\n\n"
    else:
        note = f"✅ {utils.esc(who)} разрешён (id не определился — сверяю по нику).\n\n"
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
    added = await db.inline_wl_add(cid, uname, bot_id)
    note = (f"✅ @{utils.esc(uname)} разрешён.\n\n" if added
            else f"ℹ️ @{utils.esc(uname)} уже в списке.\n\n")
    await _done(message, bot, state, await view_section(cid, "inline"), note)


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

@router.callback_query(F.data.startswith("u:ph:"))
async def cb_phrases(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    await state.clear()
    text, kb = await view_phrases(cid)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:phd:"))
async def cb_phrase_del(cb: CallbackQuery) -> None:
    _, _, cid, rid = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    await db.phrase_del(cid, int(rid))
    nn.invalidate_phrases(cid)
    text, kb = await view_phrases(cid)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("Удалено")


@router.callback_query(F.data.startswith("u:pha:"))
async def cb_phrase_add(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    await _ask(
        cb, state, Input.phrase,
        "<b>🧠 Смысловые стоп-слова</b>\n\nПришлите фразу-образец — так, как "
        "пишут спамеры. Можно несколько, каждую с новой строки.\n\n"
        "Например: <code>заработок от 5000 в день, пиши в личку</code>",
        f"u:ph:{cid}", cid=cid,
    )


@router.message(StateFilter(Input.phrase))
async def phrase_input(message: Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or "").strip()
    data = await state.get_data()
    cid = data["cid"]
    if text == "/cancel":
        await _done(message, bot, state, await view_phrases(cid))
        return
    have = len(await db.phrases_list(cid))
    added = dupes = 0
    for raw in text.split("\n"):
        line = raw.strip()
        if len(line) < 10:              # из трёх слов смысла не выжать
            continue
        if have + added >= config.SEM_LIMIT:
            break
        if await db.phrase_add(cid, line):
            added += 1
        else:
            dupes += 1
    if not added and not dupes:
        await _retry(message, bot, state,
                     "<b>🧠 Смысловые стоп-слова</b>\n\n⚠️ Нужна фраза, а не пара слов "
                     "— от десяти символов.")
        return
    nn.invalidate_phrases(cid)
    note = f"✅ Добавлено фраз: {added}."
    if dupes:
        note += f" Уже были: {dupes}."
    await _done(message, bot, state, await view_phrases(cid), note + "\n\n")


@router.callback_query(F.data.startswith("u:nnd:"))
async def cb_doubt(cb: CallbackQuery) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    await cb.answer("Считаю…")
    text, kb = await view_doubt(cid)
    await cb.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("u:nnm:"))
async def cb_doubt_mark(cb: CallbackQuery) -> None:
    _, _, cid, sid, label = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    await db.sample_relabel(int(sid), label, origin="card", labeled_by=cb.from_user.id)
    nn.invalidate(cid)
    await db.add_event(cid, "nn", f"улика {sid} размечена как {label} "
                                  f"by {cb.from_user.id}")
    text, kb = await view_doubt(cid)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("Размечено")


@router.callback_query(F.data.startswith("u:nnt:"))
async def cb_threshold_apply(cb: CallbackQuery) -> None:
    """Принять рекомендованный порог одним нажатием."""
    _, _, cid, value = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    await db.set_setting(cid, "nn_threshold", int(value))
    await _rerender(cb, cid, "nn")
    await cb.answer(f"Порог: {value}%")


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


# ---------- стартовый набор (только владелец бота) ----------
#
# Набор общий на весь бот: один и тот же список примеров подмешивается всем
# молодым чатам. Поэтому и правит его владелец, а не хозяин отдельного чата —
# удалил пример здесь, он пропал сразу везде.

SEED_PER_PAGE = 5

# что человек сейчас смотрит: user_id -> (метка, поисковое слово, страница, вид).
# В callback_data это не влезает — там 64 байта, а слово бывает любым.
_seed_view: dict[int, tuple[str | None, str | None, int, str]] = {}

_SEED_LABELS = {None: "все", "spam": "⛔ только спам", "ok": "🕊 только норма"}
# вид улики: сообщения сравниваются с сообщениями, профили с профилями
_SEED_KINDS = {"msg": "📨 Сообщения", "prof": "🪪 Профили"}


def _seed_state(uid: int) -> tuple[str | None, str | None, int, str]:
    return _seed_view.get(uid, (None, None, 0, "msg"))


async def _seed_admin(cb: CallbackQuery) -> bool:
    """Набор общий, поэтому правит его только владелец бота."""
    if cb.from_user.id in config.ADMIN_IDS:
        return True
    await cb.answer("Стартовый набор общий для всех чатов — его правит "
                    "владелец бота.", show_alert=True)
    return False


async def view_seed(uid: int) -> tuple[str, InlineKeyboardMarkup]:
    """Главный экран набора: сколько чего и что с этим можно сделать."""
    msg = await db.seed_stats("msg")
    prof = await db.seed_stats("prof")
    pool = await db.pool_stats()
    vecs = await db.seed_vec_count()
    label, q, _page, kind = _seed_state(uid)

    lines = [
        "<b>🌱 Стартовый набор</b>\n",
        "Примеры из сборщика. Вместе с размеченным в чатах это одна копилка: "
        "по ней учится нейрофильтр и сравниваются профили во всех чатах сразу. "
        "Два вида, и они не смешиваются: сообщение сравнивается с сообщениями, "
        "профиль с профилями.",
        "",
        "Удалили пример здесь — он пропал у всех чатов сразу.",
        "",
        "<b>📨 Сообщения</b>",
        f"⛔ Спам: <b>{msg['spam']}</b> · 🕊 Норма: <b>{msg['ok']}</b>",
        f"Вся копилка: ⛔ {pool['msg']['spam']} · 🕊 {pool['msg']['ok']} "
        f"(разметил человек {pool['msg']['human']}, бот {pool['msg']['bot']})",
        "",
        "<b>🪪 Профили</b>",
        f"⛔ Спам: <b>{prof['spam']}</b> · 🕊 Норма: <b>{prof['ok']}</b>",
        f"Вся копилка: ⛔ {pool['prof']['spam']} · 🕊 {pool['prof']['ok']} "
        f"(разметил человек {pool['prof']['human']}, бот {pool['prof']['bot']})",
        "",
        f"🧮 Посчитано векторов: <b>{vecs}</b>",
    ]
    if q:
        found = await db.seed_count(label, q, kind)
        lines += ["", f"🔎 Найдено по «<code>{utils.esc(q)}</code>» среди "
                      f"{_SEED_KINDS[kind].lower()}: <b>{found}</b>"]

    b = InlineKeyboardBuilder()
    b.row(*[_btn(("• " if kind == k else "") + name, f"u:seedk:{k}")
            for k, name in _SEED_KINDS.items()])
    b.row(_btn(f"📋 Смотреть: {_SEED_LABELS[label]}", "u:seedl:0"))
    b.row(_btn("🔎 Найти по слову", "u:seedq"))
    if q:
        b.row(_btn(f"❌ Удалить найденное по «{q[:18]}»", "u:seedw"))
        b.row(_btn("✖️ Сбросить поиск", "u:seedr"))
    b.row(_btn(f"🧹 Очистить: {_SEED_KINDS[kind].lower()}", "u:seedx"))
    b.row(_btn("⬅️ Назад", "u:home"))
    return "\n".join(lines), b.as_markup()


async def view_seed_list(uid: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """Постранично сам список: текст примера плюс кнопка «удалить»."""
    label, q, _page, kind = _seed_state(uid)
    total = await db.seed_count(label, q, kind)
    pages = max(1, -(-total // SEED_PER_PAGE))
    page = max(0, min(page, pages - 1))
    _seed_view[uid] = (label, q, page, kind)
    rows = await db.seed_page(label, q, page * SEED_PER_PAGE, SEED_PER_PAGE, kind)

    head = f"<b>🌱 Набор</b> · {_SEED_KINDS[kind]} · {_SEED_LABELS[label]}"
    if q:
        head += f" · поиск «{utils.esc(q)}»"
    lines = [head + "\n",
             f"Всего: <b>{total}</b>"
             + (f" · страница {page + 1} из {pages}" if pages > 1 else ""),
             ""]
    b = InlineKeyboardBuilder()
    if not rows:
        lines.append("Ничего не нашлось.")
    for i, r in enumerate(rows, page * SEED_PER_PAGE + 1):
        mark = "⛔" if r["label"] == "spam" else "🕊"
        body = utils.esc(utils.chunk(" ".join(r["text"].split()), 160))
        lines.append(f"<b>{i}.</b> {mark}")
        lines.append(f"<blockquote>{body}</blockquote>")
        b.row(_btn(f"❌ Удалить {i}", f"u:seedd:{r['id']}"))

    if pages > 1:
        b.row(
            _btn("⬅️", f"u:seedl:{page - 1 if page else pages - 1}"),
            _btn(f"{page + 1}/{pages}", f"u:seedl:{page}"),
            _btn("➡️", f"u:seedl:{page + 1 if page + 1 < pages else 0}"),
        )
    # фильтр по метке: чаще всего выкидывают именно чужую «норму».
    # У профилей «нормы» не бывает, поэтому там фильтр не нужен
    if kind == "msg":
        b.row(*[_btn(("• " if label == key else "") + name, f"u:seedf:{key or 'all'}")
                for key, name in _SEED_LABELS.items()])
    b.row(_btn("⬅️ Назад", "u:seed"))
    return "\n".join(lines), b.as_markup()


def _seed_changed() -> None:
    """Набор правили — профили всех чатов, где он подмешан, устарели."""
    from ..services import nn
    nn.invalidate()


@router.callback_query(F.data == "u:seed")
async def cb_seed(cb: CallbackQuery) -> None:
    if not await _seed_admin(cb):
        return
    text, kb = await view_seed(cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:seedl:"))
async def cb_seed_list(cb: CallbackQuery) -> None:
    if not await _seed_admin(cb):
        return
    page = int(cb.data.split(":")[2])
    text, kb = await view_seed_list(cb.from_user.id, page)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:seedk:"))
async def cb_seed_kind(cb: CallbackQuery) -> None:
    """Переключить вид улик: сообщения или профили."""
    if not await _seed_admin(cb):
        return
    kind = cb.data.split(":")[2]
    label, q, _p, _k = _seed_state(cb.from_user.id)
    # у профилей «нормы» не бывает — сбрасываем фильтр, иначе список пуст
    _seed_view[cb.from_user.id] = (None if kind == "prof" else label, q, 0, kind)
    text, kb = await view_seed(cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer(_SEED_KINDS[kind])


@router.callback_query(F.data.startswith("u:seedf:"))
async def cb_seed_filter(cb: CallbackQuery) -> None:
    if not await _seed_admin(cb):
        return
    key = cb.data.split(":")[2]
    label = None if key == "all" else key
    _, q, _p, kind = _seed_state(cb.from_user.id)
    _seed_view[cb.from_user.id] = (label, q, 0, kind)
    text, kb = await view_seed_list(cb.from_user.id, 0)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:seedd:"))
async def cb_seed_delete(cb: CallbackQuery) -> None:
    if not await _seed_admin(cb):
        return
    sid = int(cb.data.split(":")[2])
    gone = await db.seed_delete([sid])
    if gone:
        _seed_changed()
        await db.add_event(None, "nn", f"из набора удалён пример #{sid} "
                                       f"by {cb.from_user.id}")
    _l, _q, page, _k = _seed_state(cb.from_user.id)
    text, kb = await view_seed_list(cb.from_user.id, page)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("Удалено" if gone else "Уже удалено")


@router.callback_query(F.data == "u:seedr")
async def cb_seed_reset(cb: CallbackQuery) -> None:
    if not await _seed_admin(cb):
        return
    label, _q, _p, kind = _seed_state(cb.from_user.id)
    _seed_view[cb.from_user.id] = (label, None, 0, kind)
    text, kb = await view_seed(cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("Поиск сброшен")


@router.callback_query(F.data == "u:seedq")
async def cb_seed_search_ask(cb: CallbackQuery, state: FSMContext) -> None:
    if not await _seed_admin(cb):
        return
    await _ask(
        cb, state, Input.seed_q,
        "<b>🔎 Поиск в наборе</b>\n\nПришлите слово или кусок фразы — покажу "
        "все примеры, где оно встречается, и предложу удалить их разом.\n"
        "Например: <code>docker</code>, <code>ядро</code>, <code>systemd</code>.",
        "u:seed",
    )


@router.message(StateFilter(Input.seed_q))
async def seed_search_input(message: Message, state: FSMContext, bot: Bot) -> None:
    q = " ".join((message.text or "").split())[:100]
    uid = message.from_user.id
    if q == "/cancel" or not q:
        await _done(message, bot, state, await view_seed(uid))
        return
    label, _q, _p, kind = _seed_state(uid)
    _seed_view[uid] = (label, q, 0, kind)
    await _done(message, bot, state, await view_seed_list(uid, 0))


@router.callback_query(F.data == "u:seedw")
async def cb_seed_wipe_ask(cb: CallbackQuery) -> None:
    if not await _seed_admin(cb):
        return
    label, q, _p, kind = _seed_state(cb.from_user.id)
    if not q:
        await cb.answer("Сначала найдите что-нибудь.", show_alert=True)
        return
    found = await db.seed_count(label, q, kind)
    b = InlineKeyboardBuilder()
    b.row(_btn(f"❌ Да, удалить {found}", "u:seedwy"))
    b.row(_btn("⬅️ Отмена", "u:seed"))
    await cb.message.edit_text(
        f"<b>❌ Удалить найденное</b>\n\nПо «<code>{utils.esc(q)}</code>» "
        f"({_SEED_LABELS[label]}) нашлось <b>{found}</b> примеров.\n"
        "Они пропадут из набора у всех чатов. Отменить будет нельзя — "
        "набор придётся загружать заново.",
        reply_markup=b.as_markup())
    await cb.answer()


@router.callback_query(F.data == "u:seedwy")
async def cb_seed_wipe(cb: CallbackQuery) -> None:
    if not await _seed_admin(cb):
        return
    label, q, _p, kind = _seed_state(cb.from_user.id)
    gone = await db.seed_delete_where(label, q, kind)
    if gone:
        _seed_changed()
        await db.add_event(None, "nn", f"из набора удалено по «{q}»: {gone} "
                                       f"by {cb.from_user.id}")
    _seed_view[cb.from_user.id] = (label, None, 0, kind)
    text, kb = await view_seed(cb.from_user.id)
    await cb.message.edit_text(f"✅ Удалено примеров: {gone}.\n\n" + text,
                               reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data == "u:seedx")
async def cb_seed_clear_ask(cb: CallbackQuery) -> None:
    if not await _seed_admin(cb):
        return
    _l, _q, _p, kind = _seed_state(cb.from_user.id)
    st = await db.seed_stats(kind)
    b = InlineKeyboardBuilder()
    b.row(_btn(f"🧹 Да, удалить все {st['total']}", "u:seedxy"))
    b.row(_btn("⬅️ Отмена", "u:seed"))
    await cb.message.edit_text(
        f"<b>🧹 Очистить: {_SEED_KINDS[kind].lower()}</b>\n\nБудут удалены "
        f"все <b>{st['total']}</b> примеров этого вида. Второй вид останется "
        "как был.\n"
        "Загрузить сообщения заново можно с машины "
        "(<code>python tools/import_dataset.py файл</code>), профили — только "
        "заново собрать сборщиком.",
        reply_markup=b.as_markup())
    await cb.answer()


@router.callback_query(F.data == "u:seedxy")
async def cb_seed_clear(cb: CallbackQuery) -> None:
    if not await _seed_admin(cb):
        return
    _l, _q, _p, kind = _seed_state(cb.from_user.id)
    gone = await db.seed_delete_where(None, None, kind)
    _seed_changed()
    await db.add_event(None, "nn", f"стартовый набор ({kind}) очищен: {gone} "
                                   f"by {cb.from_user.id}")
    _seed_view[cb.from_user.id] = (None, None, 0, kind)
    text, kb = await view_seed(cb.from_user.id)
    await cb.message.edit_text(f"🧹 Удалено примеров: {gone}.\n\n" + text,
                               reply_markup=kb)
    await cb.answer()


# ---------- канал для проверки подписки ----------

@router.callback_query(F.data.startswith("u:subch:"))
async def cb_sub_channel(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    await _ask(
        cb, state, Input.sub_chat,
        "<b>📣 Канал для проверки подписки</b>\n\n"
        "Пришлите @юзернейм канала или его id.\n"
        "Чтобы вернуть привязанный к чату канал, пришлите <code>-</code>.\n\n"
        "Бот должен быть админом в этом канале — иначе он не сможет спросить, "
        "подписан ли человек.",
        f"u:s:{cid}:sub", cid=cid,
    )


@router.message(StateFilter(Input.sub_chat))
async def sub_chat_input(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    cid = data["cid"]
    raw = (message.text or "").strip()
    if raw == "/cancel":
        await _done(message, bot, state, await view_section(cid, "sub"))
        return
    if raw == "-":
        await db.set_setting(cid, "sub_chat_id", 0)
        _forget_sub_access(cid)
        await _done(message, bot, state, await view_section(cid, "sub"),
                    "✅ Снова смотрим на привязанный канал.\n\n")
        return

    target = None
    if raw.lstrip("-").isdigit():
        target = int(raw)
    elif raw.startswith("@") and len(raw) > 2:
        target = raw
    if target is None:
        await _retry(message, bot, state,
                     "<b>📣 Канал</b>\n\n⚠️ Нужен @юзернейм, id или «-».")
        return
    try:
        ch = await bot.get_chat(target)
    except Exception as e:
        await _retry(message, bot, state,
                     f"<b>📣 Канал</b>\n\n⚠️ Не открылся: {utils.esc(str(e))}\n"
                     "Проверьте, что бот добавлен в канал администратором.")
        return
    if ch.type not in ("channel", "supergroup", "group"):
        await _retry(message, bot, state,
                     "<b>📣 Канал</b>\n\n⚠️ Это не канал и не группа.")
        return
    await db.set_setting(cid, "sub_chat_id", ch.id)
    _forget_sub_access(cid)
    name = utils.esc(ch.title or str(ch.id))
    await _done(message, bot, state, await view_section(cid, "sub"),
                f"✅ Канал: {name}\n\n")


# ---------- слова для профилей ----------

@router.callback_query(F.data.startswith("u:pw:"))
async def cb_prof_words(cb: CallbackQuery) -> None:
    _, _, cid, page = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    text, kb = await view_prof_words(cid, int(page))
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("u:pwd:"))
async def cb_prof_word_del(cb: CallbackQuery) -> None:
    _, _, cid, page, rid = cb.data.split(":")
    cid = int(cid)
    if not await _guard(cb, cid):
        return
    await db.words_remove(int(rid))
    flt.invalidate_words(cid)
    text, kb = await view_prof_words(cid, int(page))
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("Удалено")


@router.callback_query(F.data.startswith("u:pwa:"))
async def cb_prof_word_add(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    await _ask(
        cb, state, Input.prof_words,
        "<b>📝 Слова для профилей</b>\n\nПришлите слова через запятую или с "
        "новой строки.\n<code>слово</code> — точное совпадение, "
        "<code>слово*</code> — с любыми окончаниями.",
        f"u:pw:{cid}:0", cid=cid,
    )


@router.message(StateFilter(Input.prof_words))
async def prof_words_input(message: Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or "").strip()
    data = await state.get_data()
    cid = data["cid"]
    if text == "/cancel":
        await _done(message, bot, state, await view_prof_words(cid, 0))
        return
    added, dupes = 0, 0
    for raw in text.replace("\n", ",").split(","):
        w = raw.strip().lower()
        if not w:
            continue
        mode = "stem" if w.endswith("*") else "strict"
        w = w.rstrip("*")
        if w:
            if await db.words_add(cid, w, mode, "prof"):
                added += 1
            else:
                dupes += 1
    if not added and not dupes:
        await _retry(message, bot, state,
                     "<b>📝 Слова для профилей</b>\n\n⚠️ Не нашёл ни одного слова.")
        return
    flt.invalidate_words(cid)
    note = f"✅ Добавлено: {added}."
    if dupes:
        note += f" Уже были: {dupes}."
    await _done(message, bot, state, await view_prof_words(cid, 0), note + "\n\n")


@router.callback_query(F.data.startswith("u:subwhy:"))
async def cb_sub_why(cb: CallbackQuery) -> None:
    """Перепроверить доступ к каналу прямо сейчас."""
    cid = int(cb.data.split(":")[2])
    if not await _guard(cb, cid):
        return
    from ..services import subscribe as sub_svc
    sub_svc.forget_access(cid)          # спрашиваем заново, а не из кэша
    await cb.answer("Проверяю…")
    text, kb = await view_section(cid, "sub")
    await cb.message.edit_text(text, reply_markup=kb)


async def _sub_state(cid: int, s) -> str:
    """Строка состояния под разделом «Вход по подписке».

    Проверяем доступ бота к каналу прямо при открытии: раньше поломка
    вылезала только после первой заявки, а до тех пор раздел выглядел
    работающим.
    """
    from ..services import subscribe as sub_svc
    lines = []
    bot = runtime.bot()
    if bot is not None:
        why, ok = await sub_svc.access_state(bot, cid, s)
        lines.append(("\n\n✅ " if ok else "\n\n⚠️ ") + utils.esc(why))
    if s.sub_action == "hold":
        if not s.sub_dm:
            lines.append("\n⚠️ «Держать и ждать» без сообщения в личку не "
                         "работает: человек не узнает, что от него хотят, и "
                         "заявка просто повиснет.")
        elif not await db.ans_list("sub", cid):
            lines.append("\n⚠️ Заготовка сообщения не задана — отправлять "
                         "нечего, и заявка повиснет молча.")
    else:
        lines.append("\n📄 Отказ молчаливый: человек ничего не получит, "
                     "вам придёт карточка в лог-чат.")
    if s.sub_pass == "decline":
        lines.append("\n🔒 Вход закрыт всем: заявки отклоняются, даже если "
                     "человек подписан. Чтобы открыть, поставьте «Подписан: "
                     "впустить».")
    elif s.sub_pass == "skip":
        lines.append("\n🙅 Подписанных бот не впускает сам — их заявки висят "
                     "и ждут вас. Карточку по ним не шлём: заявка и так на виду.")
    elif s.sub_pass == "button":
        lines.append("\n🔘 Сам бот заявки не одобряет, но того, кто подписался "
                     "и нажал «Я подписался» в личке, впустит.")
        if s.sub_action != "hold" or not s.sub_dm:
            lines.append("\n⚠️ Кнопка живёт только в сообщении из режима "
                         "«держать и ждать». Сейчас нажимать нечего, и режим "
                         "работает как «не трогать».")
    return "".join(lines)


def _forget_sub_access(cid: int) -> None:
    from ..services import subscribe as sub_svc
    sub_svc.forget_access(cid)
