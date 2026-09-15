"""Кэш админов чатов и результатов get_chat по @username."""
import logging
import time

from aiogram import Bot

from .. import config

logger = logging.getLogger("gremlin.adm_cache")

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
# что за чат: 'channel', 'supergroup', 'group'. Заполняется попутно, когда мы
# и так ходим в getChat — отдельных запросов ради этого не делаем
_kind: dict[int, str] = {}
# обсуждение канала с «вступить, чтобы писать»: комментарий под постом
# делает человека участником группы. Тоже попутно из того же getChat
_comment_joins: dict[int, bool] = {}


async def linked_chat(bot: Bot, chat_id: int) -> tuple[int | None, str | None, str | None]:
    """Привязанный к супергруппе канал: (id, username, название).

    Нужен, чтобы ссылки на его посты не считались рекламой чужого канала,
    а в списке чатов было видно, к какому каналу привязано обсуждение.
    """
    now = time.monotonic()
    cached = _linked.get(chat_id)
    if cached and cached[0] > now:
        return cached[1]
    result: tuple[int | None, str | None, str | None] = (None, None, None)
    try:
        chat = await bot.get_chat(chat_id)
        if chat.type:
            _kind[chat_id] = chat.type
        linked_id = getattr(chat, "linked_chat_id", None)
        _comment_joins[chat_id] = bool(
            linked_id and getattr(chat, "join_to_send_messages", None))
        if linked_id:
            uname, title = None, None
            try:
                linked = await bot.get_chat(linked_id)
                uname, title = linked.username, linked.title
                if linked.type:
                    _kind[linked_id] = linked.type
            except Exception:
                pass
            result = (linked_id, uname, title)
    except Exception:
        pass
    _linked[chat_id] = (now + config.ADMIN_CACHE_TTL, result)
    return result


async def joins_by_comment(bot: Bot, chat_id: int) -> bool:
    """В группу вступают комментарием под постом привязанного канала.

    Так бывает у обсуждения с «вступить, чтобы писать»: Telegram молча делает
    комментатора участником, без ссылки, заявки и служебного сообщения. Сам
    человек при этом в чат не заходил и считает, что в нём не состоит.
    """
    await linked_chat(bot, chat_id)
    return _comment_joins.get(chat_id, False)


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


async def refresh_linked(bot, chat_id: int) -> str | None:
    """Спросить у Telegram привязанный канал и записать его в базу.

    Зовём редко: при регистрации чата и разовой сверкой на старте. Списки
    после этого читают название прямо из базы и не ходят в Telegram вовсе.
    """
    from .. import db
    _linked.pop(chat_id, None)
    linked_id, _uname, title = await linked_chat(bot, chat_id)
    await db.set_linked(chat_id, linked_id, title)
    # тип чата пишем заодно: сверка на старте проходит по всем чатам, так что
    # старые записи без типа заполняются сами, без отдельного похода в Telegram
    await db.set_kind(chat_id, _kind.get(chat_id))
    if linked_id:
        await db.set_kind(linked_id, _kind.get(linked_id))
    return title


