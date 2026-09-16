"""Кнопки на карточках в лог-чате: снять наказание / подтвердить."""
import logging
import types

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import db, runtime

logger = logging.getLogger("gremlin.cards")

router = Router()


async def _mark(cb: CallbackQuery, note: str, markup=None) -> bool:
    """Дописать итог в карточку. False — Telegram не дал её править.

    Кнопки живут вечно, а вот править своё сообщение бот может лишь 48 часов.
    Действие к этому моменту уже выполнено, поэтому молчать нельзя — итог
    покажем всплывашкой.

    markup — что оставить под карточкой вместо кнопок. Обычно ничего, но у
    снятого авто-наказания там появляется «больше не трогать за это».
    """
    from ..services import moderation
    text = cb.message.html_text + note
    try:
        await cb.message.edit_text(text, reply_markup=markup,
                                   disable_web_page_preview=True)
    except Exception:
        logger.warning("card edit failed (старше 48 часов?)", exc_info=True)
        return False
    # кнопок на карточке больше нет — пусть и приписка сетки об этом знает
    moderation.remember_card(cb.message.chat.id, cb.message.message_id, text,
                             markup)
    # та же карточка лежит копией в другом логе — там кнопки тоже надо убрать
    await moderation.update_twins(cb.bot, cb.message.chat.id, cb.message.message_id, text)
    return True


async def may_act(cb: CallbackQuery, chat_id: int) -> bool:
    """Кнопки карточек — только тем, кто и так вправе модерировать этот чат:
    его админам, его владельцу в боте и владельцу бота.

    Карточку видит любой, кто сидит в лог-чате, а лог-чатом бывает и сам чат.
    Раньше «Разбанить» или «Забанить» мог нажать кто угодно из них.
    """
    from .. import config
    from ..services import adm_cache
    uid = cb.from_user.id
    if uid in config.ADMIN_IDS:
        return True
    ch = await db.get_chat(chat_id)
    if ch is not None and ch["owner_id"] == uid:
        return True
    if uid in await adm_cache.chat_admin_ids(cb.bot, chat_id):
        return True
    await cb.answer("Эти кнопки — для админов чата.", show_alert=True)
    return False


@router.callback_query(F.data.startswith("k:lift:"))
async def card_lift(cb: CallbackQuery, bot: Bot) -> None:
    from ..services import moderation
    pid = int(cb.data.split(":")[2])
    p = await db.get_punishment(pid)             # чат берём до снятия, потом он нужен для лога
    if p is not None and not await may_act(cb, p["chat_id"]):
        return
    ok, text, link = await moderation.lift_punishment(bot, pid)
    if not ok:
        await cb.answer(text, show_alert=True)
        return
    # ссылку дописываем в саму карточку — отдельный пост только замусорил бы лог
    clean = cb.message.html_text + "\n\n✅ <b>Наказание снято</b>"
    # Наказание выдало правило, а человек оказался нормальным — предлагаем
    # выключить это правило лично для него. Только для авто: у ручных причины
    # свои, и прощать там нечего
    scope = moderation.forgive_scope(p["reason"]) if p is not None else None
    kb = _forgive_kb(pid, scope) if scope else None
    marked = await _mark(cb, "\n\n✅ <b>Наказание снято</b>"
                         + moderation.unban_note(link), kb)
    if marked and link and p is not None:
        # запомним карточку: ссылку из неё надо будет убрать при возврате человека
        # или перед тем, как Telegram перестанет давать править сообщение
        await moderation.remember_unban_card(
            p["chat_id"], p["user_id"], cb.message.chat.id, cb.message.message_id, clean
        )
    if p is not None:
        # админ отменил наказание — значит это был не спам. Такие улики
        # ценнее всего: именно на них видно, где правила ошибаются
        moved = await db.sample_relabel_by_pid(pid, "ok")
        if not moved:
            last = await db.sample_last_for(p["chat_id"], p["user_id"])
            if last is not None:
                # улику профиля оставляем в её списке: origin='profile' — это
                # адрес, по которому её ищет сравнение профилей, а не пометка
                # происхождения. Меняем только оценку
                keep = last["origin"] == "profile"
                await db.sample_relabel(last["id"], "ok",
                                        origin=None if keep else "card")
        from ..services import nn
        nn.invalidate(p["chat_id"])          # профиль изменился
    if p is not None:
        # снятие тоже расходится по сетке, если так настроено
        from ..services import net
        runtime.spawn(net.lift_and_note(
            bot, [(cb.message.chat.id, cb.message.message_id)],
            p["chat_id"], p["user_id"],
        ))
    await db.add_event(
        p["chat_id"] if p else None, "card",
        f"снято наказание #{pid} юзером {cb.from_user.id}",
    )
    await cb.answer("Разбанен")


