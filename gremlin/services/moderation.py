"""Ядро модерации: применение наказаний, карточки в лог-чат, глобальный админ-лог."""
import logging
import time

from aiogram import Bot
from aiogram.types import ChatPermissions, InlineKeyboardMarkup, User
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import config, db, utils

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


def card_text(kind: str, chat_title: str | None, user_id: int, who: str,
              reason: str, by: str, until: int | None = None) -> str:
    """Единая структура карточки для всех событий."""
    lines = [
        f"{KIND_EMOJI.get(kind, '•')} <b>{KIND_LABEL.get(kind, kind)}</b> · {utils.esc(chat_title)}",
        f"👤 {who} (<code>{user_id}</code>)",
        f"📎 Причина: {utils.esc(reason)}",
    ]
    if kind == "mute":
        lines.append(f"⏰ До: {utils.fmt_ts(until)}")
    lines.append(f"👮 Кем: {by}")
    return "\n".join(lines)


async def apply_punishment(bot: Bot, chat_id: int, user: User, kind: str,
                           mute_min: int, reason: str, by_id: int | None) -> int | None:
    """Применить mute/ban к юзеру, записать в базу. Вернуть id наказания (None если delete)."""
    if kind == "delete":
        return None
    until = utils.until_ts(mute_min) if kind == "mute" else None
    try:
        if kind == "mute":
            await bot.restrict_chat_member(
                chat_id, user.id, permissions=MUTE_PERMS, until_date=until
            )
        elif kind == "ban":
            await bot.ban_chat_member(chat_id, user.id)
    except Exception:
        logger.warning("punish %s failed in %s for %s", kind, chat_id, user.id, exc_info=True)
        return None
    return await db.add_punishment(
        chat_id, user.id, user.username, user.full_name, kind, reason, until, by_id
    )


async def lift_punishment(bot: Bot, pid: int) -> tuple[bool, str, str | None]:
    """Снять наказание по id. Вернуть (успех, текст, ссылка на возврат или None).

    Ссылку не публикуем сами — её дописывает вызвавший в своё же сообщение,
    чтобы лог-чат не пух от отдельных постов.
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
    link = await invite_back(bot, chat_id, uid) if kind == "ban" else None
    return True, "Снято.", link


def unban_pass_key(chat_id: int, uid: int) -> str:
    return f"unban_pass:{chat_id}:{uid}"


async def invite_back(bot: Bot, chat_id: int, uid: int) -> str | None:
    """После разбана: ссылка на возврат + пропуск на автоодобрение заявки.

    Разбан только снимает блокировку, членом чата человека он не делает. В личку
    бот написать чаще всего не может (тот ему не писал), поэтому ссылку возвращаем
    наверх — вызвавший дописывает её в свою же карточку, чтобы лог-чат не пух от
    отдельных постов. Заодно ставим «пропуск»: если чат по заявкам, заявку от
    этого юзера бот одобрит автоматически.
    """
    until = int(time.time()) + config.UNBAN_PASS_HOURS * 3600
    await db.kv_set(unban_pass_key(chat_id, uid), str(until))

    link = None
    try:
        link = await bot.create_chat_invite_link(
            chat_id, member_limit=1, expire_date=until,
            name=f"возврат {uid}"[:32],
        )
    except Exception:
        logger.warning("invite link failed for chat %s", chat_id, exc_info=True)

    if link is None:
        return ""                             # бан сняли, но ссылку сделать не вышло
    try:                                      # вдруг человек всё же писал боту
        await bot.send_message(
            uid, f"Вас разблокировали. Ссылка для возврата в чат:\n{link.invite_link}"
        )
    except Exception:
        pass
    return link.invite_link


def unban_note(link: str | None) -> str:
    """Хвост к сообщению о снятии бана.

    None — снимали не бан (муту ссылка не нужна), пустая строка — бан сняли,
    но ссылку создать не удалось.
    """
    if link is None:
        return ""
    if link:
        return f"\n\nСсылка для входа:\n{link}"
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
                    user_id: int | None = None) -> None:
    """Карточка события в лог-чат чата (если включено и бит разрешён)."""
    s = await db.get_settings(chat_id)
    if not s.cards_on or not s.log_chat_id or not (s.card_mask & bit):
        return
    kb = card_kb(pid, kind, chat_id, user_id)
    try:
        await bot.send_message(s.log_chat_id, text, reply_markup=kb)
    except Exception:
        logger.warning("card send failed for chat %s", chat_id, exc_info=True)


async def violation(bot: Bot, message, feature_bit: int, feature_label: str,
                    punish_kind: str, mute_min: int, detail: str) -> None:
    """Полный цикл нарушения: удалить сообщение, наказать, карточка, логи."""
    chat = message.chat
    user = message.from_user
    try:
        await message.delete()
    except Exception:
        logger.warning("delete failed in %s", chat.id, exc_info=True)

    reason = f"{feature_label}: {detail}" if detail else feature_label
    pid = await apply_punishment(bot, chat.id, user, punish_kind, mute_min, reason, None)
    applied = punish_kind if pid else "delete"

    who = utils.mention(user.id, user.full_name, user.username)
    card = card_text(
        applied, chat.title, user.id, who, reason, "Gremlin (автомод)",
        utils.until_ts(mute_min) if applied == "mute" else None,
    )
    await db.add_event(
        chat.id, feature_label,
        f"{applied}: {user.full_name} ({user.id}) — {reason}",
    )
    await send_card(bot, chat.id, feature_bit, card, pid, applied, user.id)
