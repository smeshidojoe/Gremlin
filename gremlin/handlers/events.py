"""События: бот добавлен/удалён, ручные баны админов, смена названия."""
import asyncio
import logging
import time

from aiogram import Bot, F, Router
from aiogram.filters import (
    ADMINISTRATOR, IS_MEMBER, IS_NOT_MEMBER, MEMBER, ChatMemberUpdatedFilter,
)
from aiogram.types import (
    CallbackQuery, ChatJoinRequest, ChatMemberUpdated, InlineKeyboardButton,
    Message, MessageReactionUpdated,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import config, db, runtime, utils
from ..services import adm_cache, moderation, raid
from . import group

logger = logging.getLogger("gremlin.events")

router = Router()


# ---------- бот добавили / убрали ----------

async def _allowed_admin(bot: Bot, chat_id: int) -> int | None:
    """Есть ли среди админов чата кто-то из допущенных к боту. Вернуть его id.

    Нужно там, где непонятно, кто позвал бота: добавить могли анонимный
    админ или человек через интерфейс канала, и тогда в обновлении нет автора.
    Отказывать в такой ситуации нельзя — чат-то свой.
    """
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except Exception:
        logger.warning("не спросить админов %s", chat_id, exc_info=True)
        return None
    for m in admins:
        u = getattr(m, "user", None)
        if u is None or u.is_bot:
            continue
        if u.id in config.ADMIN_IDS or await db.access_allowed(u.id, u.username):
            return u.id
    return None


@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> IS_MEMBER))
async def bot_added(update: ChatMemberUpdated, bot: Bot) -> None:
    chat = update.chat
    # каналы тоже регистрируем: модерировать там нечего, но чат нужен в списке —
    # его назначают лог-чатом, и без записи он для бота не существует
    if chat.type not in ("group", "supergroup", "channel"):
        return
    adder = update.from_user
    owner_id = adder.id if adder and not adder.is_bot else None

    # бот работает только там, куда его позвал кто-то из допущенных
    allowed = owner_id is not None and (
        owner_id in config.ADMIN_IDS
        or await db.access_allowed(owner_id, adder.username if adder else None)
    )
    if not allowed:
        # автора не видно (анонимный админ, добавление через канал) или он
        # не в списке — смотрим, нет ли среди админов чата допущенного
        by_admin = await _allowed_admin(bot, chat.id)
        if by_admin is not None:
            owner_id, allowed = by_admin, True
    if not allowed:
        # служебный канал (подписка, лог, привязанный к обсуждению) — не чужой:
        # его уже назначили в настройках, значит добавляют по делу
        serves, serves_owner = await db.serves_chat(chat.id)
        if serves:
            await db.upsert_chat(chat.id, chat.title, chat.username,
                                 owner_id or serves_owner, chat.type)
            logger.info("служебный чат %s (%s) зарегистрирован молча",
                        chat.id, chat.title)
            return
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

    await db.upsert_chat(chat.id, chat.title, chat.username, owner_id, chat.type)
    # привязанный канал спрашиваем один раз здесь — дальше списки берут его
    # из базы и в Telegram по этому поводу не ходят
    try:
        await adm_cache.refresh_linked(bot, chat.id)
    except Exception:
        logger.debug("привязанный канал %s не узнали", chat.id, exc_info=True)
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
    adm_cache.invalidate_bot_status(update.chat.id)


@router.my_chat_member()
async def bot_rights_changed(update: ChatMemberUpdated) -> None:
    """Всё остальное про самого бота: права поменяли, не снимая админки, или
    админку отобрали, оставив в чате. Карточка чата показывает статус бота,
    и после правки прав он должен обновиться сразу, а не через полминуты.

    Стоит последним: хендлеры выше ловят свои переходы раньше него.
    """
    adm_cache.invalidate_bot_status(update.chat.id)
    adm_cache.invalidate_admins(update.chat.id)


# Кого из ставивших реакции уже смотрели: (чат, юзер) -> когда.
# Реакции сыплются пачками, и без этого проверка шла бы на каждую.
_reacted: dict[tuple[int, int], float] = {}


