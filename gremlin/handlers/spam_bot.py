"""Сборщик спама — второй бот, живущий в том же процессе.

Зачем отдельный бот. Научить нейрофильтр спаму, который он пропустил, до сих
пор было нечем: ручные наказания в обучение не идут (причины у людей свои),
а «разметить спорное» работает только с тем, что бот уже собрал сам. Здесь
владелец пересылает найденный спам, размечает кнопками, и он попадает в общую
копилку — ту, по которой учится нейрофильтр и сравниваются профили во всех
чатах сразу.

Почему в том же процессе, а не отдельным контейнером: база одна, и писать в
неё должен один процесс. Два писателя в SQLite через докеровский том мы уже
проходили, кончилось битым файлом. У aiogram start_polling принимает несколько
ботов, так что второй токен ничего не усложняет.

Диспетчер у сборщика свой. Общий не годится: у основного бота есть сквозной
обработчик всех сообщений для модерации, и он бы взялся разбирать переписку
в личке со сборщиком.

Отвечает бот только владельцу. Остальным молчит: это не публичный сервис,
а рабочий инструмент, и объясняться с посторонними ему незачем.

Что хранится. Случай — это сообщение и профиль его автора, у каждого своя
метка: спам со взломанного аккаунта — спам, а профиль у аккаунта обычный.
Раньше одна кнопка ставила одну метку обоим, и в набор профилей попадали
безликие «Cornell Trevino» с пометкой «спам». Кроме строки для модели
пишутся и исходные поля (db.samples.data): текст, распознанное с картинки,
ссылки, кнопки, поля профиля по отдельности. Выгрузка /экспорт отдаёт их
случаями в JSONL — один формат для любой модели.
"""
import datetime as dt
import io
import json
import logging
import types

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import config, db, utils

logger = logging.getLogger("gremlin.spam_bot")

router = Router()

# разобранное, ждущее разметки: id сообщения бота -> случай (см. _case)
_pending: dict[int, dict] = {}
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

# Распознавание медиа — то же, что в чатах: обучение должно видеть тот же
# текст, что и проверка. В чате модель читает подпись вместе с распознанным
# с картинки, а сборщик раньше клал одну подпись, и картинка со спамом учила
# модель на пустой строке. Настроек чата у сборщика нет — включено всё.
_MEDIA = types.SimpleNamespace(ocr_on=1, ocr_langs="rus+eng", asr_on=1,
                               asr_max_sec=db.Settings.asr_max_sec)

# какие вложения бывают — для поля media
MEDIA_KINDS = ("photo", "video", "animation", "voice", "video_note", "audio",
               "document", "sticker")

# выгрузку больше этого Telegram боту не отдаст на скачивание
IMPORT_MAX_BYTES = 20 * 1024 * 1024


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
    "покажу, что из него удалось вытащить, и спрошу кнопками:\n"
    "⛔ <b>Всё спам</b> — и сообщение, и профиль автора;\n"
    "💬 <b>Спам только текст</b> — профиль обычный, его не записываю;\n"
    "🪪 <b>Спам только профиль</b> — сообщение обычное, реклама в профиле;\n"
    "🕊 <b>Всё норма</b>.\n\n"
    "Размеченное уходит в общую копилку: по ней учится нейрофильтр и "
    "сравниваются профили во всех чатах.\n\n"
    "Можно и просто прислать текст — он пойдёт в набор тем видом, который "
    "выбран режимом.\n"
    "Обычная речь нужна не меньше спама: без неё фильтру не с чем сравнивать.\n\n"
    "<b>Профиль руками</b> — команда /профиль: пришлёт пустую форму, "
    "заполняете что знаете, лишние строки удаляете.\n\n"
    "/режим — сообщения или профили\n"
    "/стат — сколько уже собрано\n"
    "/экспорт — выгрузить копилку файлом JSONL. Такой же файл, присланный "
    "сюда, загружается обратно."
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
    pool = await db.pool_stats()
    await message.answer(
        f"<b>🌱 Собрано здесь</b>\n"
        f"📨 Сообщения: ⛔ <b>{msg['spam']}</b> · 🕊 <b>{msg['ok']}</b>\n"
        f"🪪 Профили: ⛔ <b>{prof['spam']}</b> · 🕊 <b>{prof['ok']}</b>\n\n"
        f"<b>🗃 Вся копилка</b> — это плюс размеченное в чатах, учит все чаты сразу\n"
        f"📨 Сообщения: ⛔ {pool['msg']['spam']} · 🕊 {pool['msg']['ok']}\n"
        f"🪪 Профили: ⛔ {pool['prof']['spam']} · 🕊 {pool['prof']['ok']}\n"
        f"Разметил человек: {pool['msg']['human'] + pool['prof']['human']}, "
        f"решил бот сам: {pool['msg']['bot'] + pool['prof']['bot']}\n\n"
        f"Сейчас собираю: {MODE_LABEL[_mode]} (/режим)\n"
        f"Смотреть и чистить набор удобнее в панели основного бота.")


