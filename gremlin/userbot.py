"""Юзербот-наблюдатель (Telethon) — глаза бота там, где Bot API слеп.

Зачем: обычный бот не видит сообщения других ботов. Спам-схема выглядит так —
юзер зовёт стороннего бота (@xxxbot), тот присылает в чат картинку с кнопками
на telegra.ph. Бот такого сообщения не увидит, юзербот — увидит.

Роли строго разделены: юзербот только смотрит и находит виновника, все действия
(удаление, бан, карточка, запись в базу) делает основной бот — чтобы модерация
шла одним каналом и с обычного аккаунта не летели массовые баны.
"""
import asyncio
import logging
import re
from datetime import datetime, timezone

from aiogram import Bot

from . import config, db, utils
from .services import moderation, stats_collect, watch

logger = logging.getLogger("gremlin.userbot")

# «сколько последних сообщений просматриваем, чтобы найти, кто позвал бота»
_LOOKBACK = 30
# кто кого звал: (chat_id, bot_username) -> (user_id, имя, username, monotonic)
_recent_calls: dict[tuple[int, str], tuple] = {}
_CALL_TTL = 300

_BOT_MENTION = re.compile(r"@(\w{3,32}bot)\b", re.IGNORECASE)

# живой Telethon-клиент — через него меню дёргает обновление состава чата
_client_ref = None


def _client():
    """Клиент Telethon на сессии от скраппера. None — не настроен.

    connection_retries=None — переподключаться бесконечно: по умолчанию Telethon
    сдаётся после 5 попыток и дальше молча висит отключённым.
    """
    from telethon import TelegramClient
    if not (config.TG_API_ID and config.TG_API_HASH):
        return None
    return TelegramClient(
        config.TG_SESSION, int(config.TG_API_ID), config.TG_API_HASH,
        connection_retries=None,     # бесконечно
        retry_delay=5,
        auto_reconnect=True,
        request_retries=5,
    )


async def _watchdog(client) -> None:
    """Страховка поверх авто-реконнекта: раз в минуту проверяем связь.

    Telethon сам поднимает соединение после обрывов, но если он всё же остался
    отключённым (например, разрыв во время долгого сна машины), без этой
    проверки юзербот молча перестал бы видеть сообщения.
    """
    while True:
        await asyncio.sleep(60)
        try:
            if not client.is_connected():
                logger.warning("юзербот отключился — переподключаюсь")
                await client.connect()
                if await client.is_user_authorized():
                    logger.info("юзербот снова на связи")
        except Exception:
            logger.warning("не удалось переподключить юзербота", exc_info=True)


def _button_urls(message) -> list[str]:
    """Ссылки с инлайн-кнопок сообщения (спам их прячет именно туда)."""
    urls = []
    rows = getattr(message, "buttons", None) or []
    for row in rows:
        for btn in row:
            url = getattr(btn, "url", None)
            if url:
                urls.append(url)
    return urls


def remember_call(chat_id: int, text: str, user) -> None:
    """Запомнить, что юзер упомянул @какого-то_бота — вдруг тот сейчас ответит спамом."""
    now = asyncio.get_event_loop().time()
    for uname in _BOT_MENTION.findall(text or ""):
        _recent_calls[(chat_id, uname.lower())] = (
            user.id,
            utils.esc(getattr(user, "first_name", "") or ""),
            getattr(user, "username", None),
            now,
        )


def find_caller(chat_id: int, bot_username: str | None) -> tuple | None:
    """Кто звал этого бота недавно. None — не нашли."""
    if not bot_username:
        return None
    rec = _recent_calls.get((chat_id, bot_username.lower()))
    if rec is None:
        return None
    if asyncio.get_event_loop().time() - rec[3] > _CALL_TTL:
        return None
    return rec


def score_bot_message(message, self_bot: str | None = None) -> tuple[int, list[str]]:
    """Насколько сообщение стороннего бота похоже на спам."""
    return watch.score_content(message.text or "", _button_urls(message), self_bot)


