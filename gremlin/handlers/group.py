"""Групповой пайплайн: команды !mute/!ban/!warn, капча и автомодерация всех сообщений."""
import asyncio
import logging
import time

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import config, db, utils
from ..services import (
    adm_cache, filters, moderation, net, resolve, triggers, trust, watch,
)

logger = logging.getLogger("gremlin.group")

router = Router()


async def _registered(message: Message) -> bool:
    """Чат зарегистрирован = бота позвал кто-то из допущенных. Остальные игнорируем."""
    return await db.get_chat(message.chat.id) is not None


def ts_of(obj) -> float | None:
    """Unix-время события. edit_date приходит числом, date — datetime."""
    stamp = getattr(obj, "edit_date", None) or getattr(obj, "date", None)
    if stamp is None:
        return None
    return stamp if isinstance(stamp, (int, float)) else stamp.timestamp()


def stale(obj) -> bool:
    """Сообщение из бэклога: бот лежал, а мы разгребаем накопившееся.

    На такие бот не отвечает и не наказывает — иначе после запуска в чат
    прилетает пачка запоздалых реакций. Счётчики при этом всё равно считаются.
    """
    ts = ts_of(obj)
    return ts is not None and time.time() - ts > config.MSG_MAX_AGE


# фильтры роутера: вся групповая логика работает только в «своих» чатах
router.message.filter(F.chat.type.in_({"group", "supergroup"}), _registered)
router.edited_message.filter(F.chat.type.in_({"group", "supergroup"}), _registered)

# ждут прохождения капчи: (chat_id, user_id) -> message_id капчи
_captcha_pending: dict[tuple[int, int], int] = {}
# кулдаун счётчиков: (id счётчика, user_id) -> monotonic последнего ответа.
# Персонально: иначе один человек «съедал» паузу для всего чата.
_cmd_fired: dict[tuple[int, int], float] = {}
# кулдаун гостей: (chat_id, id счётчика) -> monotonic. Общий на всех гостей сразу.
# Живёт в памяти: перезапуск его сбрасывает, но час паузы того не стоит хранить.
_guest_cmd_fired: dict[tuple[int, int], float] = {}
# последнее приветствие в чате (удаляем при новом входе, чтобы не спамить)
_last_welcome: dict[int, int] = {}
# кулдаун триггеров: (chat_id, trigger_id) -> monotonic
_trig_fired: dict[tuple[int, int], float] = {}


def _prune_cooldowns(now: float) -> None:
    """Убрать протухшие записи, чтобы словари не росли бесконечно."""
    day = 86400
    for store in (_cmd_fired, _guest_cmd_fired):
        if len(store) > 5000:
            for key in [k for k, ts in store.items() if now - ts > day]:
                del store[key]


@router.message(Command("start", "menu", "admin", ignore_mention=True))
async def ignore_bot_commands(message: Message) -> None:
    """В группе на /start и прочие личные команды бот не отвечает — меню только в личке.

    Само сообщение убираем: при добавлении по ссылке Telegram сам шлёт в чат
    «/start@бот», и он висит в ленте мусором.
    """
    try:
        await message.delete()
    except Exception:
        pass          # не админ ещё или сообщение чужое — не страшно


# ---------- команды админов чата: !mute !ban ----------

async def _is_chat_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    return user_id in await adm_cache.chat_admin_ids(bot, chat_id)


def _manual_card_text(kind: str, chat_title: str | None, target, reason: str,
                      until: int | None, by, body: str = "") -> str:
    who = utils.mention(target.id, target.full_name, target.username)
    admin = utils.mention(by.id, by.full_name, by.username)
    lines = [
        f"{moderation.KIND_EMOJI[kind]} <b>{moderation.KIND_LABEL[kind]}</b> · {utils.esc(chat_title)}",
        f"👤 {who} (<code>{target.id}</code>)",
        f"📎 Причина: {utils.esc(reason)}",
    ]
    if kind == "mute":
        lines.append(f"⏰ До: {utils.fmt_ts(until)}")
    lines.append(f"👮 Кем: {admin}")
    return "\n".join(lines) + body


