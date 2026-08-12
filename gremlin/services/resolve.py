"""Определение id по @username.

Зачем: в списках надёжнее хранить id — ник человек может сменить, и запись
перестанет срабатывать. Пробуем по возрастанию стоимости: своя база →
Bot API (умеет публичные каналы и группы) → юзербот (умеет и людей).
"""
import logging

from aiogram import Bot

from .. import db

logger = logging.getLogger("gremlin.resolve")


async def by_username(bot: Bot, username: str) -> tuple[int | None, str | None]:
    """Вернуть (id, отображаемое имя). id = None — определить не вышло."""
    uname = username.strip().lstrip("@")
    if not uname:
        return None, None

    # 1) уже знаем этого человека — бесплатно
    row = await db.user_by_username(uname)
    if row:
        return row["user_id"], row["first_name"]

    # 2) Bot API: публичные каналы и группы
    try:
        chat = await bot.get_chat(f"@{uname}")
        title = chat.title or chat.first_name
        return chat.id, title
    except Exception:
        pass

    # 3) юзербот: обычных людей резолвит только он
    try:
        from .. import userbot
        client = userbot._client_ref
        if client is not None and client.is_connected():
            ent = await client.get_entity(uname)
            name = getattr(ent, "title", None) or getattr(ent, "first_name", None)
            return ent.id, name
    except Exception:
        logger.debug("userbot resolve failed for @%s", uname, exc_info=True)

    return None, None
