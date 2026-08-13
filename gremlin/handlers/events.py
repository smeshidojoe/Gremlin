"""События: бот добавлен/удалён, ручные баны админов, смена названия."""
import logging

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
    try:
        await bot.send_message(
            owner_id,
            f"✅ Бот добавлен в чат <b>{utils.esc(chat.title)}</b>.\n"
            f"Выдай права админа и открой /menu для настройки.",
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


@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=MEMBER >> ADMINISTRATOR))
async def bot_promoted(update: ChatMemberUpdated, bot: Bot) -> None:
    if update.chat.type not in ("group", "supergroup"):
        return
    adm_cache.invalidate_admins(update.chat.id)


# ---------- ручные действия админов чата (нативные бан/мут) ----------

@router.chat_member()
async def member_updated(update: ChatMemberUpdated, bot: Bot) -> None:
    chat = update.chat
    if chat.type not in ("group", "supergroup"):
        return
    actor = update.from_user
    if actor is None or actor.id == bot.id:
        return  # свои действия уже закарточены в moderation
    if group.stale(update):
        return  # событие из бэклога — карточку слать поздно

    adm_cache.invalidate_admins(chat.id)
    old, new = update.old_chat_member, update.new_chat_member
    target = new.user
    # состав поменялся — кэш «состоит в чате» для этого юзера устарел
    adm_cache.invalidate_member(chat.id, target.id)
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
        # сняли вручную — в том числе restricted -> restricted с вернувшимися правами
        await db.deactivate_user_punishments(chat.id, target.id)
        await db.add_event(
            chat.id, "admin_action", f"снято наказание: {target.full_name} ({target.id}) by {actor.id}"
        )
        return

    if kind is None:
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


# ---------- смена названия чата ----------

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