async def _manual_punish(message: Message, bot: Bot, kind: str) -> None:
    """Общая логика !mute / !ban."""
    if stale(message):
        return  # команда из бэклога — админ уже всё разрулил сам
    if not await _is_chat_admin(bot, message.chat.id, message.from_user.id):
        return  # молча игнорим не-админов

    parts = (message.text or "").split()[1:]
    reply = message.reply_to_message
    if reply is not None and reply.from_user is None and reply.sender_chat is not None:
        # сообщение написано «от имени канала» — человека за ним не видно,
        # такому можно только запретить писать сюда как каналу
        await _punish_sender_chat(message, bot, kind, reply)
        return
    if reply and reply.from_user:
        target = reply.from_user
    else:
        # без реплая цель можно назвать: !mute @vasya 30 или !ban 12345 спам
        uid, _who = await _lift_target(message, bot)
        if uid is None or uid < 0:
            await message.reply(
                "Кого наказать? Ответьте на сообщение или укажите @ник или id."
            )
            return
        target = await net.user_stub(uid)
        parts = parts[1:]                       # первым словом шла цель
    if target.id == bot.id or await _is_chat_admin(bot, message.chat.id, target.id):
        await message.reply("Этого юзера наказать нельзя.")
        return

    mute_min = config.MANUAL_MUTE_DEFAULT      # срок не указали — сутки
    if kind == "mute" and parts:
        parsed = utils.parse_duration(parts[0])
        if parsed is not None:
            mute_min = parsed
            parts = parts[1:]
    reason = " ".join(parts) or "без причины"
    # текст берём до удаления — он уходит в карточку
    body = moderation.message_body(message.reply_to_message)  # None -> пусто

    pid, err = await moderation.punish_ex(
        bot, message.chat.id, target, kind, mute_min, reason, message.from_user.id
    )
    if pid is None:
        await message.reply(f"Не получилось: {utils.esc(err or 'Telegram отказал')}.")
        return
    until = utils.until_ts(mute_min) if kind == "mute" else None

    # В чат ничего не пишем: наказание и так видно в лог-чате, а сообщение
    # нарушителя вместе с командой убираем, чтобы лента осталась чистой.
    for msg in (message.reply_to_message, message):
        if msg is None:
            continue
        try:
            await msg.delete()
        except Exception:
            pass

    bit = config.BIT_BAN if kind == "ban" else config.BIT_MUTE
    card = _manual_card_text(
        kind, message.chat.title, target, reason, until, message.from_user, body,
    )
    sent = await moderation.send_card(bot, message.chat.id, bit, card, pid, kind)
    asyncio.create_task(net.spread_and_note(
        bot, sent, message.chat.id, target, kind, mute_min, reason,
        message.from_user.id,
    ))
    await db.add_event(
        message.chat.id, "manual",
        f"{kind}: {target.full_name} ({target.id}) — {reason} | by {message.from_user.id}",
    )


async def _punish_sender_chat(message: Message, bot: Bot, kind: str, reply) -> None:
    """!ban на сообщение от имени канала — бан самого канала-отправителя."""
    chan = reply.sender_chat
    if kind != "ban":
        await message.reply("Это сообщение от имени канала — мут тут не работает, "
                            "только <code>!ban</code>.")
        return
    try:
        await bot.ban_chat_sender_chat(message.chat.id, chan.id)
    except Exception as e:
        await message.reply(f"Не получилось: {utils.esc(moderation.human_error(e))}.")
        return
    pid = await db.add_punishment(
        message.chat.id, chan.id, chan.username, chan.title or str(chan.id),
        "banchan", "бан канала-отправителя", None, message.from_user.id,
        was_member=False,
    )
    for msg in (reply, message):
        try:
            await msg.delete()
        except Exception:
            pass
    card = (
        f"📛 <b>Бан отправителя-канала</b> · {utils.esc(message.chat.title)}\n"
        f"📢 {utils.esc(chan.title or chan.id)} (<code>{chan.id}</code>)\n"
        f"📎 Причина: бан командой\n"
        f"👮 Кем: {utils.mention(message.from_user.id, message.from_user.full_name, message.from_user.username)}"
    )
    await moderation.send_card(bot, message.chat.id, config.BIT_ANON, card, pid, "banchan")
    await db.add_event(message.chat.id, "manual",
                       f"banchan: {chan.title} ({chan.id}) | by {message.from_user.id}")


@router.message(F.text.regexp(r"^!(mute|мут)(\s|$)"))
async def cmd_mute(message: Message, bot: Bot) -> None:
    await _manual_punish(message, bot, "mute")


@router.message(F.text.regexp(r"^!(ban|бан)(\s|$)"))
async def cmd_ban(message: Message, bot: Bot) -> None:
    await _manual_punish(message, bot, "ban")


async def _misuse(message: Message, bot: Bot, s) -> None:
    """Команду модерации написал не админ: убираем и мутим, если так настроено.

    Вайтлист не трогаем — там свои люди, которым просто нечего тут делать.
    """
    user = message.from_user
    scopes = await db.wl_scopes_for(message.chat.id, user.id, user.username)
    try:
        await message.delete()
    except Exception:
        pass
    if not s.misuse_mute or scopes or user.id in config.ADMIN_IDS:
        return
    pid = await moderation.apply_punishment(
        bot, message.chat.id, user, "mute", s.misuse_mute,
        "команда модерации не от админа", None,
    )
    if pid is None:
        return
    card = _manual_card_text(
        "mute", message.chat.title, user, "команда модерации не от админа",
        utils.until_ts(s.misuse_mute), user,
        moderation.body_block(message.text or message.caption),
    ).replace("👮 Кем:", "🤖 Кем: Gremlin (автомод) ·")
    await moderation.send_card(bot, message.chat.id, config.BIT_MUTE, card, pid, "mute")
    await db.add_event(message.chat.id, "manual",
                       f"мут за чужую команду: {user.full_name} ({user.id})")


RULES_DELAY = 2.0    # даём Telegram завести тред обсуждения под постом


async def _post_rules(message: Message, chat_id: int) -> None:
    """Ответить на автопересылку поста заготовкой правил.

    Заготовок может быть несколько — берём случайную тем же механизмом, что
    и у триггеров. Пауза нужна, чтобы комментарий не улетел раньше треда.
    """
    await asyncio.sleep(RULES_DELAY)
    ans = await db.ans_pick("rules", chat_id)
    if ans is None:
        return
    try:
        await triggers.send_answer(message, ans)
    except Exception as e:
        if not utils.msg_gone(e):
            logger.warning("rules post failed in %s", chat_id, exc_info=True)


