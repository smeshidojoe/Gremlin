"""События: бот добавлен/удалён, ручные баны админов, смена названия."""
import asyncio
import logging
import time

from aiogram import Bot, F, Router
from aiogram.filters import (
    ADMINISTRATOR, IS_MEMBER, IS_NOT_MEMBER, MEMBER, ChatMemberUpdatedFilter,
)
from aiogram.types import ChatMemberUpdated, Message

from .. import config, db, utils
from ..services import adm_cache, moderation
from . import group

logger = logging.getLogger("gremlin.events")

router = Router()


# ---------- бот добавили / убрали ----------

@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> IS_MEMBER))
async def bot_added(update: ChatMemberUpdated, bot: Bot) -> None:
    chat = update.chat
    if chat.type not in ("group", "supergroup"):
        return
    adder = update.from_user
    owner_id = adder.id if adder and not adder.is_bot else None

    # бот работает только там, куда его позвал кто-то из допущенных
    allowed = owner_id is not None and (
        owner_id in config.ADMIN_IDS
        or await db.access_allowed(owner_id, adder.username if adder else None)
    )
    if not allowed:
        who = (
            utils.mention(adder.id, adder.full_name, adder.username) if adder else "неизвестно"
        )
        left = True
        try:
            await bot.leave_chat(chat.id)
        except Exception:
            left = False
            logger.warning("leave unauthorized chat %s failed", chat.id, exc_info=True)
        for admin_id in config.ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"⚠️ <b>Бота добавили в чужой чат</b>\n"
                    f"💬 {utils.esc(chat.title)} (<code>{chat.id}</code>)\n"
                    f"👤 Добавил: {who} (<code>{owner_id}</code>)\n\n"
                    + ("🚪 Бот вышел из чата." if left
                       else "⚠️ Выйти не удалось — чат не зарегистрирован, бот его игнорирует."),
                )
            except Exception:
                logger.warning("unauthorized-add notice failed for %s", admin_id, exc_info=True)
        return

    await db.upsert_chat(chat.id, chat.title, chat.username, owner_id)
    await db.add_event(chat.id, "bot", f"добавлен в чат «{chat.title}» юзером {owner_id}")
    note = ""
    if chat.type == "group":
        # Обычная группа: Telegram вообще не умеет выдавать ботам права
        # администратора в таких чатах, поэтому клиент показывает «ошибка при
        # добавлении бота в чат», хотя бот добавлен. Модерировать он тут не может
        # физически — ни удалить, ни ограничить.
        note = ("\n\n⚠️ Это обычная группа, а не супергруппа. Telegram не даёт "
                "ботам права администратора в таких чатах — отсюда и ошибка при "
                "добавлении. Модерировать бот пока не может.\n"
                "Группа станет супергруппой сама, как только вы включите в ней "
                "историю сообщений для новых участников или назначите админа "
                "с ограничениями (Управление группой → Администраторы). "
                "Все настройки после этого перенесутся сами.")
    try:
        await bot.send_message(
            owner_id,
            f"✅ Бот добавлен в чат <b>{utils.esc(chat.title)}</b>.\n"
            f"Выдай права админа и открой /menu для настройки." + note,
        )
    except Exception:
        pass  # ещё не писал боту в личку


@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_MEMBER >> IS_NOT_MEMBER))
async def bot_removed(update: ChatMemberUpdated, bot: Bot) -> None:
    chat = update.chat
    if chat.type not in ("group", "supergroup"):
        return
    await db.set_chat_active(chat.id, False)
    await db.add_event(chat.id, "bot", f"удалён из чата «{chat.title}»")

    # Лог-чат заводят под конкретную группу. Бота из группы выгнали — сидеть
    # в её логе незачем, карточек оттуда больше не будет. Общий лог, лог
    # на несколько групп и рабочие чаты не трогаем.
    s = await db.get_settings(chat.id)
    if not s.log_chat_id:
        return
    reason = await db.log_chat_still_needed(s.log_chat_id, chat.id)
    if reason:
        logger.info("лог-чат %s оставлен: %s", s.log_chat_id, reason)
        return
    try:
        await bot.leave_chat(s.log_chat_id)
    except Exception:
        logger.warning("не выйти из лог-чата %s", s.log_chat_id, exc_info=True)
        return
    await db.set_chat_active(s.log_chat_id, False)
    await db.add_event(s.log_chat_id, "bot", f"лог-чат покинут вместе с {chat.id}")
    await db.set_setting(chat.id, "log_chat_id", None)