def _forgive_kb(pid: int, scope: str):
    """Кнопка под снятым авто-наказанием: не применять к нему это правило."""
    from .. import config
    label = config.WL_SCOPE_LABELS.get(scope, scope)
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=f"🕊 Больше не трогать: {label}",
                               callback_data=f"k:fg:{pid}"))
    return b.as_markup()


@router.callback_query(F.data.startswith("k:fg:"))
async def card_forgive(cb: CallbackQuery) -> None:
    """«Фильтр ошибся» — больше не применять к этому человеку это правило."""
    from ..services import moderation
    pid = int(cb.data.split(":")[2])
    p = await db.get_punishment(pid)
    if p is None:
        await cb.answer("Наказание не найдено.", show_alert=True)
        return
    if not await may_act(cb, p["chat_id"]):
        return
    scope = moderation.forgive_scope(p["reason"])
    if scope is None:
        await cb.answer("Это наказание выдали руками — прощать нечего.",
                        show_alert=True)
        return
    from .. import config
    label = config.WL_SCOPE_LABELS.get(scope, scope)
    added = await db.forgive_add(p["chat_id"], p["user_id"], p["username"],
                                 p["name"], scope, p["reason"], cb.from_user.id)
    await db.add_event(p["chat_id"], "card",
                       f"прощён по правилу «{label}»: {p['user_id']} "
                       f"by {cb.from_user.id}")
    await _mark(cb, f"\n🕊 <b>Больше не трогаем: {label}</b>")
    await cb.answer("Прощён" if added else "Уже был прощён")


def _without_spam(markup) -> InlineKeyboardMarkup | None:
    """Кнопки карточки без «Спам-профиля»: нажатую второй раз показывать незачем."""
    if markup is None:
        return None
    rows = [row for row in markup.inline_keyboard
            if not any((b.callback_data or "").startswith("k:sp:") for b in row)]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


@router.callback_query(F.data.startswith("k:sp:"))
async def card_spam_profile(cb: CallbackQuery, bot: Bot) -> None:
    """«Спам-профиль» под ручным наказанием: профиль — в базу для сравнения."""
    from ..services import nn
    _, _, chat_id, user_id = cb.data.split(":")
    chat_id, user_id = int(chat_id), int(user_id)
    if not await may_act(cb, chat_id):
        return
    ok, note = await nn.remember_spam_profile(bot, chat_id, user_id)
    if not ok:
        await cb.answer(note, show_alert=True)
        return
    await db.add_event(chat_id, "card",
                       f"спам-профиль в базу: {user_id} by {cb.from_user.id}")
    await _mark(cb, "\n🧪 <b>Профиль записан в базу спама</b>",
                _without_spam(cb.message.reply_markup))
    await cb.answer("Записан")


async def _report_punish(cb: CallbackQuery, bot: Bot, kind: str) -> None:
    """Мут или бан по жалобе.

    Сообщение, на которое жаловались, удаляем и кладём в копилку уликой: за
    такое наказали — пусть фильтр учится. Текст берём из записи о жалобе,
    потому что самого сообщения к этому моменту уже нет.
    """
    from ..services import moderation, nn
    from .group import _reports
    _, _, chat_id, user_id, msg_id = cb.data.split(":")
    chat_id, user_id, msg_id = int(chat_id), int(user_id), int(msg_id)
    if not await may_act(cb, chat_id):
        return
    s = await db.get_settings(chat_id)
    rec = _reports.get((chat_id, msg_id))
    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception:
        pass          # сообщение уже удалили руками
    user = types.SimpleNamespace(id=user_id, username=None,
                                 full_name=await db.user_handle(user_id))
    pid = await moderation.apply_punishment(
        bot, chat_id, user, kind, s.report_mute_min if kind == "mute" else 0,
        "по жалобе участников", cb.from_user.id)
    if pid is None:
        await cb.answer("Не вышло: у бота нет прав.", show_alert=True)
        return
    if rec and rec.get("body"):
        await db.sample_add(chat_id, user_id, "card", "spam", rec["body"],
                            feature="жалоба", pid=pid)
        nn.invalidate(chat_id)
    await db.add_event(chat_id, "report",
                       f"{kind} по жалобе: {user_id} by {cb.from_user.id}")
    word = "Мут выдан" if kind == "mute" else "Забанен"
    await _mark(cb, f"\n\n✅ <b>{word} по жалобе</b>")
    await cb.answer(word)


