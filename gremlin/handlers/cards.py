"""Кнопки на карточках в лог-чате: снять наказание / подтвердить."""
from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery

from .. import db

router = Router()


@router.callback_query(F.data.startswith("k:lift:"))
async def card_lift(cb: CallbackQuery, bot: Bot) -> None:
    from ..services import moderation
    pid = int(cb.data.split(":")[2])
    ok, text, link = await moderation.lift_punishment(bot, pid)
    if not ok:
        await cb.answer(text, show_alert=True)
        return
    # ссылку дописываем в саму карточку — отдельный пост только замусорил бы лог
    await cb.message.edit_text(
        cb.message.html_text + "\n\n✅ <b>Наказание снято</b>" + moderation.unban_note(link),
        reply_markup=None, disable_web_page_preview=True,
    )
    await db.add_event(None, "card", f"снято наказание #{pid} юзером {cb.from_user.id}")
    await cb.answer("Снято")


@router.callback_query(F.data.startswith("k:ban:"))
async def card_ban(cb: CallbackQuery, bot: Bot) -> None:
    """Забанить по карточке (мут/удаление/подозрение -> бан)."""
    _, _, chat_id, user_id = cb.data.split(":")
    chat_id, user_id = int(chat_id), int(user_id)
    try:
        await bot.ban_chat_member(chat_id, user_id)
    except Exception as e:
        await cb.answer(f"Не получилось: {e}", show_alert=True)
        return
    await db.deactivate_user_punishments(chat_id, user_id)
    await db.add_punishment(
        chat_id, user_id, None, None, "ban", "бан из карточки", None, cb.from_user.id
    )
    await db.add_event(chat_id, "card", f"бан из карточки: {user_id} by {cb.from_user.id}")
    await cb.message.edit_text(
        cb.message.html_text + "\n\n⛔ <b>Забанен</b>", reply_markup=None
    )
    await cb.answer("Забанен")


# ---------- карточка наблюдения: «не трогать» ----------

@router.callback_query(F.data == "k:wok")
async def card_watch_ok(cb: CallbackQuery) -> None:
    await cb.message.edit_text(
        cb.message.html_text + "\n\n🕊 <b>Оставлен под наблюдением</b>", reply_markup=None
    )
    await cb.answer()
