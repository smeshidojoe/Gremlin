"""Сетка чатов: наказание, выданное в одном чате, разъезжается по остальным.

Сетка привязана к владельцу — это его чаты с включённой галочкой. Отдельной
таблицы сетевых банов нет: в каждом чате пишется обычное наказание, поэтому
списки активных, кнопки снятия и история работают там как для любого другого.

Рассылка идёт фоном с паузой между чатами: лимиты Telegram важнее скорости.
Когда очередь отработала, к исходной карточке в логе дописывается строка
с итогом — отдельных карточек в чужие логи не шлём, это был бы шум.
"""
import asyncio
import logging
from types import SimpleNamespace

from aiogram import Bot

from .. import config, db, utils

logger = logging.getLogger("gremlin.net")

KIND_BIT = {
    "ban": config.NET_BAN,
    "mute": config.NET_MUTE,
    "warn": config.NET_WARN,
}

_VERB = {"ban": "забанен", "mute": "замучен", "warn": "варн учтён"}


async def enabled(chat_id: int, kind: str) -> list:
    """Куда разъезжается наказание этого вида. Пустой список — никуда."""
    peers = await db.net_peers(chat_id)
    if not peers:
        return []
    net = await db.net_of_chat(chat_id)
    bit = KIND_BIT.get(kind)
    if net is None or bit is None or not (net["sync_mask"] & bit):
        return []
    return peers


async def _skip(bot: Bot, chat_id: int, user_id: int) -> str | None:
    """Причина не трогать человека в этом чате, либо None."""
    from . import adm_cache
    if user_id in await adm_cache.chat_admin_ids(bot, chat_id):
        return "админ"
    if await db.wl_scopes_for(chat_id, user_id, None):
        return "вайтлист"
    return None


# Повтором считаем то, что не мягче просимого: при активном бане мут
# не нужен, а при активном муте бан — нужен, он строже
_ALREADY = {"mute": ("mute", "ban"), "ban": ("ban",)}


async def spread(bot: Bot, src_chat: int, user, kind: str, mute_min: int,
                 reason: str, by_id: int | None,
                 ban_min: int = 0) -> tuple[int, int, int, int]:
    """Разослать наказание по сетке. Вернуть (сделано, пропущено, ошибок, из
    них мутов, ставших банами).

    Мут в каждом чате решается заново: участнику — мут, не-участнику — бан на
    тот же срок. Что вышло в исходном чате, соседей не касается.
    """
    peers = await enabled(src_chat, kind)
    if not peers:
        return 0, 0, 0, 0
    src = await db.get_chat(src_chat)
    src_title = (src["title"] if src else str(src_chat)) or str(src_chat)
    note = f"сетка · {src_title}: {reason}"

    done = skipped = failed = swapped = 0
    for peer in peers:
        cid = peer["chat_id"]
        await asyncio.sleep(config.NET_DELAY)
        try:
            if await _skip(bot, cid, user.id):
                skipped += 1
                continue
            if any([await db.active_punishment_of(cid, user.id, k) is not None
                    for k in _ALREADY.get(kind, (kind,))]):
                skipped += 1          # уже наказан там не мягче — не дублируем
                continue
            from . import moderation
            # в чужих чатах сетки человек мог ничего и не писать — убирать
            # там за ним нечего, и лезть в их переписку мы не будем
            pid = await moderation.apply_punishment(
                bot, cid, user, kind, mute_min, note, by_id, wipe=False,
                ban_min=ban_min,
            )
            if pid is None:
                failed += 1
                continue
            done += 1
            # в журнал — то, что выдали (мут), даже если Telegram применил
            # его баном; сколько таких, видно только в bot.log
            row = await db.get_punishment(pid)
            if row is not None and row["kind"] != kind:
                swapped += 1
            await db.add_event(cid, "manual",
                               f"{kind} по сетке: {user.id} — из {src_title}")
        except Exception:
            failed += 1
            logger.warning("сетка: не вышло наказать %s в %s", user.id, cid,
                           exc_info=True)
    logger.info("сетка %s: %s -> сделано %s (банами вместо мута %s), пропущено %s, "
                "ошибок %s", kind, src_chat, done, swapped, skipped, failed)
    return done, skipped, failed, swapped


async def lift(bot: Bot, src_chat: int, user_id: int) -> tuple[int, int]:
    """Снять наказания по сетке. Вернуть (снято, ошибок).

    lift_mode='source' — снимаем только если наказание выдавали из этого же чата
    (в базе такие помечены причиной «сетка · …», значит родные — без пометки).
    """
    peers = await db.net_peers(src_chat)
    net = await db.net_of_chat(src_chat)
    if not peers or net is None:
        return 0, 0
    if not (net["sync_mask"] & config.NET_LIFT):
        return 0, 0

    from . import adm_cache, moderation
    done = failed = 0
    for peer in peers:
        cid = peer["chat_id"]
        row = await db.active_punishment_of(cid, user_id, "ban")
        row = row or await db.active_punishment_of(cid, user_id, "mute")
        if row is None:
            continue
        if net["lift_mode"] == "source" and not (row["reason"] or "").startswith("сетка"):
            continue              # наказание местное — чужому чату его не снять
        await asyncio.sleep(config.NET_DELAY)
        try:
            if row["kind"] == "ban":
                await bot.unban_chat_member(cid, user_id, only_if_banned=True)
            else:
                await bot.restrict_chat_member(cid, user_id,
                                               permissions=await moderation.unmute_perms(bot, cid))
            await db.deactivate_user_punishments(cid, user_id)
            adm_cache.invalidate_member(cid, user_id)
            done += 1
            await db.add_event(cid, "manual", f"снятие по сетке: {user_id}")
        except Exception:
            failed += 1
            logger.warning("сетка: не вышло снять с %s в %s", user_id, cid, exc_info=True)
    return done, failed