@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=MEMBER >> ADMINISTRATOR))
async def bot_promoted(update: ChatMemberUpdated, bot: Bot) -> None:
    if update.chat.type not in ("group", "supergroup"):
        return
    adm_cache.invalidate_admins(update.chat.id)


# ---------- ручные действия админов чата (нативные бан/мут) ----------

# Отдельного «кика» в Telegram нет: клиенты делают его баном и мгновенным
# разбаном, и боту прилетают два обновления подряд — member -> kicked, следом
# kicked -> left. Поверить первому значит выдать карточку «Бан (вручную)»
# с кнопкой «Разбанить» там, где банить никто не собирался.
#
# Поэтому решение откладываем и ждём второе обновление. Пришло — значит кик;
# не пришло за KICK_WAIT — переспрашиваем Telegram и, если человек всё ещё
# в бане, это настоящий бан. Часть клиентов (Telegram Desktop, «Удалить
# участника») именно банит, без всякого разбана, и тогда карточка бана верна.
KICK_WAIT = 2.5

# (чат, юзер) -> когда: свежие кики, чтобы второе обновление (снятие бана)
# не породило ещё и запись «снято наказание»
_kicked: dict[tuple[int, int], float] = {}
# кого сейчас разбираем: ключ -> сигнал «бан уже сняли, это был кик»
_deciding: dict[tuple[int, int], asyncio.Event] = {}


def _recent_kick(key: tuple[int, int]) -> bool:
    """Этого только что кикнули — второе обновление не новость.

    Метка одноразовая: гасим ею ровно один хвост кика. Иначе настоящий разбан,
    случившийся в ту же минуту, тоже остался бы незамеченным.
    """
    now = time.monotonic()
    for k, ts in list(_kicked.items()):
        if now - ts > 60:
            del _kicked[k]
    return _kicked.pop(key, None) is not None


@router.chat_member()
async def member_updated(update: ChatMemberUpdated, bot: Bot) -> None:
    chat = update.chat
    if chat.type not in ("group", "supergroup"):
        return
    old, new = update.old_chat_member, update.new_chat_member
    target = new.user
    # Кэш сбрасываем раньше всех проверок: состав чата поменялся независимо
    # от того, кто это сделал и не из бэклога ли событие. Раньше выходы по
    # actor == бот и stale() случались до сброса, и бот ещё четверть часа
    # считал участником того, кого сам же выгнал.
    adm_cache.invalidate_admins(chat.id)
    adm_cache.invalidate_member(chat.id, target.id)

    actor = update.from_user
    if actor is None or actor.id == bot.id:
        return  # свои действия уже закарточены в moderation
    if group.stale(update):
        return  # событие из бэклога — карточку слать поздно
    if new.status in ("member", "administrator", "creator"):
        # человек в чате: ссылка на возврат больше не нужна, даже если вошёл иначе
        await moderation.revoke_unban_link(bot, chat.id, target.id)
    if target.is_bot:
        return

    def _muted(m) -> bool:
        return m.status == "restricted" and getattr(m, "can_send_messages", True) is False

    old_banned, new_banned = old.status == "kicked", new.status == "kicked"
    old_muted, new_muted = _muted(old), _muted(new)

    kind = None
    until = None
    if new_banned and not old_banned:
        kind = "ban"
    elif new_muted and not old_muted:
        kind = "mute"
        ud = getattr(new, "until_date", None)
        until = int(ud.timestamp()) if ud else None
    elif (old_banned or old_muted) and not (new_banned or new_muted):
        waiting = _deciding.get((chat.id, target.id))
        if waiting is not None:
            waiting.set()   # это хвост кика: разбудим того, кто ждёт развязки
            return
        if _recent_kick((chat.id, target.id)):
            return          # хвост кика, а не отдельное «снял наказание»
        # сняли вручную — в том числе restricted -> restricted с вернувшимися правами
        await db.deactivate_user_punishments(chat.id, target.id)
        await db.add_event(
            chat.id, "admin_action", f"снято наказание: {target.full_name} ({target.id}) by {actor.id}"
        )
        return

    if kind is None:
        return

    if kind == "ban":
        # решаем не сразу: сперва надо понять, бан это или кик
        asyncio.create_task(_ban_or_kick(bot, chat, target, actor))
        return

    pid = await db.add_punishment(
        chat.id, target.id, target.username, target.full_name,
        kind, "вручную админом чата", until, actor.id,
    )
    card = (
        f"{moderation.KIND_EMOJI[kind]} <b>{moderation.KIND_LABEL[kind]}</b> (вручную) · {utils.esc(chat.title)}\n"
        f"👤 {utils.mention(target.id, target.full_name, target.username)} (<code>{target.id}</code>)\n"
        + (f"⏰ До: {utils.fmt_ts(until)}\n" if kind == 'mute' else "")
        + f"👮 Кем: {utils.mention(actor.id, actor.full_name, actor.username)}"
    )
    await db.add_event(
        chat.id, "admin_action", f"{kind}: {target.full_name} ({target.id}) by {actor.id}"
    )
    await moderation.send_card(bot, chat.id, config.BIT_ADMIN, card, pid, kind)