@router.callback_query(F.data.startswith("k:raidoff:"))
async def card_raid_off(cb: CallbackQuery) -> None:
    """«Снять режим»: набег кончился раньше срока."""
    from ..services import raid
    chat_id = int(cb.data.split(":")[2])
    if not await may_act(cb, chat_id):
        return
    came = raid.stop(chat_id)
    await db.add_event(chat_id, "raid", f"режим снят вручную by {cb.from_user.id}")
    await _mark(cb, f"\n\n🔓 <b>Режим снят</b> · вошло за набег: {came}")
    await cb.answer("Снято")


@router.callback_query(F.data.startswith("k:raidban:"))
async def card_raid_ban(cb: CallbackQuery, bot: Bot) -> None:
    """«Забанить всех»: по списку вошедших за набег."""
    from ..services import raid
    chat_id = int(cb.data.split(":")[2])
    if not await may_act(cb, chat_id):
        return
    await cb.answer("Баню, это займёт время…")
    done = await raid.ban_all(bot, chat_id, cb.from_user.id)
    await db.add_event(chat_id, "raid",
                       f"бан всех по набегу: {done} by {cb.from_user.id}")
    await _mark(cb, f"\n\n⛔ <b>Забанено: {done}</b>")


@router.callback_query(F.data.startswith("k:rmute:"))
async def card_report_mute(cb: CallbackQuery, bot: Bot) -> None:
    await _report_punish(cb, bot, "mute")


@router.callback_query(F.data.startswith("k:rban:"))
async def card_report_ban(cb: CallbackQuery, bot: Bot) -> None:
    await _report_punish(cb, bot, "ban")


@router.callback_query(F.data.startswith("k:rdel:"))
async def card_report_delete(cb: CallbackQuery, bot: Bot) -> None:
    """Удалить сообщение, на которое пожаловались, никого не наказывая."""
    _, _, chat_id, msg_id = cb.data.split(":")
    chat_id, msg_id = int(chat_id), int(msg_id)
    if not await may_act(cb, chat_id):
        return
    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception:
        await cb.answer("Не вышло: сообщение старое или нет прав.", show_alert=True)
        return
    await db.add_event(chat_id, "report",
                       f"сообщение удалено по жалобе by {cb.from_user.id}")
    await _mark(cb, "\n\n🗑 <b>Сообщение удалено</b>")
    await cb.answer("Удалено")


@router.callback_query(F.data.startswith("k:rno:"))
async def card_report_drop(cb: CallbackQuery) -> None:
    """Жалоба не по делу: карточку закрываем, никого не трогаем."""
    chat_id = int(cb.data.split(":")[2])
    if not await may_act(cb, chat_id):
        return
    await _mark(cb, "\n\n✅ <b>Жалоба отклонена</b>")
    await cb.answer("Отклонена")


@router.callback_query(F.data.startswith("k:ban:"))
async def card_ban(cb: CallbackQuery, bot: Bot) -> None:
    """Забанить по карточке (мут/удаление/подозрение -> бан)."""
    _, _, chat_id, user_id = cb.data.split(":")
    chat_id, user_id = int(chat_id), int(user_id)
    if not await may_act(cb, chat_id):
        return
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
    # человек посмотрел на конкретное сообщение и подтвердил, что это спам
    from ..services import nn
    last = await db.sample_last_for(chat_id, user_id)
    if last is not None:
        await db.sample_relabel(last["id"], "spam", origin="card")
        nn.invalidate(chat_id)
    # и сам профиль — тем же видом строки, каким его потом сравнивают: имя,
    # ник, описание и канал. Раньше писались только имя с ником, и такие
    # записи сравнивались с полными профилями вполсилы
    await nn.remember_spam_profile(bot, chat_id, user_id)
    await _mark(cb, "\n\n⛔ <b>Забанен</b>")
    from ..services import net
    runtime.spawn(net.spread_id_and_note(
        bot, [(cb.message.chat.id, cb.message.message_id)], chat_id, user_id,
        "ban", 0, "бан из карточки", cb.from_user.id,
    ))
    await cb.answer("Забанен")


# ---------- карточка наблюдения: «не трогать» ----------

@router.callback_query(F.data == "k:wok")
async def card_watch_ok(cb: CallbackQuery) -> None:
    # чата наблюдения в кнопке нет — пускаем админов того чата, где карточка
    if not await may_act(cb, cb.message.chat.id):
        return
    ok = await _mark(cb, "\n\n🕊 <b>Оставлен под наблюдением</b>")
    await cb.answer("" if ok else "Оставлен под наблюдением")
