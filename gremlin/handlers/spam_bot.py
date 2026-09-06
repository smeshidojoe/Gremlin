"""Сборщик спама — второй бот, живущий в том же процессе.

Зачем отдельный бот. Научить нейрофильтр спаму, который он пропустил, до сих
пор было нечем: ручные наказания в обучение не идут (причины у людей свои),
а «разметить спорное» работает только с тем, что бот уже собрал сам. Здесь
владелец пересылает найденный спам, размечает кнопками, и он попадает в общий
стартовый набор — тот, с которого начинает каждый молодой чат.

Почему в том же процессе, а не отдельным контейнером: база одна, и писать в
неё должен один процесс. Два писателя в SQLite через докеровский том мы уже
проходили, кончилось битым файлом. У aiogram start_polling принимает несколько
ботов, так что второй токен ничего не усложняет.

Диспетчер у сборщика свой. Общий не годится: у основного бота есть сквозной
обработчик всех сообщений для модерации, и он бы взялся разбирать переписку
в личке со сборщиком.

Отвечает бот только владельцу. Остальным молчит: это не публичный сервис,
а рабочий инструмент, и объясняться с посторонними ему незачем.
"""
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import config, db, utils

logger = logging.getLogger("gremlin.spam_bot")

router = Router()

# разобранное, ждущее разметки: id сообщения бота -> (текст, что за профиль)
_pending: dict[int, tuple[str, str]] = {}
PENDING_MAX = 500

# Что именно собираем прямо сейчас. Влияет только на присланный руками текст:
# у пересылки и так видно, где сообщение, а где профиль.
#   'msg'  — текст пойдёт примером сообщения
#   'prof' — примером профиля (имя, «о себе», канал одной строкой)
_mode = "msg"
MODE_LABEL = {"msg": "📨 сообщения", "prof": "🪪 профили"}

# основной бот: у сборщика нет общих чатов с людьми, и профиль спросить может
# только он. Ставится при запуске.
_main_bot: Bot | None = None


def set_main_bot(bot: Bot) -> None:
    global _main_bot
    _main_bot = bot


@router.message(F.from_user.id.not_in(config.ADMIN_IDS))
async def strangers(message: Message) -> None:
    """Чужим не отвечаем вовсе — бот не для них."""
    logger.info("сборщик: чужое сообщение от %s, игнорирую", message.from_user.id)


@router.callback_query(F.from_user.id.not_in(config.ADMIN_IDS))
async def stranger_clicks(cb: CallbackQuery) -> None:
    await cb.answer()


HELP = (
    "<b>🧪 Сборщик спама</b>\n\n"
    "Пересылайте сюда спам — по одному или пачкой. На каждое сообщение "
    "покажу, что из него удалось вытащить, и спрошу кнопками, спам это или "
    "обычная речь.\n\n"
    "Размеченное уходит в общий стартовый набор: с него начинает каждый "
    "новый чат, пока не накопит свои примеры.\n\n"
    "Можно и просто прислать текст — он пойдёт в набор тем видом, который "
    "выбран режимом.\n"
    "Обычная речь нужна не меньше спама: без неё фильтру не с чем сравнивать.\n\n"
    "<b>Профиль руками</b> — команда /профиль: пришлёт пустую форму, "
    "заполняете что знаете, лишние строки удаляете.\n\n"
    "/режим — сообщения или профили\n"
    "/стат — сколько уже собрано."
)


PROFILE_HELP = (
    "<b>🪪 Профиль руками</b>\n\n"
    "Скопируйте форму, заполните что знаете и пришлите обратно. Пустые "
    "строки можно удалить — бот их и так пропустит.\n\n"
    "<code>Имя: Анна | 18+\n"
    "Ник: @anna18\n"
    "О себе: пиши в лс, всё покажу\n"
    "Канал: Анюта18\n"
    "Описание канала: только для взрослых</code>\n\n"
    "Пустая форма — ниже, жмите на неё, чтобы скопировать:"
)


@router.message(Command("profile", "профиль"))
async def cmd_profile(message: Message) -> None:
    global _mode
    _mode = "prof"          # раз просят форму — явно собираем профили
    await message.answer(PROFILE_HELP)
    await message.answer(f"<code>{PROFILE_FORM}</code>")


def _mode_kb():
    b = InlineKeyboardBuilder()
    b.row(_btn(("• " if _mode == "msg" else "") + "📨 Сообщения", "m:msg"),
          _btn(("• " if _mode == "prof" else "") + "🪪 Профили", "m:prof"))
    return b.as_markup()