async def _ban_or_kick(bot: Bot, chat, target, actor) -> None:
    """Дождаться развязки и отчитаться тем, что случилось на самом деле.

    Остался в бане — карточка бана с кнопкой «Разбанить». Бан уже снят
    (то есть человека просто удалили из чата) — карточка кика: разбанивать
    нечего, зато можно забанить по-настоящему, если админ передумал.
    """
    key = (chat.id, target.id)
    lifted = asyncio.Event()
    _deciding[key] = lifted
    try:
        try:
            # разбан пришёл сам — ждать и спрашивать больше нечего
            await asyncio.wait_for(lifted.wait(), KICK_WAIT)
            banned = False
        except asyncio.TimeoutError:
            try:
                member = await bot.get_chat_member(chat.id, target.id)
                banned = member.status == "kicked"
            except Exception:
                logger.warning("не спросить статус %s в %s", target.id, chat.id,
                               exc_info=True)
                banned = True      # не знаем — считаем баном, как раньше
    finally:
        _deciding.pop(key, None)

    kind = "ban" if banned else "kick"
    if not banned and not lifted.is_set():
        # разбан мы узнали опросом, значит его обновление ещё в пути —
        # пометим, чтобы оно не превратилось в отдельное «снял наказание».
        # Пришло оно раньше (lifted) — метка не нужна, иначе она проглотит
        # следующее настоящее снятие бана
        _kicked[key] = time.monotonic()
    pid = await db.add_punishment(
        chat.id, target.id, target.username, target.full_name,
        kind, "вручную админом чата", None, actor.id,
    ) if banned else None
    card = (
        f"{moderation.KIND_EMOJI[kind]} <b>{moderation.KIND_LABEL[kind]}</b> "
        f"(вручную) · {utils.esc(chat.title)}\n"
        f"👤 {utils.mention(target.id, target.full_name, target.username)} "
        f"(<code>{target.id}</code>)\n"
        f"👮 Кем: {utils.mention(actor.id, actor.full_name, actor.username)}"
    )
    await db.add_event(
        chat.id, "admin_action", f"{kind}: {target.full_name} ({target.id}) by {actor.id}")
    await moderation.send_card(bot, chat.id, config.BIT_ADMIN, card, pid, kind,
                               target.id)


# ---------- смена названия чата ----------