@router.message(F.text.regexp(r"^!(warn|варн)(\s|$)"))
async def cmd_warn(message: Message, bot: Bot) -> None:
    """!warn ответом: предупреждение с накоплением, наказание — по лимиту."""
    if stale(message):
        return
    s = await db.get_settings(message.chat.id)
    if not await _is_chat_admin(bot, message.chat.id, message.from_user.id):
        await _misuse(message, bot, s)
        return
    if not s.warns_on:
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply("Команда работает ответом на сообщение нарушителя.")
        return
    target = message.reply_to_message.from_user
    if target.id == bot.id or await _is_chat_admin(bot, message.chat.id, target.id):
        await message.reply("Этого юзера предупреждать нечем — он админ.")
        return

    reason = " ".join((message.text or "").split()[1:]) or "без причины"
    body = moderation.message_body(message.reply_to_message)
    count = await db.warn_add(message.chat.id, target, reason, message.from_user.id)

    for msg in (message.reply_to_message, message):
        try:
            await msg.delete()
        except Exception:
            pass

    who = utils.mention(target.id, target.full_name, target.username)
    admin = utils.mention(message.from_user.id, message.from_user.full_name,
                          message.from_user.username)
    card = (
        f"⚠️ <b>Варн {count}/{s.warns_limit}</b> · {utils.esc(message.chat.title)}\n"
        f"👤 {who} (<code>{target.id}</code>)\n"
        f"📎 Причина: {utils.esc(reason)}\n"
        f"👮 Кем: {admin}" + body
    )
    sent = await moderation.send_card(bot, message.chat.id, config.BIT_WARN, card,
                                      user_id=target.id)
    asyncio.create_task(net.warn_and_note(bot, sent, message.chat.id, target, reason,
                                          message.from_user.id))
    await db.add_event(
        message.chat.id, "warn",
        f"варн {count}/{s.warns_limit}: {target.full_name} ({target.id}) — {reason} "
        f"| by {message.from_user.id}",
    )
    if count < s.warns_limit:
        return

    # лимит добит: наказываем и гасим счётчик, чтобы отсчёт пошёл заново
    await db.warn_reset(message.chat.id, target.id)
    kind = s.warns_punish
    if s.trust_on:
        kind = trust.soften(kind, await trust.level(bot, message.chat.id, target.id, s),
                            s, config.TRUST_S_WARN)
    if kind == "delete":
        return                       # «удаление» уже случилось выше
    pid = await moderation.apply_punishment(
        bot, message.chat.id, target, kind, s.warns_mute_min,
        f"набрано {s.warns_limit} варнов", message.from_user.id,
    )
    if pid is None:
        return
    until = utils.until_ts(s.warns_mute_min) if kind == "mute" else None
    bit = config.BIT_BAN if kind == "ban" else config.BIT_MUTE
    punish_card = _manual_card_text(
        kind, message.chat.title, target, f"набрано {s.warns_limit} варнов",
        until, message.from_user,
    )
    sent = await moderation.send_card(bot, message.chat.id, bit, punish_card, pid, kind)
    asyncio.create_task(net.spread_and_note(
        bot, sent, message.chat.id, target, kind, s.warns_mute_min,
        f"набрано {s.warns_limit} варнов", message.from_user.id,
    ))
    await db.add_event(
        message.chat.id, "manual",
        f"{kind}: {target.full_name} ({target.id}) — лимит варнов | by {message.from_user.id}",
    )


async def _lift_target(message: Message, bot: Bot) -> tuple[int | None, str]:
    """Кого снимать: реплай, @ник или id из команды. Вернуть (id, подпись)."""
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        return u.id, utils.mention(u.id, u.full_name, u.username)
    parts = (message.text or "").split()
    if len(parts) < 2:
        return None, ""
    token = parts[1]
    if token.lstrip("-").isdigit():
        uid = int(token)
        return uid, f"<code>{uid}</code>"
    if token.startswith("@") and len(token) > 3:
        uid, name = await resolve.by_username(bot, token)
        return uid, utils.esc(name or token)
    return None, ""


