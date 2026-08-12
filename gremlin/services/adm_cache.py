"""Кэш админов чатов и результатов get_chat по @username."""
import time

from aiogram import Bot

from .. import config

# chat_id -> (expires, {user_id, ...})
_admins: dict[int, tuple[float, set[int]]] = {}
# username(lower) -> (expires, chat_type | None)
_mentions: dict[str, tuple[float, str | None]] = {}


async def chat_admin_ids(bot: Bot, chat_id: int) -> set[int]:
    now = time.monotonic()
    cached = _admins.get(chat_id)
    if cached and cached[0] > now:
        return cached[1]
    try:
        members = await bot.get_chat_administrators(chat_id)
        ids = {m.user.id for m in members}
    except Exception:
        ids = set()
    _admins[chat_id] = (now + config.ADMIN_CACHE_TTL, ids)
    return ids


def invalidate_admins(chat_id: int) -> None:
    _admins.pop(chat_id, None)


# chat_id -> (expires, (linked_chat_id | None, linked_username | None))
_linked: dict[int, tuple[float, tuple]] = {}


async def linked_chat(bot: Bot, chat_id: int) -> tuple[int | None, str | None]:
    """Привязанный к супергруппе канал (id, username). Нужен, чтобы ссылки
    на его посты не считались рекламой чужого канала."""
    now = time.monotonic()
    cached = _linked.get(chat_id)
    if cached and cached[0] > now:
        return cached[1]
    result: tuple[int | None, str | None] = (None, None)
    try:
        chat = await bot.get_chat(chat_id)
        linked_id = getattr(chat, "linked_chat_id", None)
        if linked_id:
            uname = None
            try:
                linked = await bot.get_chat(linked_id)
                uname = linked.username
            except Exception:
                pass
            result = (linked_id, uname)
    except Exception:
        pass
    _linked[chat_id] = (now + config.ADMIN_CACHE_TTL, result)
    return result


async def username_chat_type(bot: Bot, username: str) -> str | None:
    """Тип чата за @username: 'channel'/'supergroup'/'group'/'private'/'bot' или None."""
    key = username.lower().lstrip("@")
    now = time.monotonic()
    cached = _mentions.get(key)
    if cached and cached[0] > now:
        return cached[1]
    ctype: str | None = None
    try:
        chat = await bot.get_chat(f"@{key}")
        ctype = chat.type
    except Exception:
        ctype = None
    if len(_mentions) > 5000:
        _mentions.clear()
    _mentions[key] = (now + config.MENTION_CACHE_TTL, ctype)
    return ctype


# (chat_id, user_id) -> (expires, состоит ли в чате)
_members: dict[tuple[int, int], tuple[float, bool]] = {}
MEMBER_TTL = 900


async def is_member(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Состоит ли человек в чате.

    Нужно для комментариев под постами привязанного канала: их пишут те, кто в
    группу не вступал, и именно они — основной источник спама. У Telegram такие
    авторы приходят со статусом left/kicked.

    Ошибка API -> считаем участником: лучше пропустить сообщение, чем наказать
    своего из-за сбоя.
    """
    key = (chat_id, user_id)
    now = time.monotonic()
    cached = _members.get(key)
    if cached and cached[0] > now:
        return cached[1]
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        status = m.status
        if status in ("left", "kicked"):
            member = False
        elif status == "restricted":
            member = bool(getattr(m, "is_member", True))
        else:
            member = True
    except Exception:
        return True                     # не кэшируем — вдруг разовый сбой
    if len(_members) > 20000:
        _members.clear()
    _members[key] = (now + MEMBER_TTL, member)
    return member


def invalidate_member(chat_id: int, user_id: int) -> None:
    _members.pop((chat_id, user_id), None)