async def reconcile_chats(bot) -> list[tuple[int, str]]:
    """Убрать из списка чаты, где бота уже нет.

    Обычно это делает обработчик выхода, но обновление можно и пропустить:
    бот лежал, базу откатили на копию, чат почистили без бота. Тогда в меню
    остаётся призрак — чат есть, а бота там нет, и любое действие по нему
    заканчивается ошибкой Telegram.

    Возвращает список выключенных: (id, название).
    """
    from .. import db
    me = (await bot.me()).id
    rows = await db.all_chats(active_only=True)
    gone = []
    for row in rows:
        cid = row["chat_id"]
        try:
            member = await bot.get_chat_member(cid, me)
            inside = member.status not in ("left", "kicked")
        except Exception as e:
            # 403 «bot is not a member», 400 «chat not found» — бота там нет.
            # Прочие ошибки (сеть, таймаут) не повод вычёркивать чат.
            text = str(e).lower()
            if not any(m in text for m in ("not a member", "chat not found",
                                           "forbidden", "kicked")):
                logger.warning("не проверить чат %s", cid, exc_info=True)
                continue
            inside = False
        if not inside:
            gone.append((cid, row["title"] or str(cid)))
            continue
        try:
            # заодно освежаем привязанный канал: он меняется редко, а списку
            # нужен готовым — иначе панель снова пошла бы спрашивать по кругу
            await refresh_linked(bot, cid)
        except Exception:
            logger.debug("канал чата %s не освежить", cid, exc_info=True)

    # Предохранитель: если «пропали» почти все чаты разом, дело не в чатах,
    # а в Telegram или в сети. Вычёркивать список целиком по такому поводу
    # нельзя — молча потерять все настройки хуже, чем показать лишнее.
    if rows and len(gone) > max(1, len(rows) // 2):
        logger.warning("сверка чатов отменена: не нашлись %d из %d — похоже "
                       "на сбой, а не на выход из чатов", len(gone), len(rows))
        return []

    for cid, title in gone:
        await db.set_chat_active(cid, False)
        await db.clear_log_refs(cid)
        logger.info("чат %s (%s) выключен: бота там нет", cid, title)
    return gone


# (chat_id, user_id) -> (expires, состоит ли в чате)
_members: dict[tuple[int, int], tuple[float, bool]] = {}
MEMBER_TTL = 900
# chat_id -> когда последний раз писали в лог о сбое getChatMember
_member_fail_logged: dict[int, float] = {}
MEMBER_FAIL_LOG_EVERY = 600


def member_cached(chat_id: int, user_id: int) -> bool | None:
    """Что мы уже знаем о членстве, не спрашивая Telegram. None — не знаем.

    Нужно там, где ответ приятен, но не обязателен: теневые прогоны и записи
    в лог не стоят живого запроса к API на каждое сообщение.
    """
    hit = _members.get((chat_id, user_id))
    return hit[1] if hit and hit[0] > time.monotonic() else None


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
    except Exception as e:
        # Молча считать участником нельзя: по этому ответу пишется was_member,
        # выдаётся ссылка на возврат и уровень доверия, и потом не отличить
        # настоящего участника от сбоя. В лог — не чаще раза на чат за 10 минут,
        # иначе при лежащем API каждое сообщение даст строку
        if now - _member_fail_logged.get(chat_id, -MEMBER_FAIL_LOG_EVERY) \
                >= MEMBER_FAIL_LOG_EVERY:
            _member_fail_logged[chat_id] = now
            logger.warning("getChatMember упал в %s для %s, считаем участником: %s",
                           chat_id, user_id, e)
        return True                     # не кэшируем — вдруг разовый сбой
    if len(_members) > 20000:
        _members.clear()
    _members[key] = (now + MEMBER_TTL, member)
    return member


def invalidate_member(chat_id: int, user_id: int) -> None:
    _members.pop((chat_id, user_id), None)


# Права, без которых модерация ломается молча: (поле, что сказать человеку).
# Закреп бот просит при добавлении, но нигде не пользуется — его не требуем.
BOT_NEEDS = (
    ("can_delete_messages", "удалять сообщения"),
    ("can_restrict_members", "банить и ограничивать"),
    ("can_invite_users", "приглашать (ссылки и заявки)"),
)
# chat_id -> (expires, статус бота)
_bot_status: dict[int, tuple[float, dict]] = {}
BOT_STATUS_TTL = 30


async def bot_status(bot: Bot, chat_id: int) -> dict:
    """Кто бот в чате и хватает ли ему прав.

    {"state": ok | missing | not_admin | gone | unknown, "missing": [...],
     "text": готовая строка}. Карточка чата перерисовывается на каждый
    переключатель, поэтому ответ держим полминуты; сбой не запоминаем.
    """
    now = time.monotonic()
    hit = _bot_status.get(chat_id)
    if hit and hit[0] > now:
        return hit[1]
    try:
        me = await bot.me()
        m = await bot.get_chat_member(chat_id, me.id)
    except Exception:
        logger.debug("статус бота в %s не узнать", chat_id, exc_info=True)
        return {"state": "unknown", "missing": [],
                "text": "❓ не проверить — Telegram не ответил"}
    missing: list[str] = []
    if m.status in ("left", "kicked"):
        state, text = "gone", "⛔ бота нет в чате"
    elif m.status == "creator":
        state, text = "ok", "✅ владелец чата"
    elif m.status != "administrator":
        state, text = "not_admin", "⚠️ не администратор — модерация не работает"
    else:
        missing = [label for key, label in BOT_NEEDS if not getattr(m, key, False)]
        state = "missing" if missing else "ok"
        text = ("⚠️ не хватает прав: " + ", ".join(missing) if missing
                else "✅ администратор, права в порядке")
    out = {"state": state, "missing": missing, "text": text}
    _bot_status[chat_id] = (now + BOT_STATUS_TTL, out)
    return out


def invalidate_bot_status(chat_id: int) -> None:
    _bot_status.pop(chat_id, None)