@router.message(F.text.regexp(r"^!(unmute|размут|unban|разбан)(\s|$)"))
async def cmd_lift(message: Message, bot: Bot) -> None:
    """!unmute / !unban — снять наказание с того, на кого ответили или кого назвали."""
    if stale(message):
        return
    s = await db.get_settings(message.chat.id)
    if not await _is_chat_admin(bot, message.chat.id, message.from_user.id):
        await _misuse(message, bot, s)
        return

    word = (message.text or "").split(maxsplit=1)[0].lower().lstrip("!")
    unban = word in ("unban", "разбан")
    uid, who = await _lift_target(message, bot)
    if uid is None:
        await message.reply("Кого снимать? Ответьте на сообщение или укажите @ник или id.")
        return

    try:
        if uid < 0:
            await bot.unban_chat_sender_chat(message.chat.id, uid)
            done = True
        elif unban:
            member = await bot.get_chat_member(message.chat.id, uid)
            done = member.status == "kicked"
            if done:
                await bot.unban_chat_member(message.chat.id, uid, only_if_banned=True)
        else:
            await bot.restrict_chat_member(message.chat.id, uid,
                                           permissions=moderation.UNMUTE_PERMS)
            done = True
    except Exception as e:
        await message.reply(f"Не вышло: {utils.esc(str(e)[:100])}")
        return

    await db.deactivate_user_punishments(message.chat.id, uid)
    adm_cache.invalidate_member(message.chat.id, uid)
    trust.invalidate(message.chat.id, uid)
    try:
        await message.delete()
    except Exception:
        pass

    kind = "🔓 Разбан" if unban else "🔊 Размут"
    tail = "" if done else "\n<i>Наказания не было — снимать нечего.</i>"
    card = (
        f"{kind} · {utils.esc(message.chat.title)}\n"
        f"👤 {who} (<code>{uid}</code>)\n"
        f"👮 Кем: {utils.mention(message.from_user.id, message.from_user.full_name, message.from_user.username)}"
        f"{tail}"
    )
    bit = config.BIT_BAN if unban else config.BIT_MUTE
    sent = await moderation.send_card(bot, message.chat.id, bit, card)
    asyncio.create_task(net.lift_and_note(bot, sent, message.chat.id, uid))
    await db.add_event(
        message.chat.id, "manual",
        f"{'unban' if unban else 'unmute'}: {uid} | by {message.from_user.id}",
    )


@router.message(F.text.regexp(r"^!(dm|дм)(\s|$)"))
async def cmd_delete(message: Message, bot: Bot) -> None:
    """!dm ответом на сообщение — удалить его вместе с самой командой."""
    if stale(message):
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply("Команда работает ответом на сообщение.")
        return
    if not await _is_chat_admin(bot, message.chat.id, message.from_user.id):
        return  # молча игнорим не-админов
    target_msg = message.reply_to_message
    target = target_msg.from_user
    body = moderation.message_body(target_msg)     # текст сохраняем до удаления
    reason = " ".join((message.text or "").split()[1:]) or "без причины"

    try:
        await target_msg.delete()
    except Exception:
        await message.reply("Не получилось удалить — сообщение старше 48 часов или нет прав.")
        return
    try:
        await message.delete()                     # следом убираем саму команду
    except Exception:
        pass

    card = _manual_card_text(
        "delete", message.chat.title, target, reason, None, message.from_user, body
    )
    await moderation.send_card(bot, message.chat.id, config.BIT_ADMIN, card,
                               None, "delete", target.id)
    await db.add_event(
        message.chat.id, "admin_action",
        f"удаление сообщения: {target.full_name} ({target.id}) — {reason} "
        f"| by {message.from_user.id}",
    )


@router.message(F.text.contains("!") | F.caption.contains("!"))
async def chat_command(message: Message, bot: Bot) -> None:
    """Счётчики: команда в чате -> ответ по заготовке + счётчик вызовов.

    Ловим и подпись к медиа: команду часто пишут прямо под картинкой.
    Стоит выше системных !mute/!ban — но те перехватываются раньше по своим
    регуляркам, а сюда попадает всё остальное с «!».
    """
    s = await db.get_settings(message.chat.id)
    if not s.cmds_on:
        return
    text = (message.text or message.caption or "")
    row = await _find_cmd(message.chat.id, text, bool(s.cmds_anywhere))
    if row is None:
        return
    # вызов из бэклога: счётчик растёт, но в чат ничего не шлём — иначе после
    # запуска бот вывалил бы пачку запоздалых ответов
    if stale(message):
        await db.cmd_bump(row["id"])
        return

    now = time.monotonic()
    user = message.from_user
    if user is None:
        return
    _prune_cooldowns(now)

    # У участников кулдаун персональный: счётчик прирастает на единицу с человека
    # за период, а не достаётся самому быстрому. У гостей (кто в чате не состоит)
    # кулдаун на счётчик общий: вызвал один — команда закрыта для всех гостей.
    guest_cd = s.cmds_guest_cd
    is_guest = bool(guest_cd) and not await adm_cache.is_member(bot, message.chat.id, user.id)
    if is_guest:
        key, wait = (message.chat.id, row["id"]), guest_cd
        store = _guest_cmd_fired
    else:
        key, wait = (row["id"], user.id), row["cooldown"]
        store = _cmd_fired

    if wait and now - store.get(key, 0) < wait:
        # Сообщение убираем: иначе в чате копятся вызовы, на которые бот молчит.
        try:
            await message.delete()
        except Exception:
            pass          # нет прав на удаление — просто игнорируем вызов
        return            # вызов не засчитываем
    store[key] = now
    count = await db.cmd_bump(row["id"])
    ans = await db.ans_pick("cmd", row["id"])   # вариантов может быть несколько
    if ans is None:
        return
    # Ответы хранятся с разметкой (html_text при вводе), поэтому не экранируем.
    # Если разметка окажется битой, Telegram ругнётся — тогда шлём как есть текстом.
    body = f"{ans['text']} [{count}]"
    try:
        await message.reply(body)
    except Exception as e:
        if utils.msg_gone(e):
            return                       # сообщение с командой уже удалили
        try:
            await message.reply(utils.esc(body), parse_mode=None)
        except Exception as e2:
            if not utils.msg_gone(e2):
                logger.warning("counter %s failed in %s", row["id"], message.chat.id,
                               exc_info=True)