@router.message_reaction()
async def reaction_put(update: MessageReactionUpdated, bot: Bot) -> None:
    """Реакцию поставил человек — заодно смотрим, что у него за профиль.

    Рекламные аккаунты часто вообще ничего не пишут: ставят реакцию, чтобы
    засветиться в чате и собрать переходы в профиль. Для правил такой человек
    невидим — он не отправил ни одного сообщения.
    """
    user = update.user
    if user is None or user.is_bot or update.chat.type not in ("group", "supergroup"):
        return
    if not update.new_reaction:
        return                       # реакцию сняли — смотреть нечего
    if not await db.get_chat(update.chat.id):
        return
    s = await db.get_settings(update.chat.id)
    if not (s.watch_on and s.watch_react):
        return
    if user.id in config.ADMIN_IDS:
        return
    # Реакция ведёт в наблюдение, поэтому и освобождение от наблюдения должно
    # действовать здесь. Раньше проверялся только полный игнор: человека,
    # прощённого кнопкой «больше не трогать: наблюдение», через 25 минут
    # забанило за поставленную реакцию — тем же правилом, за которое простили.
    # Сообщения и вход в чат этот уровень учитывали, реакции — нет.
    if await db.free_scopes(update.chat.id, user.id, user.username) & {"all", "watch"}:
        return

    key = (update.chat.id, user.id)
    now = time.monotonic()
    if now - _reacted.get(key, utils.NEVER) < config.REACTION_TTL:
        return
    if len(_reacted) > config.REACTION_KEEP:
        _reacted.clear()
    _reacted[key] = now
    if user.id in await adm_cache.chat_admin_ids(bot, update.chat.id):
        return

    from ..services import watch
    # сообщения нет — проверяем только профиль
    await watch.check_user(bot, update.chat, user, s, None, None, event="reaction")


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

    # Вход без служебного сообщения виден только здесь: в больших чатах
    # Telegram их прячет. Набег считаем по обоим путям, иначе половина
    # входов прошла бы мимо счёта
    if new.status == "member" and old.status in ("left", "kicked"):
        s = await db.get_settings(chat.id)
        action = await raid.note_join(bot, chat, target, s)
        if action:
            await raid.apply(bot, chat, target, s, action)

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
        runtime.spawn(_ban_or_kick(bot, chat, target, actor))
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
    # чат и человека передаём явно: без них «Забанить» под мутом уходила
    # с k:ban:None:None и падала на нажатии
    await moderation.send_card(
        bot, chat.id, config.BIT_ADMIN, card, pid, kind, target.id,
        markup=moderation.with_spam_button(
            moderation.card_kb(pid, kind, chat.id, target.id), chat.id, target.id))


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
    await moderation.send_card(
        bot, chat.id, config.BIT_ADMIN, card, pid, kind, target.id,
        markup=moderation.with_spam_button(
            moderation.card_kb(pid, kind, chat.id, target.id), chat.id, target.id))


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
async def join_request(update: ChatJoinRequest, bot: Bot) -> None:
    """Заявка на вступление. Два разных повода, и оба сюда.

    Первый: человек вернулся после разбана — у него есть «пропуск», и заявку
    одобряем сами. Второй: проверка подписки на канал. Ни то ни другое не
    подошло — заявку не трогаем, решают админы чата.

    Обработчик один, потому что aiogram отдаёт обновление первому подошедшему
    и дальше не идёт: два отдельных просто не работали бы.
    """
    chat_id = update.chat.id
    user = update.from_user
    if await db.get_chat(chat_id) is None:
        return
    if not await moderation.unban_pass_valid(chat_id, user.id):
        await _sub_join_request(update, bot)
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


# ---------- вход в чат только по подписке на канал ----------


class _Chat:
    """Заглушка чата для карточки: из callback приходит только id."""

    def __init__(self, chat_id: int, title: str):
        self.id = chat_id
        self.title = title

