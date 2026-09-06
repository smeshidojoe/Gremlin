"""Медиа-ответы триггеров: скачивание файла на диск и отправка его в чат.

Файл скачивается один раз при создании триггера и лежит в config.TRIG_DIR —
триггер не зависит от сохранности переписки, из которой его прислали.
"""
import html
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass

from aiogram import Bot
from aiogram.types import FSInputFile, LinkPreviewOptions, Message

from .. import config, utils

logger = logging.getLogger("gremlin.triggers")

# тип медиа -> (расширение файла, метод отправки, поддерживает ли подпись)
MEDIA_KINDS = {
    "photo": (".jpg", "answer_photo", True),
    "video": (".mp4", "answer_video", True),
    "animation": (".mp4", "answer_animation", True),
    "sticker": (".webp", "answer_sticker", False),
    "voice": (".ogg", "answer_voice", True),
    "video_note": (".mp4", "answer_video_note", False),
    "audio": (".mp3", "answer_audio", True),
    "document": ("", "answer_document", True),
}


# фраза со звёздочкой ловит любые окончания: «пив*» -> пиво, пива, пивом
_STEM_CACHE: dict[str, re.Pattern] = {}


def phrase_matches(phrase: str, text_low: str) -> bool:
    """Совпала ли фраза триггера с текстом.

    Без звёздочки — слово целиком: «донат» не должен срабатывать на «донатный»
    и «задонатил». Со звёздочкой на конце — основа плюс любые буквы: «донат*»
    ловит все окончания, но по-прежнему не кусок середины другого слова.
    """
    stem = phrase.endswith("*")
    body = phrase[:-1] if stem else phrase
    if not body:
        return False
    rx = _STEM_CACHE.get(phrase)
    if rx is None:
        tail = r"\w*" if stem else r"(?!\w)"
        rx = re.compile(rf"(?<!\w){re.escape(body)}{tail}", re.IGNORECASE | re.UNICODE)
        if len(_STEM_CACHE) > 2000:
            _STEM_CACHE.clear()
        _STEM_CACHE[phrase] = rx
    return rx.search(text_low) is not None


@dataclass
class Media:
    kind: str
    file_id: str


def extract_media(message: Message) -> Media | None:
    """Достать первое поддерживаемое медиа из сообщения."""
    for kind in MEDIA_KINDS:
        obj = getattr(message, kind, None)
        if not obj:
            continue
        # photo — список размеров, берём самый большой
        file_id = obj[-1].file_id if kind == "photo" else obj.file_id
        return Media(kind=kind, file_id=file_id)
    return None


def media_dir(chat_id: int, purpose: str = "trig") -> str:
    """Папка под файлы одного чата и одного назначения.

    Раскладка data/media/<чат>/<назначение> нужна не для красоты: в общей куче
    на тысяче файлов глазами уже ничего не найдёшь, а убрать за ушедшим чатом
    можно только перебором имён.
    """
    if purpose not in config.MEDIA_PURPOSES:
        purpose = "trig"
    path = os.path.join(config.MEDIA_DIR, str(chat_id), purpose)
    os.makedirs(path, exist_ok=True)
    return path


def media_path(chat_id: int, kind: str, purpose: str = "trig") -> str:
    """Свободное имя под новый файл."""
    ext = MEDIA_KINDS.get(kind, ("",))[0]
    return os.path.join(media_dir(chat_id, purpose),
                        f"{uuid.uuid4().hex[:12]}{ext}")


async def save_media(bot: Bot, file_id: str, chat_id: int, kind: str,
                     purpose: str = "trig") -> str:
    """Скачать файл в папку своего чата и назначения, вернуть путь."""
    path = media_path(chat_id, kind, purpose)
    await bot.download(file_id, destination=path)
    return path


async def send(message: Message, row) -> None:
    """Ответить триггером. Вариантов может быть несколько — берём случайный."""
    from .. import db
    ans = await db.ans_pick("trig", row["id"])
    if ans is None:
        logger.warning("trigger %s has no answers", row["id"])
        return
    await send_answer(message, ans)