@router.message(Command("export", "экспорт"))
async def cmd_export(message: Message) -> None:
    """Копилка случаями в JSONL: одна строка — сообщение и профиль автора."""
    records = await db.pool_export()
    buf = io.StringIO()
    for rec in records:
        buf.write(json.dumps(rec, ensure_ascii=False) + "\n")
    name = f"gremlin-{dt.date.today().isoformat()}.jsonl"
    await message.answer_document(
        BufferedInputFile(buf.getvalue().encode("utf-8"), filename=name),
        caption=f"Случаев: {len(records)}. Пришлите такой файл сюда — загружу обратно.")


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


def read_form(raw: str) -> tuple[dict, list[str], list[str]]:
    """Разобрать форму профиля -> (поля копилки, свободные строки, что распознали).

    Пустые поля выбрасываем. Строку без двоеточия берём как есть, в конец:
    не заставлять же подписывать каждую мелочь.
    """
    from ..services import profile as prof_svc
    fields, free, seen = {}, [], []
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
                    fields[name] = val
                    seen.append(name)
                continue
        free.append(line)
        seen.append("свободная строка")
    return prof_svc.form_to_data(fields, free), free, seen


def parse_profile(raw: str) -> tuple[str, list[str]]:
    """Разобрать форму профиля -> (строка для набора, что распознали).

    Строку собираем тем же видом, каким бот проверяет живой профиль:
    «Анна @anna · о себе · канал · описание канала» — без подписей полей и в
    одном порядке, как бы форму ни заполнили. С подписями пример в наборе
    выглядел иначе, чем проверяемый профиль, и сходство выходило ниже
    настоящего.
    """
    from ..services import profile as prof_svc
    data, _free, seen = read_form(raw)
    return prof_svc.face_of_data(data), seen


def _forward_kind(message: Message) -> str | None:
    origin = getattr(message, "forward_origin", None)
    kind = getattr(origin, "type", None)
    return getattr(kind, "value", kind)


def _origin(message: Message) -> tuple[int | None, str]:
    """Кто автор пересланного: (id или None, как его назвать).

    id есть не всегда: при закрытых пересылках Telegram отдаёт одно имя
    строкой, и достать профиль тогда неоткуда.
    """
    origin = getattr(message, "forward_origin", None)
    kind = _forward_kind(message)
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


async def _face_of(message: Message) -> tuple[str, dict, bool]:
    """Профиль автора пересланного -> (строка, поля, удалось ли спросить).

    Строка — тем же видом, каким бот проверяет живой профиль: имя @ник · о
    себе · канал. От скрытого автора Telegram отдаёт одно имя — его не пишем
    вовсе: сигнала в голом имени нет, а с пометкой «спам» оно учит модель,
    что спамер всякий, кого зовут по-английски.
    """
    from ..services import profile as prof_svc
    if _forward_kind(message) != "user":
        return "", {}, True
    u = message.forward_origin.sender_user
    data = await prof_svc.fetch(_main_bot, u.id) if _main_bot else None
    return prof_svc.face_text(u, data), prof_svc.fields_of(u, data), bool(data)


def _buttons(message: Message) -> list[str]:
    """Кнопки под сообщением как есть: «подпись → ссылка»."""
    rows = getattr(getattr(message, "reply_markup", None), "inline_keyboard", None) or []
    out = []
    for row in rows:
        for b in row:
            label = getattr(b, "text", "") or "кнопка"
            url = getattr(b, "url", None)
            out.append(f"{label} → {url}" if url else label)
    return out


def _links(message: Message, seen: str) -> list[str]:
    """Все ссылки: из текста, спрятанные под словами и с кнопок. Без повторов."""
    from ..services import filters, moderation
    got = (filters.find_tg_links(message, seen) + filters.find_ext_links(message, seen)
           + moderation.button_urls(message))
    return list(dict.fromkeys(got))