async def _link_punish(bot: Bot, chat_id: int, user_id: int, s,
                       kind: str) -> tuple[str, int]:
    """Наказание за ссылку: своё на каждый тип и отдельно для не участников.

    kind: tg | ext | men | fwd. Статус в чате спрашиваем, только если настройки
    участников и гостей для этого типа различаются — иначе лишний запрос в
    Telegram на каждое нарушение.
    """
    mine = (getattr(s, f"lp_{kind}"), getattr(s, f"lm_{kind}"))
    guest = (getattr(s, f"gp_{kind}"), getattr(s, f"gm_{kind}"))
    if mine == guest and not s.trust_on:
        return mine
    if s.trust_on:
        lvl = await trust.level(bot, chat_id, user_id, s)
        punish, minutes = guest if lvl == trust.GUEST else mine
        return trust.soften(punish, lvl, s, config.TRUST_LINK_BIT[kind]), minutes
    return mine if await adm_cache.is_member(bot, chat_id, user_id) else guest


async def _find_cmd(chat_id: int, text: str, anywhere: bool):
    """Найти счётчик в сообщении.

    По умолчанию — только если сообщение с него начинается, как обычная команда.
    С тумблером «в любом месте» — первое слово с «!», которое совпало.
    """
    # хвостовую пунктуацию срезаем: «!черви?» и «!черви!!!» — та же команда
    words = [w.rstrip("?!.,;:…\"'»)(") for w in text.split()]
    words = [w for w in words if len(w) > 1 and w.startswith("!")]
    if not words:
        return None
    if not anywhere:
        # команда должна открывать сообщение, иначе это просто разговор о ней
        first = text.split()[0].rstrip("?!.,;:…\"'»)(").lower()
        return await db.cmd_find(chat_id, first) if first.startswith("!") else None
    for w in words:
        row = await db.cmd_find(chat_id, w.lower())
        if row is not None:
            return row
    return None


async def fire_trigger(bot: Bot, message: Message, s) -> None:
    """Ответить триггером, если фраза совпала. Работает для всех, включая админов."""
    if not s.trig_on or message.edit_date is not None or stale(message):
        return
    low = (message.text or message.caption or "").lower()
    if not low:
        return
    now = time.monotonic()
    for t in await db.trig_list(message.chat.id):
        if not triggers.phrase_matches(t["phrase"], low):
            continue
        key = (message.chat.id, t["id"])
        if t["cooldown"] and now - _trig_fired.get(key, 0) < t["cooldown"]:
            continue  # этот на кулдауне — пробуем следующий совпавший
        _trig_fired[key] = now
        try:
            await triggers.send(message, t)
        except Exception as e:
            if utils.msg_gone(e):
                logger.info("триггер %s: отвечать уже некому, сообщение удалили", t["id"])
            else:
                logger.warning("trigger %s failed in %s", t["id"], message.chat.id,
                               exc_info=True)
        return  # один триггер на сообщение


# ---------- капча новичкам ----------

async def _captcha_timeout(bot: Bot, chat_id: int, user_id: int, timeout: int, msg_id: int) -> None:
    await asyncio.sleep(timeout)
    if _captcha_pending.pop((chat_id, user_id), None) is None:
        return  # уже прошёл
    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception:
        pass
    try:  # кик с возможностью вернуться (ban+unban)
        await bot.ban_chat_member(chat_id, user_id)
        await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
    except Exception:
        logger.warning("captcha kick failed in %s", chat_id, exc_info=True)
    await db.add_event(chat_id, "captcha", f"не прошёл капчу, кик: {user_id}")


@router.callback_query(F.data.startswith("capt:"))
async def captcha_pass(cb: CallbackQuery, bot: Bot) -> None:
    _, chat_id, user_id = cb.data.split(":")
    chat_id, user_id = int(chat_id), int(user_id)
    if cb.from_user.id != user_id:
        await cb.answer("Это капча не для тебя.", show_alert=True)
        return
    _captcha_pending.pop((chat_id, user_id), None)
    try:
        await bot.restrict_chat_member(chat_id, user_id, permissions=moderation.UNMUTE_PERMS)
    except Exception:
        logger.warning("captcha unmute failed in %s", chat_id, exc_info=True)
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.answer("Добро пожаловать!")
    await db.add_event(chat_id, "captcha", f"прошёл капчу: {user_id}")


# ---------- сервисные сообщения (вход/выход) ----------

