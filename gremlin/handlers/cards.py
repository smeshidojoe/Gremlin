"""Кнопки на карточках в лог-чате: снять наказание / подтвердить."""
import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery

from .. import db

logger = logging.getLogger("gremlin.cards")

router = Router()


async def _mark(cb: CallbackQuery, note: str) -> bool:
    """Дописать итог в карточку. False — Telegram не дал её править.

    Кнопки живут вечно, а вот править своё сообщение бот может лишь 48 часов.
    Действие к этому моменту уже выполнено, поэтому молчать нельзя — итог
    покажем всплывашкой.
    """
    from ..services import moderation
    text = cb.message.html_text + note
    try:
        await cb.message.edit_text(text, reply_markup=None,
                                   disable_web_page_preview=True)
    except Exception:
        logger.warning("card edit failed (старше 48 часов?)", exc_info=True)
        return False
    # та же карточка лежит копией в другом логе — там кнопки тоже надо убрать
    await moderation.update_twins(cb.bot, cb.message.chat.id, cb.message.message_id, text)
    return True


@router.callback_query(F.data.startswith("k:lift:"))
async def card_lift(cb: CallbackQuery, bot: Bot) -> None:
    from ..services import moderation
    pid = int(cb.data.split(":")[2])
    p = await db.get_punishment(pid)             # чат берём до снятия, потом он нужен для лога
    ok, text, link = await moderation.lift_punishment(bot, pid)
    if not ok:
        await cb.answer(text, show_alert=True)
        return
    # ссылку дописываем в саму карточку — отдельный пост только замусорил бы лог
    clean = cb.message.html_text + "\n\n✅ <b>Наказание снято</b>"
    marked = await _mark(cb, "\n\n✅ <b>Наказание снято</b>" + moderation.unban_note(link))
    if marked and link and p is not None:
        # запомним карточку: ссылку из неё надо будет убрать при возврате человека
        # или перед тем, как Telegram перестанет давать править сообщение
        await moderation.remember_unban_card(
            p["chat_id"], p["user_id"], cb.message.chat.id, cb.message.message_id, clean
        )
    await db.add_event(
        p["chat_id"] if p else None, "card",
        f"снято наказание #{pid} юзером {cb.from_user.id}",
    )
    await cb.answer("Разбанен")


@router.callback_query(F.data.startswith("k:ban:"))
async def card_ban(cb: CallbackQuery, bot: Bot) -> None:
    """Забанить по карточке (мут/удаление/подозрение -> бан)."""
    _, _, chat_id, user_id = cb.data.split(":")
    chat_id, user_id = int(chat_id), int(user_id)
    from ..services import adm_cache
    member = await adm_cache.is_member(bot, chat_id, user_id)   # до бана
    try:
        await bot.ban_chat_member(chat_id, user_id)
    except Exception as e:
        await cb.answer(f"Не получилось: {e}", show_alert=True)
        return
    await db.deactivate_user_punishments(chat_id, user_id)
    await db.add_punishment(
        chat_id, user_id, None, None, "ban", "бан из карточки", None, cb.from_user.id,
        was_member=member,
    )
    await db.add_event(chat_id, "card", f"бан из карточки: {user_id} by {cb.from_user.id}")
    await _mark(cb, "\n\n⛔ <b>Забанен</b>")
    await cb.answer("Забанен")


# ---------- карточка наблюдения: «не трогать» ----------

@router.callback_query(F.data == "k:wok")
async def card_watch_ok(cb: CallbackQuery) -> None:
    ok = await _mark(cb, "\n\n🕊 <b>Оставлен под наблюдением</b>")
    await cb.answer("" if ok else "Оставлен под наблюдением")