async def _caller_punish(bot: Bot, chat_id: int, s, uid: int, bot_uname: str | None,
                         score: int) -> tuple[str, int]:
    """Что полагается тому, кто позвал спам-бота.

    Это то же нарушение, что ловит раздел «Инлайн-боты»: человек дёрнул чужого
    бота, и тот напечатал рекламу. Bot API таких сообщений боту не отдаёт, их
    видит только юзербот — но правило должно быть одно, иначе одно и то же
    наказывается по-разному в зависимости от того, кто первым заметил.

    Раздел выключен — остаётся старое поведение наблюдения: бан с порога
    «автобан», иначе только карточка.
    """
    if not s.inline_on:
        return ("ban", 0) if s.watch_ban and score >= s.watch_ban else ("delete", 0)
    if await db.inline_wl_allowed(chat_id, bot_uname, None):
        return "delete", 0                     # этого бота в чате разрешили
    scopes = await db.wl_scopes_for(chat_id, uid, None)
    if scopes & {"all", "inline"}:
        return "delete", 0                     # звавший в вайтлисте
    kind, mute_min = s.inline_punish, s.inline_mute_min
    if s.trust_on:
        from .services import trust
        kind = trust.soften(kind, await trust.level(bot, chat_id, uid, s),
                            s, config.TRUST_S_INLINE)
    if s.inline_spam and score >= s.inline_spam:
        kind, mute_min = "ban", 0              # спам-содержимое — сразу бан
    return kind, mute_min


async def _handle_bot_spam(bot: Bot, chat_id: int, message, sender) -> None:
    """Сообщение от стороннего бота: удалить и наказать того, кто его позвал."""
    s = await db.get_settings(chat_id)
    if not s.watch_on:
        return
    score, reasons = score_bot_message(message, getattr(sender, "username", None))
    if score < s.watch_suspect:
        return

    ch = await db.get_chat(chat_id)
    title = ch["title"] if ch else str(chat_id)
    bot_uname = getattr(sender, "username", None)
    why = ", ".join(reasons) or "сообщение стороннего бота"
    # у Telethon свои объекты, поэтому текст и тип вложения достаём вручную
    body = moderation.body_block(
        getattr(message, "text", None), "медиа" if getattr(message, "media", None) else None
    )

    # 1) убираем сообщение бота — руками основного бота, он админ
    try:
        await bot.delete_message(chat_id, message.id)
    except Exception:
        try:                       # не вышло — удаляем юзерботом
            await message.delete()
        except Exception:
            logger.warning("cannot delete bot message in %s", chat_id, exc_info=True)

    # 2) ищем, кто позвал этого бота
    caller = find_caller(chat_id, bot_uname)
    lines = [
        f"🤖 <b>Спам стороннего бота</b> · {utils.esc(title)}",
        f"📢 Бот: @{utils.esc(bot_uname or '—')}",
        f"📎 Сигналы: {utils.esc(why)} — <b>{score} очков</b>",
    ]
    pid, applied = None, "delete"
    if caller:
        uid, name, uname, _ = caller
        who = utils.mention(uid, name, uname)
        lines.append(f"👤 Позвал: {who} (<code>{uid}</code>)")
        kind, mute_min = await _caller_punish(bot, chat_id, s, uid, uname, score)
        if kind != "delete":
            from .services import net
            user = await net.user_stub(uid)
            user.username, user.full_name = uname or user.username, name or user.full_name
            pid = await moderation.apply_punishment(
                bot, chat_id, user, kind, mute_min,
                f"вызов спам-бота @{bot_uname}: {why} ({score})", None)
            if pid:
                row = await db.get_punishment(pid)
                applied = row["kind"] if row else kind
                lines.append("⛔ Забанен автоматически" if applied == "ban"
                             else f"🔇 Мут на {utils.fmt_minutes(mute_min)}")
            else:
                lines.append("⚠️ Наказать не вышло — не хватило прав")
    else:
        lines.append("👤 Позвавшего найти не удалось")
        uid = None

    await db.add_event(chat_id, "watch", f"спам-бот @{bot_uname} ({score}) — {why}")
    sent = await moderation.send_card(
        bot, chat_id, config.BIT_WATCH, "\n".join(lines) + body,
        pid, applied if pid else "delete", caller[0] if caller else None,
    )
    if pid:
        from .services import net
        asyncio.create_task(net.spread_and_note(
            bot, sent, chat_id, await net.user_stub(uid), applied, mute_min,
            f"вызов спам-бота @{bot_uname}", None))


