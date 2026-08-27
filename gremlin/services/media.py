"""Текст, спрятанный в медиа: картинки читаем tesseract'ом, голосовые — сторонней службой.

Половина рекламы приходит скриншотом или голосовым, и для правил такое
сообщение выглядит пустым: «фото без текста». Здесь оно превращается
в обычный текст, который дальше проходит тот же путь, что и набранный руками —
стоп-слова, ссылки, нейрофильтр, улика в копилку, цитата в карточке.

Две принципиальные вещи:

  * Распознавание ничего не решает. Оно только достаёт текст, наказывают
    прежние правила. Поэтому включение OCR не меняет поведение чата — меняет
    лишь то, сколько бот видит.
  * Расшифровка голосовых вынесена наружу. Модель ASR весит гигабайты и ест
    процессор секундами, держать её в одном процессе с ботом нельзя: пока она
    считает, чат не модерируется. Бот ходит к ней по HTTP, и какая там модель
    стоит — его не касается. Контракт нарочно примитивный:

        POST <ASR_URL>   multipart/form-data, поле file (ogg/opus как есть)
        200 {"text": "расшифровка", "duration": 12.3}

    Этому соответствует что угодно: GigaAM, whisper.cpp, faster-whisper.
    Служба не отвечает — бот работает как раньше, просто без расшифровок.
"""
import asyncio
import logging
from collections import OrderedDict

from .. import config

logger = logging.getLogger("gremlin.media")

# Разбор идёт по одному за раз: и tesseract, и распознавалка речи упираются
# в процессор, а параллельная пачка голосовых способна занять всё ядро и
# затормозить модерацию текста, ради которой бот и живёт.
_ocr_lock = asyncio.Lock()
_asr_lock = asyncio.Lock()

# file_unique_id -> распознанный текст. Одно и то же медиа приходит не раз:
# альбом, пересылка, редактирование, повторная проверка при перезапуске.
_cache: OrderedDict[str, str] = OrderedDict()

_MISSING = object()


def _short(text: str, limit: int = 200) -> str:
    """Кусок распознанного для лога: по нему видно, что именно прочитал бот."""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _remember(key: str, text: str) -> str:
    _cache[key] = text
    _cache.move_to_end(key)
    while len(_cache) > config.MEDIA_CACHE:
        _cache.popitem(last=False)
    return text


def cached(message) -> str:
    """Уже распознанное для этого сообщения. Пусто, если не распознавали.

    Нужна там, где текст хочется показать (карточка) или сохранить (улика),
    но запускать ради этого распознавание заново не хочется.
    """
    key = _key(message)
    return _cache.get(key, "") if key else ""


def _key(message) -> str | None:
    """file_unique_id первого подходящего вложения."""
    for attr in ("photo", "voice", "video_note", "audio", "document", "sticker"):
        obj = getattr(message, attr, None)
        if not obj:
            continue
        if attr == "photo":
            obj = obj[-1]                 # самый крупный вариант
        return getattr(obj, "file_unique_id", None)
    return None


# ---------- картинки ----------

def ocr_ready() -> bool:
    return _ocr_state() == "ok"


_ocr_checked = _MISSING


def _ocr_state() -> str:
    """'ok' или причина, по которой картинки не читаются."""
    global _ocr_checked
    if _ocr_checked is _MISSING:
        import shutil
        found = shutil.which(config.TESSERACT_BIN)
        _ocr_checked = "ok" if found else f"не найден {config.TESSERACT_BIN}"
        if found:
            logger.info("OCR: %s", found)
        else:
            logger.warning("OCR недоступен: %s", _ocr_checked)
    return _ocr_checked


def status() -> str:
    """Строки состояния для меню и панели."""
    return _ocr_state()


def asr_status() -> str:
    return "ok" if config.ASR_URL else "не задан ASR_URL"