async def send_answer(message: Message, ans, reply: bool = True,
                      subs: dict | None = None):
    """Отправить один вариант ответа: текст или медиа с диска.

    Текст лежит с разметкой (жирный, курсив, ссылки — как их набрали в Telegram),
    поэтому уходит как HTML. Битая разметка -> повтор обычным текстом.

    reply=False — обычным сообщением, а не ответом: приветствию новичка
    отвечать не на что. subs — подстановки вроде {name}.
    """
    text = ans["text"] or ""
    for key, val in (subs or {}).items():
        text = text.replace(key, val)
    no_preview = LinkPreviewOptions(is_disabled=True)
    send_text = message.reply if reply else message.answer

    if not ans["file_path"]:
        # превью ссылок гасим: ответ должен выглядеть так, как его написали,
        # а не тащить за собой картинку с чужого сайта
        try:
            return await send_text(text, link_preview_options=no_preview)
        except Exception as e:
            if utils.msg_gone(e):
                return None                   # исходное сообщение удалили
            return await send_text(html.escape(text), parse_mode=None,
                                   link_preview_options=no_preview)

    kind = ans["media_type"]
    if kind not in MEDIA_KINDS or not os.path.exists(ans["file_path"]):
        logger.warning("trigger media missing: %s", ans["file_path"])
        return None
    _ext, method, has_caption = MEDIA_KINDS[kind]
    kwargs = {}
    if reply:
        kwargs["reply_to_message_id"] = message.message_id
    if has_caption and text:
        kwargs["caption"] = text
    try:
        return await getattr(message, method)(FSInputFile(ans["file_path"]), **kwargs)
    except Exception as e:
        if utils.msg_gone(e):
            return None
        if "caption" in kwargs:               # подпись с битой разметкой
            kwargs["caption"] = html.escape(text)
            kwargs["parse_mode"] = None
        return await getattr(message, method)(FSInputFile(ans["file_path"]), **kwargs)


async def send_answer_to(bot, chat_id: int, ans, subs: dict | None = None,
                         markup=None):
    """Та же заготовка, но в произвольный чат — например человеку в личку.

    send_answer работает от объекта сообщения (reply/answer), а тут отвечать
    не на что: заявку на вступление подали, сообщения нет. Метод бота
    называется send_photo там, где у сообщения answer_photo, — только этим
    и отличается.
    """
    text = ans["text"] or ""
    for key, val in (subs or {}).items():
        text = text.replace(key, val)
    no_preview = LinkPreviewOptions(is_disabled=True)

    if not ans["file_path"]:
        try:
            return await bot.send_message(chat_id, text, reply_markup=markup,
                                          link_preview_options=no_preview)
        except Exception as e:
            if utils.msg_gone(e):
                return None
            return await bot.send_message(chat_id, html.escape(text), parse_mode=None,
                                          reply_markup=markup,
                                          link_preview_options=no_preview)

    kind = ans["media_type"]
    if kind not in MEDIA_KINDS or not os.path.exists(ans["file_path"]):
        logger.warning("медиа заготовки пропало: %s", ans["file_path"])
        # текст всё равно нужен: без него человек не поймёт, чего от него хотят
        if text:
            return await bot.send_message(chat_id, text, reply_markup=markup,
                                          link_preview_options=no_preview)
        return None
    _ext, method, has_caption = MEDIA_KINDS[kind]
    send = getattr(bot, "send_" + method.removeprefix("answer_"))
    kwargs = {"reply_markup": markup}
    if has_caption and text:
        kwargs["caption"] = text
    try:
        return await send(chat_id, FSInputFile(ans["file_path"]), **kwargs)
    except Exception as e:
        if utils.msg_gone(e):
            return None
        if "caption" in kwargs:
            kwargs["caption"] = html.escape(text)
            kwargs["parse_mode"] = None
        return await send(chat_id, FSInputFile(ans["file_path"]), **kwargs)


async def migrate_layout() -> int:
    """Разложить старые файлы по data/media/<чат>/<назначение>. Разово.

    Раньше всё лежало одной кучей в data/triggers. Путь в базе абсолютный и
    снят на другой машине (в контейнере он свой), поэтому ищем файл по имени:
    сперва там, где записано, потом в старой общей папке.

    Пропавший файл — не беда: путь в базе оставляем как был, дальше сработает
    обычная защита «медиа заготовки пропало».
    """
    from .. import db
    if await db.kv_get("mig_media_dirs"):
        return 0
    moved = 0
    for table, chat_id, row_id, old in await db.media_rows():
        purpose = await db.media_purpose(row_id) if table == "answers" else "trig"
        name = old.replace("\\", "/").rsplit("/", 1)[-1]
        src = old if os.path.exists(old) else os.path.join(config.TRIG_DIR, name)
        if not os.path.exists(src):
            logger.info("медиа не нашлось при переезде: %s", old)
            continue
        dst = os.path.join(media_dir(chat_id, purpose), name)
        if os.path.abspath(src) == os.path.abspath(dst):
            continue
        try:
            shutil.move(src, dst)
        except Exception:
            logger.warning("не переехал файл %s", src, exc_info=True)
            continue
        await db.media_set_path(table, row_id, dst)
        moved += 1
    await db.kv_set("mig_media_dirs", "1")
    if moved:
        logger.info("медиа разложено по папкам чатов: %d файлов", moved)
    return moved