async def refresh_members(chat_id: int | None = None) -> int:
    """Пересобрать состав чата в базе статистики (как fetch_participants у скраппера).

    Возвращает число участников. 0 — юзербот не поднят или чат не доступен.
    Вышедших помечаем is_member=0 только после полного прохода, иначе на обрыве
    можно снять флаг у половины чата.
    """
    client = _client_ref
    if client is None or not client.is_connected():
        return 0
    cid = chat_id or stats_collect.tracked_chat_id()
    if not cid:
        return 0
    try:
        from telethon.tl.types import (
            ChannelParticipantAdmin, ChannelParticipantCreator, ChannelParticipantsAdmins,
            User as TgUser, UserStatusOffline, UserStatusRecently,
        )
        entity = await client.get_entity(cid)
        admins = set()
        try:
            async for u in client.iter_participants(entity, filter=ChannelParticipantsAdmins()):
                admins.add(u.id)
        except Exception:
            pass  # без прав на список админов — не критично

        rows, present = [], set()
        async for u in client.iter_participants(entity):
            if not isinstance(u, TgUser):
                continue
            present.add(u.id)
            p = getattr(u, "participant", None)
            joined = getattr(p, "date", None)
            is_admin = int(u.id in admins or isinstance(
                p, (ChannelParticipantAdmin, ChannelParticipantCreator)))
            last_online = None
            if isinstance(u.status, UserStatusOffline):
                last_online = u.status.was_online.isoformat()
            elif isinstance(u.status, UserStatusRecently):
                last_online = "recently"
            rows.append((u.id, u.username, u.first_name, u.last_name, int(bool(u.bot)),
                         int(bool(u.deleted)), is_admin,
                         joined.isoformat() if joined else None, last_online))
        if not rows:
            return 0
        await asyncio.to_thread(stats_collect.save_members, rows, present)
        logger.info("состав чата %s обновлён: %s участников", cid, len(rows))
        return len(rows)
    except Exception:
        logger.warning("refresh_members failed for %s", cid, exc_info=True)
        return 0


async def start(bot: Bot) -> object | None:
    """Поднять юзербота. Возвращает клиент (или None, если выключен/не настроен)."""
    if not config.USERBOT_ON:
        logger.info("юзербот выключен (USERBOT_ON)")
        return None
    try:
        from telethon import events
    except ImportError:
        logger.warning("telethon не установлен — юзербот не запущен")
        return None
    client = _client()
    if client is None:
        logger.warning("TG_API_ID/TG_API_HASH не заданы — юзербот не запущен")
        return None

    await client.start()
    global _client_ref
    _client_ref = client
    me = await client.get_me()
    logger.info("юзербот запущен: @%s (id=%s)", me.username, me.id)
    asyncio.create_task(_watchdog(client))

    @client.on(events.NewMessage())
    async def _on_message(event) -> None:
        try:
            chat_id = event.chat_id
            sender = await event.get_sender()
            if sender is None:
                return

            # статистика: пишем только тот чат, по которому собрана база
            stats_chat = stats_collect.tracked_chat_id()      # кэш на 5 минут внутри
            if stats_chat and chat_id == stats_chat and not getattr(sender, "bot", False):
                await asyncio.to_thread(
                    stats_collect.record,
                    sender.id, getattr(sender, "username", None),
                    getattr(sender, "first_name", None), getattr(sender, "last_name", None),
                    False, event.message.id, len(event.message.text or ""),
                    event.message.media is not None,
                    event.message.reply_to_msg_id is not None,
                    event.message.date or datetime.now(timezone.utc),
                )

            # модерация — только в чатах, которые ведёт бот, и только по свежему
            if await db.get_chat(chat_id) is None:
                return
            msg_ts = event.message.date
            if msg_ts and (datetime.now(timezone.utc) - msg_ts).total_seconds() > config.MSG_MAX_AGE:
                return
            if getattr(sender, "bot", False):
                if sender.id != (await bot.me()).id:
                    await _handle_bot_spam(bot, chat_id, event.message, sender)
            else:
                remember_call(chat_id, event.message.text or "", sender)
        except Exception:
            logger.warning("userbot handler failed", exc_info=True)

    return client