def _mode_text() -> str:
    return (f"<b>Сейчас собираю: {MODE_LABEL[_mode]}</b>\n\n"
            "Режим важен только для текста, присланного руками. У пересылки "
            "бот сам видит, где сообщение, а где профиль, и кладёт их по "
            "своим спискам: сообщения сравниваются с сообщениями, профили "
            "с профилями.")


@router.message(Command("mode", "режим"))
async def cmd_mode(message: Message) -> None:
    await message.answer(_mode_text(), reply_markup=_mode_kb())


@router.callback_query(F.data.startswith("m:"))
async def set_mode(cb: CallbackQuery) -> None:
    global _mode
    _mode = "prof" if cb.data.split(":")[1] == "prof" else "msg"
    await cb.message.edit_text(_mode_text(), reply_markup=_mode_kb())
    await cb.answer(f"Собираю {MODE_LABEL[_mode]}")


@router.message(Command("start", "help", "помощь"))
async def cmd_start(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("stat", "стат"))
async def cmd_stat(message: Message) -> None:
    msg = await db.seed_stats("msg")
    prof = await db.seed_stats("prof")
    await message.answer(
        f"<b>🌱 Стартовый набор</b>\n\n"
        f"<b>📨 Сообщения</b>\n"
        f"⛔ Спам: <b>{msg['spam']}</b> · 🕊 Норма: <b>{msg['ok']}</b>\n"
        f"В работе {config.NN_SEED_LIMIT} — поровну того и другого, и только "
        f"пока чат не набрал своих {config.NN_SEED_UNTIL}.\n\n"
        f"<b>🪪 Профили</b>\n"
        f"⛔ Спам: <b>{prof['spam']}</b>\n"
        f"В работе {config.NN_FACE_SEED}, не отключаются: рекламный профиль "
        f"одинаков в любом чате.\n\n"
        f"Сейчас собираю: {MODE_LABEL[_mode]} (/режим)\n"
        f"Смотреть и чистить набор удобнее в панели основного бота.")


# Поля профиля: что человек пишет слева от двоеточия -> как назовём внутри.
# Без формы люди присылали слипшийся текст и не понимали, что куда попало,
# поэтому просим ровно те поля, которые бот потом и проверяет.
PROFILE_FIELDS = {
    "имя": "Имя", "name": "Имя",
    "ник": "Ник", "юзернейм": "Ник", "username": "Ник",
    "о себе": "О себе", "описание": "О себе", "био": "О себе", "bio": "О себе",
    "канал": "Канал", "channel": "Канал",
    "описание канала": "Описание канала", "о канале": "Описание канала",
}

PROFILE_FORM = (
    "Имя: \n"
    "Ник: \n"
    "О себе: \n"
    "Канал: \n"
    "Описание канала: "
)


def parse_profile(raw: str) -> tuple[str, list[str]]:
    """Разобрать форму профиля -> (строка для набора, что распознали).

    Строку вида «Имя: Анна | 18+» кладём как «Имя: Анна | 18+», пустые поля
    выбрасываем. Строку без двоеточия берём как есть: не заставлять же
    подписывать каждую мелочь.
    """
    parts, seen = [], []
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            name = PROFILE_FIELDS.get(key.strip().lower())
            if name:
                val = val.strip()
                if val:
                    parts.append(f"{name}: {val}")
                    seen.append(name)
                continue
        parts.append(line)
        seen.append("свободная строка")
    return " · ".join(parts), seen


def _origin(message: Message) -> tuple[int | None, str]:
    """Кто автор пересланного: (id или None, как его назвать).

    id есть не всегда: при закрытых пересылках Telegram отдаёт одно имя
    строкой, и достать профиль тогда неоткуда.
    """
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return None, ""
    kind = getattr(origin, "type", None)
    kind = getattr(kind, "value", kind)
    if kind == "user":
        u = origin.sender_user
        name = u.full_name + (f" @{u.username}" if u.username else "")
        return u.id, name
    if kind == "hidden_user":
        return None, getattr(origin, "sender_user_name", "") or ""
    if kind == "channel":
        ch = origin.chat
        return None, (ch.title or "") + (f" @{ch.username}" if ch.username else "")
    if kind == "chat":
        return None, getattr(origin.sender_chat, "title", "") or ""
    return None, ""