async def _sub_join_request(update: ChatJoinRequest, bot: Bot) -> None:
    """Пускаем, если человек подписан на канал.

    Молчим и ничего не решаем, когда проверка выключена, канала нет или
    спросить про подписку не вышло: заявка просто останется висеть, и её
    разберут админы руками. Закрывать вход всем из-за своей же ошибки в
    настройках — худшее, что тут можно сделать.
    """
    chat, user = update.chat, update.from_user
    if user is None or user.is_bot:
        return
    if not await db.get_chat(chat.id):
        return
    s = await db.get_settings(chat.id)
    if not s.sub_on:
        return

    from ..services import subscribe as sub
    target = await sub.target_channel(bot, chat.id, s)
    if not target:
        logger.warning("подписка: в чате %s канал не задан и не привязан", chat.id)
        return

    try:
        state = await sub.subscribed(bot, target, user.id)
    except sub.SubError as e:
        # Настройка сломана: канала нет или бот в нём не админ. Заявку не
        # трогаем — закрывать вход всем из-за своей же ошибки нельзя, — но и
        # молчать нельзя: снаружи это выглядит как «функция не работает».
        await _sub_broken(bot, chat, sub.note_problem(chat.id, str(e)))
        return
    sub.clear_problem(chat.id)

    if state:
        if s.sub_pass == "decline":
            # Вход закрыт всем: проверка подписки при этом не выключена и
            # продолжает работать — вернут «впустить», и чат откроется тем же
            # движением. Отказ тихий, как и у неподписанных.
            tries = sub.note_request(chat.id, user.id)
            try:
                await bot.decline_chat_join_request(chat.id, user.id)
            except Exception as e:
                logger.warning("заявку %s в %s не отклонить: %s",
                               user.id, chat.id, e)
                return
            sub.forget_wait(chat.id, user.id)
            await sub.note_event(chat.id, "отклонён", user, "— вход закрыт")
            if sub.card_due(tries):
                again = ("" if tries < 2 else
                         f"\n🔁 Заявка подряд {tries}-я, о промежуточных "
                         "не сообщал")
                await _sub_card(bot, chat, user, "🚫 <b>Заявка отклонена</b>",
                                "подписан, но вход сейчас закрыт" + again)
            return
        if s.sub_pass != "approve":
            # Чистое сито: бот отсекает неподписанных, а решение по остальным
            # оставляет админам. «Только по кнопке» ведёт себя тут так же:
            # сам бот не впускает, но оставляет ход человеку.
            # Карточка с кнопками — чтобы решать прямо из лог-чата, не идя в
            # список заявок: там не видно ни профиля, ни того, что подписка есть
            sub.forget_wait(chat.id, user.id)
            await sub.note_event(chat.id, "подписан", user, "— оставлен админам")
            # карточку на каждую повторную заявку не шлём: кнопки у прошлой
            # никуда не делись, а лог-чат от одного человека забивался
            if sub.card_due(sub.note_request(chat.id, user.id)):
                await _sub_ask_card(bot, chat, user, s)
            return
        try:
            await bot.approve_chat_join_request(chat.id, user.id)
        except Exception as e:
            logger.warning("заявку %s в %s не одобрить: %s", user.id, chat.id, e)
            return
        sub.forget(chat.id, user.id)
        await sub.note_event(chat.id, "впущен", user, "— подписан")
        await _sub_card(bot, chat, user, "✅ <b>Впущен по подписке</b>", "")
        return

    # Заявку можно отозвать и подать заново сколько угодно раз. Решаем по
    # каждой честно, а вот рассказывать об одном и том же человеке каждый раз
    # незачем: карточку показываем на первую и на ту, после которой попытки
    # уже похожи на долбёжку.
    tries = sub.note_request(chat.id, user.id)
    loud = sub.card_due(tries)
    # без разметки: в карточке пояснение проходит через esc()
    again = ("" if tries < 2 else
             f"\n🔁 Заявка подряд {tries}-я, о промежуточных не сообщал")

    # не подписан: сперва пишем в личку, потом решаем судьбу заявки —
    # после отклонения окно на сообщение Telegram закрывает.
    # В режиме отказа _sub_dm сам ничего не отправит: письмо там ни к чему.
    # Право написать спрашиваем отдельно от карточки: карточка бережёт лог-чат,
    # письмо — самого человека, и счёт у них общим быть не должен
    sent = (await _sub_dm(bot, chat, user, update.user_chat_id, target, s)
            if sub.dm_due(chat.id, user.id) else False)
    if s.sub_action == "hold":
        sub.remember(chat.id, user.id)
        note = "заявка ждёт подписки" + ("" if sent else ", но написать в личку не вышло")
        await sub.note_event(chat.id, "ждёт", user)
        if loud:
            await _sub_card(bot, chat, user, "⏳ <b>Заявка ждёт подписки</b>",
                            note + again)
        return
    try:
        await bot.decline_chat_join_request(chat.id, user.id)
    except Exception as e:
        logger.warning("заявку %s в %s не отклонить: %s", user.id, chat.id, e)
        return
    await sub.note_event(chat.id, "отклонён", user, "— не подписан")
    if loud:
        await _sub_card(bot, chat, user, "🚫 <b>Заявка отклонена</b>",
                        "не подписан на канал" + again)