async def user_stub(user_id: int, bot: Bot | None = None,
                    chat_id: int | None = None, *, username: str | None = None,
                    name: str | None = None):
    """Заглушка юзера по id — для мест, где под рукой только число.

    Своя база знает только тех, кто писал при боте. Кого в ней нет (назвали
    командой по @нику, пришёл заявкой), спрашиваем у Telegram через чат —
    иначе в карточке вместо имени и ника голый id. username и name —
    подсказки от вызывающего, когда он их уже знает.
    """
    row = await db.get_user(user_id)
    uname = row["username"] if row else None
    full = row["first_name"] if row else None
    if not (uname and full) and bot is not None and chat_id is not None:
        try:
            u = (await bot.get_chat_member(chat_id, user_id)).user
            uname, full = uname or u.username, full or u.full_name
        except Exception:
            logger.debug("имя %s не узнать", user_id, exc_info=True)
    uname = uname or (username or "").lstrip("@") or None
    return SimpleNamespace(
        id=user_id,
        username=uname,
        # без имени лучше ник, чем тот же id второй раз
        full_name=full or name or (f"@{uname}" if uname else str(user_id)),
    )


async def spread_id_and_note(bot: Bot, sent: list, src_chat: int, user_id: int,
                             kind: str, mute_min: int, reason: str,
                             by_id: int | None) -> None:
    await spread_and_note(bot, sent, src_chat, await user_stub(user_id), kind,
                          mute_min, reason, by_id)


async def spread_warn(bot: Bot, src_chat: int, user, reason: str,
                      by_id: int) -> tuple[int, int, int]:
    """Разослать варн: в соседних чатах он ложится в их собственный счётчик.

    Лимит и наказание у каждого чата свои — где-то три варна, где-то пять,
    поэтому мы только добавляем варн и смотрим, не добрал ли человек там.
    """
    peers = await enabled(src_chat, "warn")
    if not peers:
        return 0, 0, 0
    src = await db.get_chat(src_chat)
    src_title = (src["title"] if src else str(src_chat)) or str(src_chat)
    note = f"сетка · {src_title}: {reason}"

    from . import moderation
    done = skipped = failed = 0
    for peer in peers:
        cid = peer["chat_id"]
        try:
            s = await db.get_settings(cid)
            if not s.warns_on or await _skip(bot, cid, user.id):
                skipped += 1
                continue
            count = await db.warn_add(cid, user, note, by_id)
            done += 1
            if count < s.warns_limit or s.warns_punish == "delete":
                continue
            await asyncio.sleep(config.NET_DELAY)
            await db.warn_reset(cid, user.id)
            await moderation.apply_punishment(
                bot, cid, user, s.warns_punish, s.warns_mute_min,
                f"набрано {s.warns_limit} варнов", by_id, wipe=False,
            )
        except Exception:
            failed += 1
            logger.warning("сетка: варн не прошёл в %s", cid, exc_info=True)
    return done, skipped, failed


async def warn_and_note(bot: Bot, sent: list, src_chat: int, user, reason: str,
                        by_id: int) -> None:
    done, skipped, failed = await spread_warn(bot, src_chat, user, reason, by_id)
    line = summary("warn", done, skipped, failed)
    if line and sent:
        from . import moderation
        await moderation.append_to_cards(bot, sent, line)


def _in(n: int) -> str:
    return f"{n} {utils.plural(n, 'чате', 'чатах', 'чатах')}"


def summary(kind: str, done: int, skipped: int = 0, failed: int = 0) -> str:
    """Строка-приписка к карточке в логе исходного чата.

    Мут пишем мутом и там, где Telegram применил его баном: выдан мут.
    """
    if not done and not failed:
        return ""
    if kind == "lift":
        parts = [f"снято ещё в {_in(done)}"]
    else:
        parts = [f"{_VERB.get(kind, kind)} ещё в {_in(done)}"]
    if skipped:
        parts.append(f"{skipped} пропущено")
    if failed:
        parts.append(f"{failed} не удалось")
    return "\n🕸 Сетка: " + " · ".join(parts)


async def spread_and_note(bot: Bot, sent: list, src_chat: int, user, kind: str,
                          mute_min: int, reason: str, by_id: int | None,
                          ban_min: int = 0) -> None:
    """Разослать и дописать итог в уже отправленную карточку."""
    done, skipped, failed, _swapped = await spread(bot, src_chat, user, kind,
                                                   mute_min, reason, by_id,
                                                   ban_min=ban_min)
    line = summary(kind, done, skipped, failed)
    if line and sent:
        from . import moderation
        await moderation.append_to_cards(bot, sent, line)


async def lift_and_note(bot: Bot, sent: list, src_chat: int, user_id: int) -> None:
    done, failed = await lift(bot, src_chat, user_id)
    line = summary("lift", done, 0, failed)
    if line and sent:
        from . import moderation
        await moderation.append_to_cards(bot, sent, line)
