"""Ядро модерации: применение наказаний, карточки в лог-чат, глобальный админ-лог."""
import asyncio
import json
import logging
import time

from aiogram import Bot
from aiogram.types import (
    ChatPermissions, InlineKeyboardMarkup, LinkPreviewOptions, User,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import config, db, utils
from . import adm_cache

logger = logging.getLogger("gremlin.moderation")

UNMUTE_PERMS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_invite_users=True,
)

MUTE_PERMS = ChatPermissions(
    can_react_to_messages=False,
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
)

def mute_perms(reactions: bool) -> ChatPermissions:
    """Права замученного. Реакции оставляем, если чат так настроен."""
    return MUTE_PERMS.model_copy(update={"can_react_to_messages": bool(reactions)})


KIND_EMOJI = {"ban": "⛔", "mute": "🔇", "delete": "🗑", "banchan": "📛"}
KIND_LABEL = {"ban": "Бан", "mute": "Мут", "delete": "Удаление", "banchan": "Бан отправителя-канала"}


def card_kb(pid: int | None, kind: str,
            chat_id: int | None = None, user_id: int | None = None) -> InlineKeyboardMarkup | None:
    """Кнопки карточки по итогу: забанен -> только разбан, мут -> бан или размут,
    просто удаление/подозрение -> бан."""
    b = InlineKeyboardBuilder()
    if kind in ("ban", "banchan"):
        b.button(text="🔓 Разбанить", callback_data=f"k:lift:{pid}")
    elif kind == "mute":
        b.button(text="⛔ Забанить", callback_data=f"k:ban:{chat_id}:{user_id}")
        b.button(text="🔊 Размутить", callback_data=f"k:lift:{pid}")
    elif chat_id is not None and user_id is not None:
        b.button(text="⛔ Забанить", callback_data=f"k:ban:{chat_id}:{user_id}")
    else:
        return None
    b.adjust(2)
    return b.as_markup()


# подписи типов вложений для строки «Сообщение»
_MEDIA_LABELS = {
    "photo": "фото", "video": "видео", "animation": "гифка", "sticker": "стикер",
    "voice": "голосовое", "video_note": "кружок", "audio": "аудио",
    "document": "файл", "poll": "опрос", "contact": "контакт", "location": "геопозиция",
}

BODY_LIMIT = 1000     # карточка целиком не должна упереться в лимит Telegram


def body_block(text: str | None, media: str | None = None) -> str:
    """Хвост карточки с текстом сообщения нарушителя.

    Показываем обычным текстом, без его форматирования: в логе нужна улика,
    а не кликабельная спам-ссылка.
    """
    text = (text or "").strip()
    if not text:
        return f"\n\n💬 <b>Сообщение:</b> [{media} без текста]" if media else ""
    head = f"💬 <b>Сообщение ({media}):</b>" if media else "💬 <b>Сообщение:</b>"
    return f"\n\n{head}\n{utils.esc(utils.chunk(text, BODY_LIMIT))}"


def _buttons(message) -> list[str]:
    """Кнопки под сообщением: подпись и ссылка, если она есть.

    Реклама через инлайн-ботов часто приходит картинкой без подписи, а вся суть
    висит на кнопке — без этого в карточке было бы только «фото без текста».
    """
    markup = getattr(message, "reply_markup", None)
    rows = getattr(markup, "inline_keyboard", None) or []
    out = []
    for row in rows:
        for b in row:
            label = utils.esc(getattr(b, "text", "") or "кнопка")
            url = getattr(b, "url", None)
            out.append(f"{label} → {utils.esc(url)}" if url else label)
    return out


def button_urls(message) -> list[str]:
    """Только ссылки с кнопок — для скоринга содержимого."""
    markup = getattr(message, "reply_markup", None)
    rows = getattr(markup, "inline_keyboard", None) or []
    return [b.url for row in rows for b in row if getattr(b, "url", None)]


def message_link(message) -> str | None:
    """Ссылка на сообщение. Для закрытых чатов — вида t.me/c/<чат>/<id>,
    её откроет любой, кто в чате состоит. Для лички ссылки не бывает."""
    chat = getattr(message, "chat", None)
    if chat is None or chat.type not in ("group", "supergroup", "channel"):
        return None
    if getattr(chat, "username", None):
        return f"https://t.me/{chat.username}/{message.message_id}"
    internal = str(chat.id)
    if not internal.startswith("-100"):
        return None                      # старые группы ссылок на сообщения не имеют
    return f"https://t.me/c/{internal[4:]}/{message.message_id}"