@router.message(F.migrate_to_chat_id)
async def chat_migrated(message: Message, bot: Bot) -> None:
    """Группу повысили до супергруппы — переносим её на новый id.

    Telegram присылает об этом два служебных сообщения: одно в старую группу
    (с migrate_to_chat_id), другое в новую (с migrate_from_chat_id). Хватает
    любого, второй раз перенос просто ничего не находит.

    Без этого чат для бота становится чужим: настройки, вайтлисты, стоп-слова
    и копилка улик остаются на старом id, а в новом чате бот молчит.
    """
    old_id, new_id = message.chat.id, message.migrate_to_chat_id
    if not await db.migrate_chat(old_id, new_id):
        return
    await db.add_event(new_id, "bot",
                       f"чат повышен до супергруппы: {old_id} -> {new_id}")
    logger.info("чат %s переехал на %s", old_id, new_id)

    ch = await db.get_chat(new_id)
    owner_id = ch["owner_id"] if ch else None
    if not owner_id:
        return
    try:
        await bot.send_message(
            owner_id,
            f"ℹ️ Чат <b>{utils.esc(ch['title'])}</b> стал супергруппой, "
            f"Telegram выдал ему новый id (<code>{new_id}</code>).\n"
            f"Настройки, списки и наказания перенесены, делать ничего не нужно. "
            f"Проверьте только, что у бота остались права администратора.")
    except Exception:
        pass          # владелец ещё не писал боту в личку


@router.message(F.migrate_from_chat_id)
async def chat_migrated_new(message: Message, bot: Bot) -> None:
    """То же со стороны новой супергруппы — на случай, если первое сообщение
    бот не увидел (лежал, был добавлен позже)."""
    old_id, new_id = message.migrate_from_chat_id, message.chat.id
    if await db.migrate_chat(old_id, new_id):
        await db.add_event(new_id, "bot",
                           f"чат повышен до супергруппы: {old_id} -> {new_id}")
        logger.info("чат %s переехал на %s (со стороны супергруппы)", old_id, new_id)


@router.message(F.new_chat_title)
async def title_changed(message: Message) -> None:
    if not await db.get_chat(message.chat.id):
        return
    await db.update_chat_title(message.chat.id, message.new_chat_title, message.chat.username)
    # чистка служебного сообщения — здесь, иначе group-хендлер до него не доберётся
    s = await db.get_settings(message.chat.id)
    if s.service_other:
        try:
            await message.delete()
        except Exception:
            pass


# ---------- заявки на вступление ----------

@router.chat_join_request()
async def join_request(update, bot: Bot) -> None:
    """Заявку от недавно разбаненного одобряем сами.

    Чат закрыт: попасть в него можно только по ссылке с подтверждением. Разбан
    сам по себе туда не возвращает, поэтому вместе со ссылкой в лог-чат кладём
    «пропуск» на config.UNBAN_PASS_HOURS — по нему заявка проходит без админа.
    Всем остальным заявку не трогаем: решают админы чата.
    """
    chat_id = update.chat.id
    user = update.from_user
    if await db.get_chat(chat_id) is None:
        return
    if not await moderation.unban_pass_valid(chat_id, user.id):
        return
    try:
        await bot.approve_chat_join_request(chat_id, user.id)
    except Exception:
        logger.warning("approve join request failed in %s for %s", chat_id, user.id,
                       exc_info=True)
        return
    # вернулся — пропуск и ссылка своё отработали
    await moderation.revoke_unban_link(bot, chat_id, user.id)
    adm_cache.invalidate_member(chat_id, user.id)
    await db.add_event(
        chat_id, "join", f"заявка одобрена после разбана: {user.full_name} ({user.id})"
    )
    s = await db.get_settings(chat_id)
    if s.log_chat_id:
        try:
            await bot.send_message(
                s.log_chat_id,
                f"✅ <b>Заявка одобрена</b> · "
                f"{utils.mention(user.id, user.full_name, user.username)} "
                f"(<code>{user.id}</code>) — возврат после разбана",
            )
        except Exception:
            pass