async def _case(message: Message, bot: Bot) -> dict:
    """Разобрать присланное в случай: сообщение и профиль автора по отдельности.

    text — строка для модели сообщений, ровно такая, какую проверка видит в
    чате: подпись вместе с распознанным с картинки или голосового.
    """
    from ..services import media
    raw = message.text or message.caption or ""
    forwarded = _forward_kind(message) is not None
    uid, who = _origin(message)
    case = {"uid": uid, "who": who, "text": "", "msg": {}, "prof": "", "prof_data": {},
            "asked": True, "seen": []}

    if not forwarded and _mode == "prof":
        from ..services import profile as prof_svc
        data, _free, case["seen"] = read_form(raw)
        case["prof"], case["prof_data"] = prof_svc.face_of_data(data), data
        return case

    seen = ""
    kind = next((k for k in MEDIA_KINDS if getattr(message, k, None)), None)
    if kind:
        try:
            seen = await media.extract(bot, message, _MEDIA)
        except Exception:
            logger.warning("сборщик: медиа не распозналось", exc_info=True)
    case["text"] = " ".join(" ".join(filter(None, [raw, seen])).split())
    case["msg"] = {"text": raw, "media": kind, "media_text": seen,
                   "links": _links(message, seen), "buttons": _buttons(message),
                   "forward": _forward_kind(message)}
    case["prof"], case["prof_data"], case["asked"] = await _face_of(message)
    return case


def _remember(msg_id: int, case: dict) -> None:
    if len(_pending) > PENDING_MAX:
        _pending.clear()          # разметку бросили на полпути, не жалко
    _pending[msg_id] = case


def _kb(case: dict):
    """Кнопки разметки. Обе части есть — четыре варианта, одна — два."""
    b = InlineKeyboardBuilder()
    if len(case["text"]) >= 10 and len(case["prof"]) >= 10:
        b.row(_btn("⛔ Всё спам", "s:all"), _btn("💬 Спам только текст", "s:msg"))
        b.row(_btn("🪪 Спам только профиль", "s:prof"), _btn("🕊 Всё норма", "s:ok"))
    else:
        b.row(_btn("⛔ Спам", "s:all"), _btn("🕊 Норма", "s:ok"))
    b.row(_btn("⏭ Пропустить", "s:skip"))
    return b.as_markup()


@router.message(F.document.file_name.func(
    lambda n: bool(n) and n.lower().endswith(".jsonl")))
async def load_jsonl(message: Message, bot: Bot) -> None:
    """Загрузить выгрузку обратно: тот же формат, что отдаёт /экспорт."""
    doc = message.document
    if (doc.file_size or 0) > IMPORT_MAX_BYTES:
        await message.reply("Файл больше 20 МБ — Telegram не даст боту его скачать.")
        return
    buf = await bot.download(doc)
    added = skipped = bad = 0
    for line in buf.read().decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            bad += 1
            continue
        parts = sum(1 for k in ("message", "profile")
                    if (rec.get(k) or {}).get("label") in ("spam", "ok"))
        got = await load_case(rec, message.from_user.id)
        added += got
        skipped += parts - got
    await db.seed_commit()
    if added:
        from ..services import nn
        nn.invalidate()
    await db.add_event(None, "nn", f"сборщик: загружено {added} by {message.from_user.id}")
    await message.reply(f"Загружено: <b>{added}</b>. Уже было или без метки: {skipped}."
                        + (f" Нечитаемых строк: {bad}." if bad else ""))


async def load_case(rec: dict, by: int | None) -> int:
    """Один случай выгрузки -> записи в копилке. Сколько записей добавилось.

    Строку для модели собираем заново из полей, если они есть: тем же видом,
    что и проверка в чате. У старых записей полей нет — берём готовую строку.
    """
    from ..services import profile as prof_svc
    ids = []
    msg, prof = rec.get("message") or {}, rec.get("profile") or {}
    if msg.get("label") in ("spam", "ok"):
        text = " ".join(filter(None, [msg.get("text"), msg.get("media_text")]))
        data = {k: v for k, v in msg.items() if k != "label"}
        ids.append(await db.seed_add(text, msg["label"], "msg", data=data,
                                     labeled_by=rec.get("by") or by))
    if prof.get("label") in ("spam", "ok"):
        data = {k: v for k, v in prof.items() if k not in ("label", "text")}
        text = prof_svc.face_of_data(data) if data.get("name") or data.get("bio") \
            else prof.get("text", "")
        ids.append(await db.seed_add(text, prof["label"], "prof",
                                     user_id=rec.get("user_id"), data=data or None,
                                     labeled_by=rec.get("by") or by))
    await db.sample_link(ids)
    return sum(1 for i in ids if i)