def message_body(message, with_link: bool = False) -> str:
    """То же для сообщения aiogram: сам достанет текст, вложение и кнопки.

    with_link — сообщение остаётся в чате (например, карточка подозрения),
    и на него можно дать ссылку. Для удалённых она бессмысленна.
    """
    if message is None:
        return ""
    media = next((label for attr, label in _MEDIA_LABELS.items()
                  if getattr(message, attr, None) is not None), None)
    lines = []
    link = message_link(message) if with_link else None
    if link:
        lines.append(f"🔗 <b>Ссылка:</b> {link}")   # сначала «где», потом «что»
    inner = body_block(message.text or message.caption, media).lstrip("\n")
    if inner:
        lines.append(inner)
    block = ("\n\n" + "\n".join(lines)) if lines else ""
    buttons = _buttons(message)
    if buttons:
        # каждая с новой строки: со ссылками строка иначе переносится в кашу
        shown = "\n".join(buttons[:5])
        if len(buttons) > 5:
            shown += f"\n…ещё {len(buttons) - 5}"
        block += f"\n🔘 <b>Кнопки:</b>\n{shown}"
    return block


def card_text(kind: str, chat_title: str | None, user_id: int, who: str,
              reason: str, by: str, until: int | None = None, body: str = "") -> str:
    """Единая структура карточки для всех событий."""
    lines = [
        f"{KIND_EMOJI.get(kind, '•')} <b>{KIND_LABEL.get(kind, kind)}</b> · {utils.esc(chat_title)}",
        f"👤 {who} (<code>{user_id}</code>)",
        f"📎 Причина: {utils.esc(reason)}",
    ]
    if kind == "mute":
        lines.append(f"⏰ До: {utils.fmt_ts(until)}")
    lines.append(f"👮 Кем: {by}")
    return "\n".join(lines) + body


# ответы Telegram, которые стоит переводить: админ должен понимать, что пошло не так
_ERRORS_RU = (
    ("not enough rights", "у бота нет права ограничивать участников"),
    ("CHAT_ADMIN_REQUIRED", "у бота нет прав администратора"),
    ("USER_ADMIN_INVALID", "нельзя тронуть админа чата"),
    ("PARTICIPANT_ID_INVALID", "неверная цель — это не человек из этого чата"),
    ("USER_ID_INVALID", "такого пользователя не существует"),
    ("PEER_ID_INVALID", "неверный id"),
    ("USER_NOT_PARTICIPANT", "в чате не состоит"),
    ("method is available for supergroup", "в обычной группе так нельзя — "
                                           "нужен супергруппа-чат"),
    ("Too Many Requests", "Telegram просит подождать"),
)


def human_error(e: Exception) -> str:
    text = str(e)
    for needle, human in _ERRORS_RU:
        if needle.lower() in text.lower():
            return human
    return text[:120]


async def punish_ex(bot: Bot, chat_id: int, user: User, kind: str, mute_min: int,
                    reason: str, by_id: int | None) -> tuple[int | None, str | None]:
    """То же, что apply_punishment, но возвращает и текст ошибки Telegram.

    Нужен там, где ответ видит живой админ: «не получилось» без причины
    отправляет искать несуществующую проблему с правами.
    """
    if kind == "delete":
        return None, None
    # спрашиваем до наказания: после бана статус всё равно станет «kicked»,
    # и уже не понять, был человек в чате или писал из комментариев
    member = await adm_cache.is_member(bot, chat_id, user.id)
    if kind == "mute" and not member:
        # Telegram ограничивает только участников: под постами привязанного
        # канала пишут те, кто в группу не вступал, и restrict на них падает
        # с PARTICIPANT_ID_INVALID — наказание просто не применялось. Отпускать
        # такого нельзя, поэтому мут превращается в бан.
        kind = "ban"
        reason += " · мут не-участнику невозможен, заменён баном"
    until = utils.until_ts(mute_min) if kind == "mute" else None
    try:
        if kind == "mute":
            s = await db.get_settings(chat_id)
            await bot.restrict_chat_member(
                chat_id, user.id, permissions=mute_perms(s.mute_reactions),
                until_date=until,
            )
        elif kind == "ban":
            await bot.ban_chat_member(chat_id, user.id)
    except Exception as e:
        logger.warning("punish %s failed in %s for %s", kind, chat_id, user.id,
                       exc_info=True)
        return None, human_error(e)
    from . import trust
    trust.invalidate(chat_id, user.id)
    pid = await db.add_punishment(
        chat_id, user.id, user.username, user.full_name, kind, reason, until, by_id,
        was_member=member,
    )
    return pid, None