async def ocr_bytes(raw: bytes, langs: str) -> str:
    """Прогнать картинку через tesseract. Пусто — не распозналось."""
    if _ocr_state() != "ok":
        return ""
    async with _ocr_lock:
        proc = await asyncio.create_subprocess_exec(
            config.TESSERACT_BIN, "stdin", "stdout", "-l", langs,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(raw), config.OCR_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning("tesseract не уложился в %s сек", config.OCR_TIMEOUT)
            return ""
    if proc.returncode != 0:
        logger.warning("tesseract вернул %s: %s", proc.returncode,
                       err.decode(errors="replace").strip()[:200])
        return ""
    text = " ".join(out.decode("utf-8", errors="replace").split())
    return text if len(text) >= config.OCR_MIN_CHARS else ""


# ---------- голосовые ----------

async def transcribe_bytes(raw: bytes, filename: str) -> str:
    """Отправить запись в стороннюю службу и забрать расшифровку."""
    if not config.ASR_URL:
        return ""
    import aiohttp
    async with _asr_lock:
        try:
            timeout = aiohttp.ClientTimeout(total=config.ASR_TIMEOUT)
            form = aiohttp.FormData()
            form.add_field("file", raw, filename=filename,
                           content_type="application/octet-stream")
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(config.ASR_URL, data=form) as resp:
                    if resp.status != 200:
                        logger.warning("расшифровка: служба ответила %s", resp.status)
                        return ""
                    data = await resp.json(content_type=None)
        except Exception:
            logger.warning("расшифровка недоступна (%s)", config.ASR_URL, exc_info=True)
            return ""
    text = " ".join(str(data.get("text") or "").split())
    return text


# ---------- разбор сообщения ----------

def _voice_of(message):
    """Запись голосом и её длительность, если она в сообщении есть."""
    for attr, name in (("voice", "voice.ogg"), ("video_note", "note.mp4"),
                       ("audio", "audio.mp3")):
        obj = getattr(message, attr, None)
        if obj is not None:
            return obj, name
    return None, None


async def _download(bot, file_id: str, limit: int) -> bytes | None:
    """Скачать вложение в память. Слишком большое не тянем вовсе."""
    try:
        info = await bot.get_file(file_id)
        if (info.file_size or 0) > limit:
            logger.debug("вложение %s байт — больше потолка", info.file_size)
            return None
        buf = await bot.download_file(info.file_path)
        return buf.read()
    except Exception:
        logger.warning("не скачать вложение", exc_info=True)
        return None


async def extract(bot, message, s) -> str:
    """Текст из медиа этого сообщения — распознанный или из кэша.

    Вызывается до правил: то, что здесь вернулось, участвует в проверках
    наравне с подписью. Ничего не распозналось — пустая строка, и всё
    работает как до включения.
    """
    key = _key(message)
    if key and key in _cache:
        _cache.move_to_end(key)
        return _cache[key]

    text = ""
    photo = getattr(message, "photo", None)
    voice, filename = _voice_of(message)

    if photo and s.ocr_on and _ocr_state() == "ok":
        raw = await _download(bot, photo[-1].file_id, config.OCR_MAX_BYTES)
        if raw:
            text = await ocr_bytes(raw, s.ocr_langs)
            if text:
                # текст в логе, а не только его длина: иначе непонятно, почему
                # правила промолчали — не распозналось или распозналось не то
                logger.info("распознана картинка в %s (%d знаков): %s",
                            message.chat.id, len(text), _short(text))

    elif voice is not None and s.asr_on and config.ASR_URL:
        seconds = getattr(voice, "duration", 0) or 0
        if seconds > s.asr_max_sec:
            logger.debug("запись %s сек длиннее потолка %s", seconds, s.asr_max_sec)
        else:
            raw = await _download(bot, voice.file_id, config.ASR_MAX_BYTES)
            if raw:
                text = await transcribe_bytes(raw, filename)
                if text:
                    logger.info("расшифрована запись в %s (%d знаков): %s",
                                message.chat.id, len(text), _short(text))

    return _remember(key, text) if key else text