async def _sub_broken(bot: Bot, chat, why: str) -> None:
    """Сказать владельцу чата, что проверка подписки сломана. Раз в сутки."""
    if not sub_warn_due(chat.id):
        return
    row = await db.get_chat(chat.id)
    owner = row["owner_id"] if row else None
    text = (f"⚠️ <b>Вход по подписке не работает</b> · "
            f"{utils.esc(chat.title or chat.id)}\n\n"
            f"{utils.esc(why)}.\n\n"
            "Заявки я не трогаю — закрывать вход всем из-за неверной настройки "
            "нельзя. Они висят и ждут вас.\n\n"
            "Чаще всего лечится так: добавьте бота администратором в канал, "
            "подписку на который проверяем. Особых прав не нужно — важен сам "
            "факт, что он админ.")
    await db.add_event(chat.id, "sub", f"проверка подписки сломана: {why}")
    for uid in filter(None, {owner, *config.ADMIN_IDS}):
        try:
            await bot.send_message(uid, text)
        except Exception:
            logger.info("не сказать %s о поломке подписки", uid, exc_info=True)


def sub_warn_due(chat_id: int) -> bool:
    from ..services import subscribe as sub
    return sub.warn_due(chat_id)


async def _sub_dm(bot: Bot, chat, user, user_chat_id: int | None,
                  target: int, s) -> bool:
    """Написать человеку, почему не пустили. True — дошло."""
    # Письмо привязано к режиму ожидания, а не к тумблеру: в режиме отказа
    # заявки уже нет, кнопка «я подписался» ничего бы не одобрила, и человек
    # получил бы письмо про действие, которого не существует.
    if s.sub_action != "hold" or not s.sub_dm or not user_chat_id:
        return False
    ans = await db.ans_pick("sub", chat.id)
    if ans is None:
        return False

    from ..services import subscribe as sub
    from ..services import triggers
    link = await sub.channel_link(bot, target)
    b = InlineKeyboardBuilder()
    if link:
        b.row(InlineKeyboardButton(text="📣 Подписаться", url=link))
    if s.sub_action == "hold":
        # кнопка нужна только придержанной заявке: отклонённую одобрять нечего
        b.row(InlineKeyboardButton(text="✅ Я подписался",
                                   callback_data=f"sub:chk:{chat.id}"))
    subs = {"{name}": utils.esc(user.full_name), "{chat}": utils.esc(chat.title or "")}
    try:
        await triggers.send_answer_to(bot, user_chat_id, ans, subs,
                                      b.as_markup() if b.buttons else None)
        # отмечаем только по факту отправки: иначе «уже писали» стояло бы и
        # там, где письма не было вовсе
        sub.dm_noted(chat.id, user.id)
        return True
    except Exception as e:
        # человек мог закрыть личку — это обычное дело, не ошибка
        logger.info("не написать в личку %s: %s", user.id, e)
        return False