async def _profile_of(user_id: int | None) -> str:
    """Описание профиля автора, если его удалось спросить."""
    if not user_id or _main_bot is None:
        return ""
    from ..services import profile as prof_svc
    data = await prof_svc.fetch(_main_bot, user_id)
    return prof_svc.text_of(data)


def _remember(msg_id: int, text: str, prof: str) -> None:
    if len(_pending) > PENDING_MAX:
        _pending.clear()          # разметку бросили на полпути, не жалко
    _pending[msg_id] = (text, prof)


@router.message()
async def collect(message: Message) -> None:
    """Разобрать присланное и спросить, спам это или норма."""
    raw = message.text or message.caption or ""
    forwarded = getattr(message, "forward_origin", None) is not None
    uid, who = _origin(message)
    prof = await _profile_of(uid)

    # Присланное руками — это то, что выбрано режимом. Пересылку разбираем
    # как есть: там видно, где сообщение, а где профиль его автора.
    seen: list[str] = []
    if not forwarded and _mode == "prof":
        prof, seen = parse_profile(raw)
        text = ""
    else:
        text = " ".join(raw.split())

    if len(text) < 10 and len(prof) < 10:
        hint = ("\nФорма профиля — /профиль." if _mode == "prof" else "")
        await message.reply(
            "Тут нечего запоминать: ни текста, ни описания профиля.\n"
            "Для набора нужен текст длиннее десяти символов." + hint)
        return

    lines = ["<b>Разобрал:</b>", ""]
    if who:
        lines.append(f"👤 Автор: {utils.esc(who)}"
                     + (f" (<code>{uid}</code>)" if uid else " · профиль скрыт"))
    if text:
        lines.append(f"💬 {utils.esc(utils.chunk(text, 400))}")
    if prof:
        lines.append(f"📝 Профиль: {utils.esc(utils.chunk(prof, 300))}")
    if seen:
        lines.append(f"<i>Поля: {utils.esc(', '.join(seen))}</i>")
    if not text and not seen:
        lines.append("<i>Текста сообщения нет — запомню только профиль.</i>")
    if not prof and uid:
        lines.append("<i>Профиль спросить не вышло.</i>")
    lines += ["", "Что это?"]

    b = InlineKeyboardBuilder()
    b.row(_btn("⛔ Спам", "s:spam"), _btn("🕊 Норма", "s:ok"))
    b.row(_btn("⏭ Пропустить", "s:skip"))
    sent = await message.reply("\n".join(lines), reply_markup=b.as_markup())
    _remember(sent.message_id, text, prof)


def _btn(text: str, data: str):
    from aiogram.types import InlineKeyboardButton
    return InlineKeyboardButton(text=text, callback_data=data)


@router.callback_query(F.data.startswith("s:"))
async def mark(cb: CallbackQuery) -> None:
    what = cb.data.split(":")[1]
    saved = _pending.pop(cb.message.message_id, None)
    if saved is None:
        await cb.answer("Это сообщение я уже забыл — пришлите заново.",
                        show_alert=True)
        return
    if what == "skip":
        await cb.message.edit_text(cb.message.html_text + "\n\n⏭ <b>Пропущено</b>")
        await cb.answer()
        return

    text, prof = saved
    label = "spam" if what == "spam" else "ok"
    added = 0
    # Текст и профиль кладём в разные списки: сообщение сравнивается с
    # сообщениями, профиль с профилями. Свалить их в одну кучу — значит
    # сравнивать строку из имени и био с обычной репликой в чате.
    if text and await db.seed_add(text, label, "msg"):
        added += 1
    if prof and await db.seed_add(prof, label, "prof"):
        added += 1
    await db.seed_commit()
    if added:
        from ..services import nn
        nn.invalidate()           # набор изменился, профили чатов устарели

    mark_text = "⛔ <b>В набор как спам</b>" if label == "spam" else "🕊 <b>В набор как норма</b>"
    if not added:
        mark_text = "🔁 <b>Уже было в наборе</b>"
    elif added == 2:
        mark_text += " — и в сообщения, и в профили"
    elif prof and not text:
        mark_text += " — в профили"
    await cb.message.edit_text(cb.message.html_text + "\n\n" + mark_text)
    await db.add_event(None, "nn", f"сборщик: {label} +{added} by {cb.from_user.id}")
    await cb.answer("Записал" if added else "Уже было")