@router.message(F.new_chat_members)
async def on_join(message: Message, bot: Bot) -> None:
    if stale(message):
        return  # новичок зашёл, пока бот лежал — встречать поздно
    s = await db.get_settings(message.chat.id)
    for user in message.new_chat_members or []:
        adm_cache.invalidate_member(message.chat.id, user.id)
        if not user.is_bot:
            await db.add_event(message.chat.id, "join", f"{user.full_name} ({user.id})")
    if s.welcome_on:
        humans = [u for u in message.new_chat_members or [] if not u.is_bot]
        if humans:
            names = ", ".join(
                utils.mention(u.id, u.first_name, u.username) for u in humans
            )
            old = _last_welcome.pop(message.chat.id, None)
            if old:
                try:
                    await bot.delete_message(message.chat.id, old)
                except Exception:
                    pass
            sent = await _welcome(message, s, names)
            if sent is not None:
                _last_welcome[message.chat.id] = sent
    if s.watch_on:
        admins = await adm_cache.chat_admin_ids(bot, message.chat.id)
        adder = message.from_user
        adder_is_admin = adder is not None and (
            adder.id in admins or adder.id in config.ADMIN_IDS
        )
        for user in message.new_chat_members or []:
            # чужие боты: банить, если добавил не-админ
            if user.is_bot and user.id != bot.id:
                if s.watch_bots and not adder_is_admin:
                    try:
                        await bot.ban_chat_member(message.chat.id, user.id)
                    except Exception:
                        logger.warning("bot ban failed in %s", message.chat.id, exc_info=True)
                        continue
                    pid = await db.add_punishment(
                        message.chat.id, user.id, user.username, user.full_name,
                        "ban", "чужой бот добавлен не-админом", None, None,
                    )
                    card = (
                        f"⛔ <b>Бан бота</b> · {utils.esc(message.chat.title)}\n"
                        f"🤖 @{user.username or user.id}\n"
                        f"📎 Причина: добавлен не-админом\n"
                        f"🤖 Кем: Gremlin (автомод)"
                    )
                    await db.add_event(message.chat.id, "watch", f"бан бота @{user.username} ({user.id})")
                    sent = await moderation.send_card(
                        bot, message.chat.id, config.BIT_WATCH, card, pid, "ban")
                    asyncio.create_task(net.spread_and_note(
                        bot, sent, message.chat.id, user, "ban", 0,
                        "чужой бот добавлен не-админом", None))
                continue
            # профиль новичка-человека
            if not user.is_bot and user.id not in admins and user.id not in config.ADMIN_IDS:
                scopes = await db.wl_scopes_for(message.chat.id, user.id, user.username)
                if not scopes & {"all", "watch"}:
                    await watch.check_user(bot, message.chat, user, s)
    if s.captcha_on:
        admins = await adm_cache.chat_admin_ids(bot, message.chat.id)
        for user in message.new_chat_members or []:
            if user.is_bot or user.id in admins or user.id in config.ADMIN_IDS:
                continue
            scopes = await db.wl_scopes_for(message.chat.id, user.id, user.username)
            if "all" in scopes:
                continue
            try:
                # Мут со сроком: снятие висит на задаче в памяти, и перезапуск бота
                # во время капчи оставлял человека немым навсегда. Срок ставим с
                # запасом — обычно мут снимает кнопка или кик по таймауту.
                await bot.restrict_chat_member(
                    message.chat.id, user.id,
                    permissions=moderation.mute_perms(s.mute_reactions),
                    until_date=int(time.time()) + s.captcha_timeout + 60,
                )
            except Exception:
                logger.warning("captcha restrict failed in %s", message.chat.id, exc_info=True)
                continue
            b = InlineKeyboardBuilder()
            b.button(text="✅ Я не бот", callback_data=f"capt:{message.chat.id}:{user.id}")
            sent = await message.answer(
                f"👋 {utils.mention(user.id, user.full_name, user.username)}, подтверди, "
                f"что ты человек — нажми кнопку за {s.captcha_timeout} сек, иначе кик.",
                reply_markup=b.as_markup(),
            )
            _captcha_pending[(message.chat.id, user.id)] = sent.message_id
            asyncio.create_task(
                _captcha_timeout(bot, message.chat.id, user.id, s.captcha_timeout, sent.message_id)
            )
    if s.service_join:
        try:
            await message.delete()
        except Exception:
            pass


async def _welcome(message: Message, s, names: str) -> int | None:
    """Поздороваться: вариант из списка, а если его нет — старый простой текст.

    Заготовки лежат там же, где ответы триггеров, поэтому умеют медиа и
    несколько вариантов — бот берёт случайный.
    """
    ans = await db.ans_pick("welcome", message.chat.id)
    try:
        if ans is not None:
            sent = await triggers.send_answer(message, ans, reply=False,
                                              subs={"{name}": names})
            return sent.message_id if sent else None
        if s.welcome_text:
            sent = await message.answer(s.welcome_text.replace("{name}", names))
            return sent.message_id
    except Exception:
        logger.warning("welcome failed in %s", message.chat.id, exc_info=True)
    return None


@router.message(F.left_chat_member)
async def on_leave(message: Message, bot: Bot) -> None:
    if stale(message):
        return
    u = message.left_chat_member
    if u:
        adm_cache.invalidate_member(message.chat.id, u.id)
    if u and not u.is_bot:
        await db.add_event(message.chat.id, "leave", f"{u.full_name} ({u.id})")
    s = await db.get_settings(message.chat.id)
    if s.service_leave:
        try:
            await message.delete()
        except Exception:
            pass