async def _sub_ask_card(bot: Bot, chat, user, s) -> None:
    """Карточка «решайте сами»: кто просится и три кнопки.

    Шлём только там, где бот сознательно не решает за админа. Профиль
    подтягиваем, если проверка профилей включена: решать по одному имени
    неудобно, а лишний запрос тут не в тягость — заявки редки.
    """
    from ..services import moderation
    who = utils.mention(user.id, user.full_name, user.username)
    lines = [f"🙋 <b>Заявка на вступление</b> · {utils.esc(chat.title or chat.id)}",
             f"👤 {who} (<code>{user.id}</code>)",
             "📎 Подписан на канал — решение за вами"]
    if s.prof_on:
        try:
            from ..services import profile as prof_svc
            about = prof_svc.describe(await prof_svc.fetch(bot, user.id))
        except Exception:
            about = ""
            logger.debug("профиль %s для карточки заявки не узнали", user.id,
                         exc_info=True)
        if about:
            lines.append(utils.esc(about))
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="✅ Принять",
                               callback_data=f"sub:ok:{chat.id}:{user.id}"),
          InlineKeyboardButton(text="🚫 Отказать",
                               callback_data=f"sub:no:{chat.id}:{user.id}"))
    b.row(InlineKeyboardButton(text="⛔ Забанить",
                               callback_data=f"sub:ban:{chat.id}:{user.id}"))
    await moderation.send_card(bot, chat.id, config.BIT_SUB, "\n".join(lines),
                               markup=b.as_markup())


@router.callback_query(F.data.startswith("sub:ok:"))
async def sub_take(cb: CallbackQuery, bot: Bot) -> None:
    """Кнопка «Принять» на карточке заявки."""
    _, _, cid, uid = cb.data.split(":")
    cid, uid = int(cid), int(uid)
    from .cards import may_act
    if not await may_act(cb, cid):
        return
    try:
        await bot.approve_chat_join_request(cid, uid)
    except Exception as e:
        await cb.answer(f"Не вышло: {e}", show_alert=True)
        return
    adm_cache.invalidate_member(cid, uid)
    await db.add_event(cid, "sub", f"впущен админом из карточки: {uid}")
    await _sub_done(cb, "✅ <b>Впущен</b>")


@router.callback_query(F.data.startswith("sub:no:"))
async def sub_drop(cb: CallbackQuery, bot: Bot) -> None:
    """Кнопка «Отказать»: заявку отклоняем, человека не трогаем."""
    _, _, cid, uid = cb.data.split(":")
    cid, uid = int(cid), int(uid)
    from .cards import may_act
    if not await may_act(cb, cid):
        return
    try:
        await bot.decline_chat_join_request(cid, uid)
    except Exception as e:
        await cb.answer(f"Не вышло: {e}", show_alert=True)
        return
    await db.add_event(cid, "sub", f"заявка отклонена админом: {uid}")
    await _sub_done(cb, "🚫 <b>Отказано</b>")


@router.callback_query(F.data.startswith("sub:ban:"))
async def sub_ban(cb: CallbackQuery, bot: Bot) -> None:
    """Кнопка «Забанить»: и заявку долой, и дорогу закрыть.

    Через общий punish_ex, а не голым banChatMember: тогда бан попадает в
    список активных наказаний и снимается оттуда же, как любой другой.
    """
    from ..services import moderation
    _, _, cid, uid = cb.data.split(":")
    cid, uid = int(cid), int(uid)
    from .cards import may_act
    if not await may_act(cb, cid):
        return
    try:
        await bot.decline_chat_join_request(cid, uid)
    except Exception:
        logger.info("заявка %s в %s уже не висит", uid, cid, exc_info=True)
    stub = await _user_stub(uid)
    pid, err = await moderation.punish_ex(bot, cid, stub, "ban", 0,
                                          "заявка на вступление", cb.from_user.id,
                                          wipe=False)
    if pid is None:
        await cb.answer(f"Не вышло: {err or 'Telegram отказал'}", show_alert=True)
        return
    await db.add_event(cid, "sub", f"забанен из карточки заявки: {uid}")
    await _sub_done(cb, "⛔ <b>Забанен</b>")