async def apply_punishment(bot: Bot, chat_id: int, user: User, kind: str,
                           mute_min: int, reason: str, by_id: int | None) -> int | None:
    """Применить mute/ban к юзеру, записать в базу. Вернуть id наказания (None если delete).

    Тонкая обёртка над punish_ex: текст ошибки нужен не всем, а расходиться
    в поведении две копии одного кода рано или поздно начнут.
    """
    pid, _ = await punish_ex(bot, chat_id, user, kind, mute_min, reason, by_id)
    return pid


async def lift_punishment(bot: Bot, pid: int,
                          invite: bool = True) -> tuple[bool, str, str | None]:
    """Снять наказание по id. Вернуть (успех, текст, ссылка на возврат или None).

    Ссылку не публикуем сами — её дописывает вызвавший в своё же сообщение,
    чтобы лог-чат не пух от отдельных постов. invite=False — ссылку вообще не
    создавать: в меню она не нужна, а лишние одноразовые ссылки копятся в чате.
    """
    p = await db.get_punishment(pid)
    if p is None:
        return False, "Наказание не найдено.", None
    chat_id, uid, kind = p["chat_id"], p["user_id"], p["kind"]
    try:
        if kind == "mute":
            await bot.restrict_chat_member(chat_id, uid, permissions=UNMUTE_PERMS)
        elif kind == "ban":
            await bot.unban_chat_member(chat_id, uid, only_if_banned=True)
        elif kind == "banchan":
            await bot.unban_chat_sender_chat(chat_id, uid)
    except Exception as e:
        logger.warning("lift %s failed: %s", pid, e)
        return False, f"Не удалось снять: {e}", None
    await db.deactivate_punishment(pid)
    # не состоял в чате (комментатор под постом канала) — возвращать некуда
    was_member = bool(p["was_member"]) if "was_member" in p.keys() else True
    link = (await invite_back(bot, chat_id, uid)
            if (kind == "ban" and invite and was_member) else None)
    return True, "Снято.", link


def unban_pass_key(chat_id: int, uid: int) -> str:
    return f"unban_pass:{chat_id}:{uid}"


def unban_link_key(chat_id: int, uid: int) -> str:
    return f"unban_link:{chat_id}:{uid}"


async def invite_back(bot: Bot, chat_id: int, uid: int) -> str | None:
    """После разбана: ссылка на возврат + пропуск на автоодобрение заявки.

    Разбан только снимает блокировку, членом чата человека он не делает. В личку
    бот написать чаще всего не может (тот ему не писал), поэтому ссылку возвращаем
    наверх — вызвавший дописывает её в свою же карточку.

    Ссылка сделана «по заявке», а не одноразовой: одноразовую мог потратить любой,
    кто увидел карточку в лог-чате, и разбаненному бы не досталось. Тут же заявку
    от него бот одобрит сам по «пропуску», а заявка постороннего уйдёт админам.
    """
    until = int(time.time()) + config.UNBAN_PASS_HOURS * 3600
    await db.kv_set(unban_pass_key(chat_id, uid), str(until))

    old = await db.kv_get(unban_link_key(chat_id, uid))
    if old:
        return old                            # живая ссылка уже есть, вторую не плодим

    try:
        link = await bot.create_chat_invite_link(
            chat_id, creates_join_request=True, expire_date=until,
            name=f"возврат {uid}"[:32],
        )
    except Exception:
        logger.warning("invite link failed for chat %s", chat_id, exc_info=True)
        return ""                             # бан сняли, но ссылку сделать не вышло

    await db.kv_set(unban_link_key(chat_id, uid), link.invite_link)
    try:                                      # вдруг человек всё же писал боту
        await bot.send_message(
            uid, f"Вас разблокировали. Ссылка для возврата в чат:\n{link.invite_link}"
        )
    except Exception:
        pass
    return link.invite_link