@router.message(
    F.pinned_message | F.new_chat_title | F.new_chat_photo | F.delete_chat_photo
    | F.group_chat_created | F.supergroup_chat_created | F.message_auto_delete_timer_changed
)
async def on_service_other(message: Message) -> None:
    """Прочие служебные: закреп, смена названия/фото и т.п."""
    if stale(message):
        return
    s = await db.get_settings(message.chat.id)
    if s.service_other:
        try:
            await message.delete()
        except Exception:
            pass


# ---------- автомодерация (catch-all, регистрируется последним) ----------

@router.message()
@router.edited_message()
async def moderate(message: Message, bot: Bot) -> None:
    chat = message.chat

    user = message.from_user

    # статистику ведём и по бэклогу: сообщения-то были
    if (user is not None and not user.is_bot and message.edit_date is None
            and user.id not in config.SERVICE_IDS):
        # служебный аккаунт Telegram приносит в обсуждение посты канала —
        # это не человек, и в статистике чата его быть не должно
        await db.msg_inc(chat.id, user.id, user.username, user.first_name)

    # а вот модерировать задним числом не надо — админы уже всё разрулили
    if stale(message):
        return

    s = await db.get_settings(chat.id)

    # --- сообщения от имени канала/группы (sender_chat) ---
    if message.sender_chat is not None:
        # анонимный админ этого же чата — ок; автопересылка из привязанного канала — ок
        if message.sender_chat.id == chat.id or message.is_automatic_forward:
            if message.is_automatic_forward and s.rules_on:
                asyncio.create_task(_post_rules(message, chat.id))
            return
        anon_body = moderation.message_body(message)   # текст берём до удаления
        sender_scopes = await db.wl_scopes_for(
            chat.id, message.sender_chat.id, message.sender_chat.username
        )
        if not s.anon_on or sender_scopes & {"all", "anon"}:
            return
        try:
            await message.delete()
        except Exception:
            pass
        try:
            await bot.ban_chat_sender_chat(chat.id, message.sender_chat.id)
        except Exception:
            logger.warning("ban_chat_sender_chat failed in %s", chat.id, exc_info=True)
            return
        title = message.sender_chat.title or str(message.sender_chat.id)
        pid = await db.add_punishment(
            chat.id, message.sender_chat.id, message.sender_chat.username, title,
            "banchan", "сообщение от имени канала/группы", None, None,
            was_member=False,
        )
        card = (
            f"📛 <b>Бан отправителя-канала</b> · {utils.esc(chat.title)}\n"
            f"📢 {utils.esc(title)} (<code>{message.sender_chat.id}</code>)\n"
            f"📎 Причина: сообщение от имени канала/группы\n"
            f"🤖 Кем: Gremlin (автомод)" + anon_body
        )
        await db.add_event(chat.id, "anon", f"banchan: {title} ({message.sender_chat.id})")
        await moderation.send_card(bot, chat.id, config.BIT_ANON, card, pid, "banchan")
        return

    if user is None or user.is_bot and user.id == bot.id:
        return

    # Ниже — только модерация, и от неё освобождены владелец бота, админы чата
    # и вайтлист. Но триггеры — развлекательная часть, они должны работать
    # для всех, поэтому перед выходом всё равно даём им сработать.
    if user.id in config.ADMIN_IDS:
        await fire_trigger(bot, message, s)
        return
    if user.id in await adm_cache.chat_admin_ids(bot, chat.id):
        await fire_trigger(bot, message, s)
        return
    scopes = await db.wl_scopes_for(chat.id, user.id, user.username)
    if "all" in scopes:
        await fire_trigger(bot, message, s)
        return

    # --- медиа-фильтры (удаление без наказания) ---
    if s.media_on and s.media_mask:
        for bit, attr, _label in config.MEDIA_BITS:
            if s.media_mask & bit and getattr(message, attr, None) is not None:
                try:
                    await message.delete()
                except Exception:
                    pass
                return

    # --- инлайн-боты (via_bot) ---
    if (s.inline_on and message.via_bot is not None and "inline" not in scopes
            and not await db.inline_wl_allowed(
                chat.id, message.via_bot.username, message.via_bot.id)):
        kind, mute_min = s.inline_punish, s.inline_mute_min
        if s.trust_on:
            kind = trust.soften(kind, await trust.level(bot, chat.id, user.id, s),
                                s, config.TRUST_S_INLINE)
        detail = f"@{message.via_bot.username}"
        # Гифка через @gif и рекламная простыня с кнопками на telegra.ph — разные
        # вещи, а правило одно. Поэтому за спам-содержимое наказание ужесточаем.
        if s.inline_spam:
            score, why = watch.score_content(
                message.text or message.caption or "", moderation.button_urls(message),
                self_bot=message.via_bot.username,
            )
            if score >= s.inline_spam:
                kind, mute_min = "ban", 0
                detail += f" — спам: {', '.join(why)} ({score} очков)"
        await moderation.violation(
            bot, message, config.BIT_INLINE, "инлайн-бот", kind, mute_min, detail,
        )
        return

    # --- пересылки: из каналов, групп и от людей ---
    if s.forwards_on and message.forward_origin is not None and "links" not in scopes:
        origin = message.forward_origin
        origin_chat = getattr(origin, "chat", None)
        origin_user = getattr(origin, "sender_user", None)
        source = None                    # что писать в причине; None = пересылку пропускаем
        if origin_chat is not None:
            if origin_chat.id != chat.id:      # свои же сообщения пересылать можно
                allowed = await db.wl_scopes_for(chat.id, origin_chat.id, origin_chat.username)
                if not allowed & {"all", "anon", "links"}:
                    kind = "канала" if origin_chat.type == "channel" else "чата"
                    source = f"из {kind}: {origin_chat.title or origin_chat.id}"
        elif origin_user is not None:
            if origin_user.id != user.id:      # своё же — не нарушение
                allowed = await db.wl_scopes_for(chat.id, origin_user.id, origin_user.username)
                if not allowed & {"all", "links"}:
                    source = f"от {origin_user.full_name}"
        else:
            # скрытый отправитель: id не отдают, есть только подпись
            source = f"от {getattr(origin, 'sender_user_name', 'скрытого отправителя')}"
        if source is not None:
            await moderation.violation(
                bot, message, config.BIT_LINKS, "пересылка",
                *await _link_punish(bot, chat.id, user.id, s, "fwd"), source,
            )
            return

    # «свои» цели: этот чат (по нику и по t.me/c/<id>), привязанный канал, сам бот
    own_names, own_ids = set(), set()
    if s.links_on or s.extlinks_on:
        me = await bot.me()
        own_names = {me.username, chat.username}
        own_ids = {chat.id}
        linked_id, linked_name, _ = await adm_cache.linked_chat(bot, chat.id)
        if linked_id:
            own_ids.add(linked_id)
        if linked_name:
            own_names.add(linked_name)
        for r in await db.wl_list(chat.id):    # разрешённые каналы — тоже свои
            if r["user_id"] and r["scope"] in ("all", "anon", "links"):
                own_ids.add(r["user_id"])
        for r in await db.link_wl_list(chat.id):   # свой список разрешённых ссылок
            if r["target_id"]:
                own_ids.add(r["target_id"])
            if r["username"]:
                own_names.add(r["username"])

    # --- ссылки на чужие тг-чаты/каналы ---
    if s.links_on and "links" not in scopes:
        links = [
            l for l in filters.find_tg_links(message)
            if not filters.link_allowed(l, own_names, own_ids)
        ]
        if links:
            await moderation.violation(
                bot, message, config.BIT_LINKS, "ссылка на сторонний чат",
                *await _link_punish(bot, chat.id, user.id, s, "tg"), links[0],
            )
            return
        # опционально: @упоминания каналов/групп (упоминания людей всегда ок)
        if s.mentions_check:
            known = {u.lower() for u in own_names if u}
            for uname in filters.mentions_in(message):
                if uname.lower() in known:
                    continue
                ctype = await adm_cache.username_chat_type(bot, uname)
                if ctype in ("channel", "supergroup", "group"):
                    await moderation.violation(
                        bot, message, config.BIT_LINKS, "упоминание стороннего чата",
                        *await _link_punish(bot, chat.id, user.id, s, "men"), f"@{uname}",
                    )
                    return

    # --- внешние ссылки (любые сайты) ---
    if s.extlinks_on and "links" not in scopes:
        ext = [
            l for l in filters.find_ext_links(message)
            if not filters.link_allowed(l, own_names, own_ids)
        ]
        if ext:
            await moderation.violation(
                bot, message, config.BIT_LINKS, "внешняя ссылка",
                *await _link_punish(bot, chat.id, user.id, s, "ext"), ext[0],
            )
            return

    # --- стоп-слова ---
    text = message.text or message.caption or ""
    if s.words_on and text and "words" not in scopes:
        word = await filters.match_stopword(chat.id, text)
        # words_guests: фильтр только для тех, кто в чате не состоит (комментаторы
        # под постами привязанного канала). Проверяем после поиска слова —
        # запрос к API нужен лишь когда слово реально нашлось.
        if word and s.words_guests and await adm_cache.is_member(bot, chat.id, user.id):
            word = None
        if word:
            kind = s.words_punish
            if s.trust_on:
                kind = trust.soften(kind, await trust.level(bot, chat.id, user.id, s),
                                    s, config.TRUST_S_WORDS)
            await moderation.violation(
                bot, message, config.BIT_WORDS, "стоп-слово",
                kind, s.words_mute_min, word,
            )
            return

    # --- антифлуд (только новые сообщения, не редактирования) ---
    if s.flood_on and message.edit_date is None and "flood" not in scopes:
        if filters.flood_hit(chat.id, user.id, s.flood_msgs, s.flood_window):
            kind = "mute"
            if s.trust_on:
                kind = trust.soften(kind, await trust.level(bot, chat.id, user.id, s),
                                    s, config.TRUST_S_FLOOD)
            await moderation.violation(
                bot, message, config.BIT_FLOOD, "флуд",
                kind, s.flood_mute_min,
                f"{s.flood_msgs} сообщений за {s.flood_window} сек",
            )
            return

    # --- наблюдение за профилями (специфичные правила уже отработали) ---
    if s.watch_on and "watch" not in scopes:
        lvl = await trust.level(bot, chat.id, user.id, s) if s.trust_on else None
        await watch.check_user(bot, chat, user, s, message, lvl)

    # --- триггеры (последними: на удалённое модерацией не отвечаем) ---
    await fire_trigger(bot, message, s)
