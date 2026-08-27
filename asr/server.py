"""Служба расшифровки голосовых: HTTP-ручка поверх Vosk.

Бот сам речь не распознаёт и не должен: модель ест процессор секундами,
а пока она считает, чат не модерируется. Поэтому распознавалка живёт
отдельным процессом (обычно — отдельным контейнером), а бот ходит к ней
по HTTP и переживает её падение без последствий.

Контракт нарочно примитивный, чтобы Vosk можно было заменить на что угодно:

    POST /transcribe   multipart/form-data, поле file (ogg/opus как есть)
    200 {"text": "расшифровка", "duration": 12.3}

    GET  /health       200 {"ok": true, "model": "..."}

Vosk выбран для начала как самый неприхотливый: маленькая русская модель
весит 88 МБ и работает на обычном процессоре. Захотите точнее (GigaAM,
whisper) — меняется только этот файл, бот не заметит.
"""
import asyncio
import json
import logging
import os
import sys

from aiohttp import web

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("asr")

MODEL_PATH = os.getenv("VOSK_MODEL", "/models/vosk-model-small-ru-0.22")
HOST = os.getenv("ASR_HOST", "0.0.0.0")
PORT = int(os.getenv("ASR_PORT", "8080"))
FFMPEG = os.getenv("FFMPEG_BIN", "ffmpeg")
RATE = 16000                      # Vosk ждёт 16 кГц моно
MAX_BYTES = 20 * 1024 * 1024
# распознаём по одной записи за раз: модель и так забирает ядро целиком,
# а очередь из десяти голосовых не должна превращаться в десять процессов
_lock = asyncio.Lock()
_model = None


def load_model():
    """Модель поднимается один раз при старте: загрузка занимает секунды."""
    global _model
    from vosk import Model, SetLogLevel
    SetLogLevel(-1)               # Kaldi иначе сыпет в stderr сотни строк
    if not os.path.isdir(MODEL_PATH):
        sys.exit(f"нет модели: {MODEL_PATH}")
    logger.info("гружу модель %s", MODEL_PATH)
    _model = Model(MODEL_PATH)
    logger.info("модель готова")


async def to_pcm(raw: bytes) -> bytes:
    """Перегнать что прислали в сырой PCM 16 кГц моно.

    Telegram отдаёт голосовые в ogg/opus, кружки — в mp4, и напрямую Vosk
    ни то, ни другое не читает. ffmpeg работает через каналы, файлы на диск
    не пишутся вовсе.
    """
    proc = await asyncio.create_subprocess_exec(
        FFMPEG, "-hide_banner", "-loglevel", "error",
        "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", str(RATE), "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(raw)
    if proc.returncode != 0:
        raise web.HTTPBadRequest(
            text=f"не разобрать запись: {err.decode(errors='replace')[:200]}")
    return out


def recognize(pcm: bytes) -> str:
    """Синхронное распознавание — вызывается в отдельном потоке."""
    from vosk import KaldiRecognizer
    rec = KaldiRecognizer(_model, RATE)
    rec.SetWords(False)
    step = RATE * 2 * 10          # по десять секунд звука за раз
    for i in range(0, len(pcm), step):
        rec.AcceptWaveform(pcm[i:i + step])
    return (json.loads(rec.FinalResult()).get("text") or "").strip()


async def handle_transcribe(request: web.Request) -> web.Response:
    reader = await request.multipart()
    raw = b""
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name != "file":
            continue
        while True:
            chunk = await part.read_chunk()
            if not chunk:
                break
            raw += chunk
            if len(raw) > MAX_BYTES:
                raise web.HTTPRequestEntityTooLarge(
                    max_size=MAX_BYTES, actual_size=len(raw))
    if not raw:
        raise web.HTTPBadRequest(text="файл не пришёл")

    pcm = await to_pcm(raw)
    duration = round(len(pcm) / 2 / RATE, 1)
    async with _lock:
        text = await asyncio.to_thread(recognize, pcm)
    logger.info("расшифровано %.1f сек -> %d знаков", duration, len(text))
    return web.json_response({"text": text, "duration": duration})


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": _model is not None,
                              "model": os.path.basename(MODEL_PATH)})


def main() -> None:
    load_model()
    app = web.Application(client_max_size=MAX_BYTES + 1024)
    app.router.add_post("/transcribe", handle_transcribe)
    app.router.add_get("/health", handle_health)
    logger.info("слушаю %s:%s", HOST, PORT)
    web.run_app(app, host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()