# Карточка, в которую дописана ссылка на возврат: её надо будет почистить,
# когда ссылка отзовётся или протухнет. Править своё сообщение бот может лишь
# 48 часов, поэтому чистим с запасом — за пять минут до лимита.
CARD_EDIT_LIMIT = 48 * 3600 - 300


def unban_card_key(chat_id: int, uid: int) -> str:
    return f"unban_card:{chat_id}:{uid}"


async def remember_unban_card(chat_id: int, uid: int, log_chat_id: int,
                              msg_id: int, clean_text: str) -> None:
    """Запомнить карточку со ссылкой и её вид без ссылки.

    Карточек может быть несколько: одного и того же человека могли банить и
    разбанивать не раз, и ссылка попадала в каждую. Поэтому копим список, а не
    одну запись — иначе старая карточка осталась бы с живой ссылкой.
    """
    raw = await db.kv_get(unban_card_key(chat_id, uid))
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    cards = [c for c in data.get("cards", []) if c.get("msg") != msg_id]
    cards.append({"log": log_chat_id, "msg": msg_id, "text": clean_text})
    await db.kv_set(unban_card_key(chat_id, uid), json.dumps({
        "until": int(time.time()) + CARD_EDIT_LIMIT, "cards": cards[-10:],
    }))


async def clear_unban_card(bot: Bot, chat_id: int, uid: int, returned: bool = False) -> None:
    """Убрать ссылку из карточек — молча, если править уже нельзя.

    returned — человек вернулся в чат; иначе просто вышел срок.
    """
    raw = await db.kv_get(unban_card_key(chat_id, uid))
    if not raw:
        return
    await db.kv_set(unban_card_key(chat_id, uid), None)
    note = ("\n\n<i>Участник возвращён. Ссылка на возврат деактивирована.</i>"
            if returned else "\n\n<i>Ссылка на возврат деактивирована.</i>")
    try:
        cards = json.loads(raw).get("cards", [])
    except ValueError:
        return
    for card in cards:
        try:
            await bot.edit_message_text(
                card["text"] + note, chat_id=card["log"], message_id=card["msg"],
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except Exception:
            logger.warning("не вышло убрать ссылку из карточки", exc_info=True)
        # копия карточки в другом логе тащит ту же ссылку — чистим и там
        await update_twins(bot, card["log"], card["msg"], card["text"] + note)


async def card_sweeper(bot: Bot) -> None:
    """Раз в пять минут чистит карточки, у которых ссылка вот-вот протухнет.

    В Telegram при этом не ходим: пока чистить нечего, это один запрос к своей
    же базе и сон.
    """
    while True:
        try:
            now = time.time()
            for key, raw in await db.kv_prefix("unban_card:"):
                try:
                    data = json.loads(raw)
                except ValueError:
                    await db.kv_set(key, None)
                    continue
                if now < data.get("until", 0):
                    continue
                _, chat_id, uid = key.split(":")
                await clear_unban_card(bot, int(chat_id), int(uid))
                await db.kv_set(unban_link_key(int(chat_id), int(uid)), None)
            # связки копий живут ровно столько, сколько Telegram даёт править
            for key, raw in await db.kv_prefix("twin:"):
                try:
                    if now >= json.loads(raw).get("until", 0):
                        await db.kv_set(key, None)
                except ValueError:
                    await db.kv_set(key, None)
        except Exception:
            logger.warning("card sweeper tick failed", exc_info=True)
        await asyncio.sleep(300)


async def revoke_unban_link(bot: Bot, chat_id: int, uid: int) -> None:
    """Человек вернулся — ссылка больше не нужна, отзываем её.

    Иначе она болталась бы в карточке живой ещё двое суток, и по ней мог зайти
    любой, кому карточку переслали.
    """
    link = await db.kv_get(unban_link_key(chat_id, uid))
    await db.kv_set(unban_link_key(chat_id, uid), None)
    await db.kv_set(unban_pass_key(chat_id, uid), None)
    # человек в чате: ссылка мертва, убираем её из карточек
    await clear_unban_card(bot, chat_id, uid, returned=True)
    if not link:
        return
    try:
        await bot.revoke_chat_invite_link(chat_id, link)
    except Exception:
        logger.warning("revoke invite link failed in %s", chat_id, exc_info=True)


def unban_note(link: str | None) -> str:
    """Хвост к сообщению о снятии бана.

    None — снимали не бан (муту ссылка не нужна), пустая строка — бан сняли,
    но ссылку создать не удалось.
    """
    if link is None:
        return ""
    if link:
        return f"\n\nСсылка для входа (по заявке, её бот одобрит сам):\n{link}"
    return ("\n\n⚠️ Ссылку создать не вышло — боту нужно право "
            "«Пригласительные ссылки».")


async def unban_pass_valid(chat_id: int, uid: int) -> bool:
    """Есть ли действующий пропуск на автоодобрение заявки."""
    raw = await db.kv_get(unban_pass_key(chat_id, uid))
    if not raw:
        return False
    try:
        ok = int(raw) > int(time.time())
    except ValueError:
        ok = False
    if not ok:
        await db.kv_set(unban_pass_key(chat_id, uid), None)
    return ok


async def send_card(bot: Bot, chat_id: int, bit: int, text: str,
                    pid: int | None = None, kind: str = "delete",
                    user_id: int | None = None,
                    markup: InlineKeyboardMarkup | None = None) -> list:
    """Карточка события: в лог-чат этого чата и в глобальный лог владельца бота.

    Лог чата слушается настройками самого чата, глобальный — нет: он общий и
    собирает всё подряд, иначе смысла в нём мало.
    """
    s = await db.get_settings(chat_id)
    kb = markup if markup is not None else card_kb(pid, kind, chat_id, user_id)
    no_preview = LinkPreviewOptions(is_disabled=True)
    targets = []
    if s.cards_on and s.log_chat_id and (s.card_mask & bit):
        targets.append(s.log_chat_id)
    global_log = await db.global_log()
    if global_log and global_log not in targets and global_log != chat_id:
        targets.append(global_log)
    sent = []
    for target in targets:
        try:
            msg = await bot.send_message(target, text, reply_markup=kb,
                                         link_preview_options=no_preview)
            sent.append((target, msg.message_id))
        except Exception:
            logger.warning("card send failed for chat %s -> %s", chat_id, target,
                           exc_info=True)
    for target, msg_id in sent:
        remember_card(target, msg_id, text, kb)
    if len(sent) > 1:
        await link_twins(sent)
    return sent


# Что сейчас написано в отправленных карточках: (чат, сообщение) -> (текст, кнопки).
# Нужно, чтобы дописать итог рассылки по сетке. Спрашивать текст у Telegram
# нечем: getMessage в Bot API нет, а трюк с edit_message_reply_markup, которым
# это делалось раньше, заодно снимал кнопки, а на уже поправленной карточке
# падал — и карточка затиралась одной строчкой итога.
_bodies: dict[tuple[int, int], tuple[str, object]] = {}
_BODIES_MAX = 1000


def remember_card(chat_id: int, msg_id: int, text: str, markup=None) -> None:
    """Запомнить текущий вид карточки. Зовётся и после правок кнопками."""
    if len(_bodies) > _BODIES_MAX:
        _bodies.clear()          # итог приходит через секунды, старьё не нужно
    _bodies[(chat_id, msg_id)] = (text, markup)


async def append_to_cards(bot: Bot, sent: list, line: str) -> None:
    """Дописать строку к уже отправленным карточкам, кнопки сохранить.

    Нужно для итогов, которые известны позже самой карточки: рассылка по сетке
    идёт с паузами и заканчивается через несколько секунд после события.

    Карточку, которую не помним (например, бот успел перезапуститься), не
    трогаем: лучше остаться без приписки, чем затереть ею всю карточку.
    """
    for chat_id, msg_id in sent:
        body = _bodies.get((chat_id, msg_id))
        if body is None:
            logger.info("карточка %s/%s не в памяти, приписку пропускаю",
                        chat_id, msg_id)
            continue
        text, markup = body
        text += line
        try:
            await bot.edit_message_text(
                text, chat_id=chat_id, message_id=msg_id, reply_markup=markup,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except Exception:
            logger.warning("не вышло дописать итог в карточку %s/%s", chat_id, msg_id,
                           exc_info=True)
            continue
        remember_card(chat_id, msg_id, text, markup)


# Одно и то же событие уходит и в лог чата, и в глобальный. Кнопки живут в обеих
# копиях, поэтому копии надо помнить: нажали в одной — вторую правим следом,
# иначе там останутся живые кнопки под уже снятым наказанием.

def twin_key(chat_id: int, msg_id: int) -> str:
    return f"twin:{chat_id}:{msg_id}"


async def link_twins(sent: list[tuple[int, int]]) -> None:
    """Записать копии карточки друг на друга."""
    until = int(time.time()) + CARD_EDIT_LIMIT
    for chat_id, msg_id in sent:
        others = [[c, m] for c, m in sent if (c, m) != (chat_id, msg_id)]
        await db.kv_set(twin_key(chat_id, msg_id),
                        json.dumps({"until": until, "twins": others}))


async def update_twins(bot: Bot, chat_id: int, msg_id: int, text: str) -> None:
    """Повторить итог в копиях карточки и снять с них кнопки."""
    raw = await db.kv_get(twin_key(chat_id, msg_id))
    if not raw:
        return
    try:
        twins = json.loads(raw).get("twins", [])
    except ValueError:
        return
    for twin_chat, twin_msg in twins:
        try:
            await bot.edit_message_text(
                text, chat_id=twin_chat, message_id=twin_msg, reply_markup=None,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except Exception:
            logger.warning("twin card edit failed: %s/%s", twin_chat, twin_msg,
                           exc_info=True)
            continue
        remember_card(twin_chat, twin_msg, text, None)


# Альбом (несколько фото одним постом) приходит боту как несколько отдельных
# сообщений с общим media_group_id. Без склейки в лог улетала карточка на каждую
# картинку. Копим их тут: (chat_id, media_group_id) -> что успели увидеть.
_ALBUMS: dict[tuple[int, str], dict] = {}
ALBUM_WAIT = 1.5     # сколько ждём остальные части альбома, сек


def _album_sweep(now: float) -> None:
    for key, st in [(k, v) for k, v in _ALBUMS.items() if now - v["ts"] > 60]:
        _ALBUMS.pop(key, None)


async def violation(bot: Bot, message, feature_bit: int, feature_label: str,
                    punish_kind: str, mute_min: int, detail: str) -> None:
    """Полный цикл нарушения: удалить сообщение, наказать, карточка, логи."""
    chat = message.chat
    user = message.from_user
    group_id = getattr(message, "media_group_id", None)
    key = (chat.id, group_id) if group_id else None

    if key is not None and key in _ALBUMS:
        # это ещё одна картинка того же поста: молча удаляем и доливаем в карточку
        state = _ALBUMS[key]
        state["count"] += 1
        if not state["caption"]:
            state["caption"] = message.caption or message.text or ""
        try:
            await message.delete()
        except Exception:
            logger.warning("delete failed in %s", chat.id, exc_info=True)
        return

    body = message_body(message)      # текст берём до удаления
    if key is not None:
        _album_sweep(time.time())
        _ALBUMS[key] = {"count": 1, "caption": message.caption or message.text or "",
                        "ts": time.time()}
    try:
        await message.delete()
    except Exception:
        logger.warning("delete failed in %s", chat.id, exc_info=True)

    reason = f"{feature_label}: {detail}" if detail else feature_label
    pid = await apply_punishment(bot, chat.id, user, punish_kind, mute_min, reason, None)
    applied = punish_kind if pid else "delete"
    if pid:
        # мут не-участнику мог превратиться в бан — на карточке должно быть то,
        # что случилось на самом деле, а не то, что задумывалось
        row = await db.get_punishment(pid)
        if row is not None:
            applied = row["kind"]
            reason = row["reason"]

    if key is not None:
        # ждём остальные части поста, потом рисуем одну карточку на весь альбом
        await asyncio.sleep(ALBUM_WAIT)
        state = _ALBUMS.pop(key, {"count": 1, "caption": ""})
        body = body_block(state["caption"], f"альбом, {state['count']} шт.")

    who = utils.mention(user.id, user.full_name, user.username)
    card = card_text(
        applied, chat.title, user.id, who, reason, "Gremlin (автомод)",
        utils.until_ts(mute_min) if applied == "mute" else None, body,
    )
    await db.add_event(
        chat.id, feature_label,
        f"{applied}: {user.full_name} ({user.id}) — {reason}",
    )
    sent = await send_card(bot, chat.id, feature_bit, card, pid, applied, user.id)
    if applied != "delete":
        from . import net
        asyncio.create_task(net.spread_and_note(bot, sent, chat.id, user, applied,
                                                mute_min, reason, None))