async def _user_stub(uid: int):
    from ..services import net
    return await net.user_stub(uid)


async def _sub_done(cb: CallbackQuery, note: str) -> None:
    """Дописать итог в карточку и убрать кнопки — во всех её копиях.

    Карточка уходит и в лог чата, и в глобальный лог. Правилась только та,
    где нажали: в другом логе кнопки оставались живыми, и заявку можно было
    «решить» второй раз. Карточки наказаний так умеют давно — теперь и эта.
    """
    from ..services import moderation
    text = cb.message.html_text + "\n\n" + note
    try:
        await cb.message.edit_text(text, reply_markup=None)
    except Exception:
        logger.debug("карточку заявки не поправить", exc_info=True)
    else:
        moderation.remember_card(cb.message.chat.id, cb.message.message_id,
                                 text, None)
    await moderation.update_twins(cb.bot, cb.message.chat.id,
                                  cb.message.message_id, text)
    await cb.answer()


async def _sub_card(bot: Bot, chat, user, head: str, note: str) -> None:
    from ..services import moderation
    who = utils.mention(user.id, user.full_name, user.username)
    card = (f"{head} · {utils.esc(chat.title or chat.id)}\n"
            f"👤 {who} (<code>{user.id}</code>)"
            + (f"\n📎 {utils.esc(note)}" if note else ""))
    await moderation.send_card(bot, chat.id, config.BIT_SUB, card)


@router.callback_query(F.data.startswith("sub:chk:"))
async def sub_recheck(cb: CallbackQuery, bot: Bot) -> None:
    """Кнопка «я подписался» под сообщением в личке."""
    chat_id = int(cb.data.split(":")[2])
    s = await db.get_settings(chat_id)
    from ..services import subscribe as sub
    target = await sub.target_channel(bot, chat_id, s)
    if not target:
        await cb.answer("Канал больше не задан — напишите админам чата.",
                        show_alert=True)
        return

    try:
        state = await sub.subscribed(bot, target, cb.from_user.id)
    except sub.SubError:
        await cb.answer("Не получилось проверить подписку. Напишите админам "
                        "чата — у бота нет доступа к каналу.", show_alert=True)
        return
    if not state:
        link = await sub.channel_link(bot, target)
        await cb.answer(
            "Подписки пока не видно.\n\nПодпишитесь на канал по кнопке выше"
            + (f":\n{link}" if link else "")
            + "\n\nи нажмите эту кнопку ещё раз.", show_alert=True)
        return

    if s.sub_pass == "skip":
        # «не трогать» — значит совсем: иначе кнопка была бы дырой в настройке,
        # ради которой её и включили. Кому нужен обратный случай, ставит
        # «только по кнопке»: там ход остаётся за человеком
        await cb.answer("Подписка есть — спасибо! Заявку теперь смотрят админы "
                        "чата, ждите.", show_alert=True)
        return
    if not sub.waiting(chat_id, cb.from_user.id):
        await cb.answer("Заявка уже не висит — подайте её заново, теперь пустим.",
                        show_alert=True)
        return
    try:
        await bot.approve_chat_join_request(chat_id, cb.from_user.id)
    except Exception as e:
        logger.info("заявку %s в %s не одобрить по кнопке: %s",
                    cb.from_user.id, chat_id, e)
        await cb.answer("Заявка не нашлась — подайте её заново, теперь пустим.",
                        show_alert=True)
        sub.forget(chat_id, cb.from_user.id)
        return
    sub.forget(chat_id, cb.from_user.id)
    await sub.note_event(chat_id, "впущен", cb.from_user, "— по кнопке")
    ch = await db.get_chat(chat_id)
    chat = _Chat(chat_id, ch["title"] if ch else str(chat_id))
    await _sub_card(bot, chat, cb.from_user, "✅ <b>Впущен по подписке</b>",
                    "подписался и нажал кнопку")
    await cb.answer("Готово, вы в чате!", show_alert=True)
