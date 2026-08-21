"""Медиа-ответы триггеров: скачивание файла на диск и отправка его в чат.

Файл скачивается один раз при создании триггера и лежит в config.TRIG_DIR —
триггер не зависит от сохранности переписки, из которой его прислали.
"""
import html
import logging
import os
import re
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


async def save_media(bot: Bot, file_id: str, chat_id: int, kind: str) -> str:
    """Скачать файл в TRIG_DIR, вернуть путь."""
    os.makedirs(config.TRIG_DIR, exist_ok=True)
    ext = MEDIA_KINDS[kind][0]
    path = os.path.join(config.TRIG_DIR, f"{chat_id}_{uuid.uuid4().hex[:12]}{ext}")
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


async def send_answer(message: Message, ans) -> None:
    """Отправить один вариант ответа: текст или медиа с диска.

    Текст лежит с разметкой (жирный, курсив, ссылки — как их набрали в Telegram),
    поэтому уходит как HTML. Битая разметка -> повтор обычным текстом.
    """
    if not ans["file_path"]:
        # превью ссылок гасим: ответ триггера должен выглядеть так, как его
        # написали, а не тащить за собой картинку с чужого сайта
        no_preview = LinkPreviewOptions(is_disabled=True)
        try:
            await message.reply(ans["text"], link_preview_options=no_preview)
        except Exception as e:
            if utils.msg_gone(e):
                return                        # исходное сообщение удалили
            await message.reply(html.escape(ans["text"] or ""), parse_mode=None,
                                link_preview_options=no_preview)
        return
    kind = ans["media_type"]
    if kind not in MEDIA_KINDS or not os.path.exists(ans["file_path"]):
        logger.warning("trigger media missing: %s", ans["file_path"])
        return
    _ext, method, has_caption = MEDIA_KINDS[kind]
    kwargs = {"reply_to_message_id": message.message_id}
    if has_caption and ans["text"]:
        kwargs["caption"] = ans["text"]
    try:
        await getattr(message, method)(FSInputFile(ans["file_path"]), **kwargs)
    except Exception as e:
        if utils.msg_gone(e):
            return
        if "caption" in kwargs:               # подпись с битой разметкой
            kwargs["caption"] = html.escape(ans["text"])
            kwargs["parse_mode"] = None
        await getattr(message, method)(FSInputFile(ans["file_path"]), **kwargs)