@router.message()
async def collect(message: Message, bot: Bot) -> None:
    """Разобрать присланное и спросить, спам это или норма."""
    case = await _case(message, bot)
    text, prof = case["text"], case["prof"]

    if len(text) < 10 and len(prof) < 10:
        hint = ("\nФорма профиля — /профиль." if _mode == "prof" else "")
        await message.reply(
            "Тут нечего запоминать: ни текста, ни описания профиля.\n"
            "Для набора нужен текст длиннее десяти символов." + hint)
        return

    uid, who = case["uid"], case["who"]
    lines = ["<b>Разобрал:</b>", ""]
    if who:
        lines.append(f"👤 Автор: {utils.esc(who)}"
                     + (f" (<code>{uid}</code>)" if uid else " · профиль скрыт"))
    if text:
        lines.append(f"💬 {utils.esc(utils.chunk(text, 400))}")
    if case["msg"].get("media_text"):
        lines.append("<i>Текст с картинки или голосового — вместе с подписью.</i>")
    if case["msg"].get("links"):
        lines.append(f"🔗 Ссылок: {len(case['msg']['links'])}")
    if prof:
        lines.append(f"📝 Профиль: {utils.esc(utils.chunk(prof, 300))}")
    if case["seen"]:
        lines.append(f"<i>Поля: {utils.esc(', '.join(case['seen']))}</i>")
    if not text and not case["seen"]:
        lines.append("<i>Текста сообщения нет — запомню только профиль.</i>")
    if not prof and who and not uid:
        lines.append("<i>Автор скрыт: одно имя без профиля не записываю.</i>")
    if not case["asked"] and uid:
        lines.append("<i>Профиль спросить не вышло — запомню имя и ник.</i>")
    lines += ["", "Что это?"]

    sent = await message.reply("\n".join(lines), reply_markup=_kb(case))
    _remember(sent.message_id, case)


def _btn(text: str, data: str):
    from aiogram.types import InlineKeyboardButton
    return InlineKeyboardButton(text=text, callback_data=data)


# кнопка -> (метка сообщения, метка профиля); None — эту часть не пишем
MARKS = {"all": ("spam", "spam"), "msg": ("spam", None),
         "prof": ("ok", "spam"), "ok": ("ok", "ok")}
MARK_TEXT = {"all": "⛔ <b>Всё спам</b>", "msg": "💬 <b>Спам только текст</b>",
             "prof": "🪪 <b>Спам только профиль</b>", "ok": "🕊 <b>Всё норма</b>"}


@router.callback_query(F.data.startswith("s:"))
async def mark(cb: CallbackQuery) -> None:
    what = cb.data.split(":")[1]
    case = _pending.pop(cb.message.message_id, None)
    if case is None:
        await cb.answer("Это сообщение я уже забыл — пришлите заново.",
                        show_alert=True)
        return
    if what == "skip" or what not in MARKS:
        await cb.message.edit_text(cb.message.html_text + "\n\n⏭ <b>Пропущено</b>")
        await cb.answer()
        return

    msg_label, prof_label = MARKS[what]
    by = cb.from_user.id
    # Текст и профиль кладём в разные списки: сообщение сравнивается с
    # сообщениями, профиль с профилями. Свалить их в одну кучу — значит
    # сравнивать строку из имени и био с обычной репликой в чате.
    ids = []
    if msg_label and case["text"]:
        ids.append(await db.seed_add(case["text"], msg_label, "msg",
                                     user_id=case["uid"], data=case["msg"], labeled_by=by))
    if prof_label and case["prof"]:
        ids.append(await db.seed_add(case["prof"], prof_label, "prof",
                                     user_id=case["uid"], data=case["prof_data"],
                                     labeled_by=by))
    await db.sample_link(ids)
    await db.seed_commit()
    added = sum(1 for i in ids if i)
    if added:
        from ..services import nn
        nn.invalidate()           # копилка изменилась — модель устарела

    note = MARK_TEXT[what] if added else "🔁 <b>Уже было в копилке</b>"
    if added and not (case["text"] and case["prof"]):
        note = "⛔ <b>Спам</b>" if what == "all" else "🕊 <b>Норма</b>"
    await cb.message.edit_text(cb.message.html_text + "\n\n" + note)
    await db.add_event(None, "nn", f"сборщик: {what} +{added} by {by}")
    await cb.answer("Записал" if added else "Уже было")
