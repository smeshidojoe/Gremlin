"""Классификатор откровенных картинок — для аватарок.

Половина рекламных аккаунтов не пишет ничего запрещённого: у них обычное имя,
обычная реплика по теме, а вся суть на аватарке. Текстом такое не поймать.

Модель — ViT на 384×384, два ответа: обычная картинка или откровенная. Лежит
рядом с rubert и грузится так же: onnxruntime, восьмибитная версия на 84 МБ.
Скачивается отдельно (tools/download_nsfw.py); файлов нет — проверка молча
выключена, всё остальное работает как обычно.

Замер на живых аватарках: у рекламного аккаунта 100%, у трёх обычных людей
1, 1 и 3%. Считает около секунды на картинку в один поток, поэтому зовём её
последней — только когда по тексту профиля зацепиться не вышло.
"""
import asyncio
import io
import json
import logging
import os

from .. import config

logger = logging.getLogger("gremlin.nsfw")

_sess = None
_np = None
_Image = None
_size = 384
_mean = None
_std = None
_input = "pixel_values"
_state: str | None = None      # None — не пробовали, "ok" | причина отказа
_load_lock = asyncio.Lock()


def status() -> str:
    return _state or "не загружался"


def _load_sync() -> str:
    global _sess, _np, _Image, _size, _mean, _std, _input
    onnx = os.path.join(config.NSFW_MODEL_DIR, "model.onnx")
    pre = os.path.join(config.NSFW_MODEL_DIR, "preprocessor_config.json")
    if not os.path.exists(onnx):
        return "нет модели (скачайте: python tools/download_nsfw.py)"
    try:
        import numpy
        import onnxruntime
        from PIL import Image
    except ImportError as e:
        return f"нет библиотеки: {e.name}"

    size, mean, std = 384, [0.5, 0.5, 0.5], [0.5, 0.5, 0.5]
    if os.path.exists(pre):
        try:
            with open(pre, encoding="utf-8") as f:
                cfg = json.load(f)
            size = int(cfg.get("size", {}).get("height", size))
            mean = cfg.get("image_mean", mean)
            std = cfg.get("image_std", std)
        except Exception:
            logger.warning("не разобрал preprocessor_config.json", exc_info=True)

    opts = onnxruntime.SessionOptions()
    # как и у нейрофильтра: бот живёт в одном процессе с моделью, все ядра ей
    # отдавать нельзя — иначе на большой картинке чат подвиснет
    opts.intra_op_num_threads = 1
    opts.inter_op_num_threads = 1
    opts.log_severity_level = 3
    sess = onnxruntime.InferenceSession(onnx, opts, providers=["CPUExecutionProvider"])
    _sess, _np, _Image = sess, numpy, Image
    _size = size
    _mean = numpy.array(mean, dtype=numpy.float32).reshape(3, 1, 1)
    _std = numpy.array(std, dtype=numpy.float32).reshape(3, 1, 1)
    _input = sess.get_inputs()[0].name
    return "ok"


async def ensure() -> bool:
    global _state
    if _state is not None:
        return _state == "ok"
    async with _load_lock:
        if _state is None:
            _state = await asyncio.to_thread(_load_sync)
            if _state == "ok":
                logger.info("классификатор картинок загружен из %s",
                            config.NSFW_MODEL_DIR)
            else:
                logger.warning("классификатор картинок недоступен: %s", _state)
    return _state == "ok"


def _score_sync(raw: bytes) -> int:
    im = _Image.open(io.BytesIO(raw)).convert("RGB")
    im = im.resize((_size, _size), _Image.BILINEAR)
    a = _np.asarray(im, dtype=_np.float32).transpose(2, 0, 1) / 255.0
    x = ((a - _mean) / _std)[None]
    logits = _sess.run(None, {_input: x})[0][0]
    e = _np.exp(logits - logits.max())
    p = e / e.sum()
    # метка 1 — откровенная картинка (см. config.json модели)
    return int(round(100 * float(p[1])))


async def score(raw: bytes) -> int | None:
    """Насколько картинка откровенная, в процентах. None — сравнить нечем."""
    if not raw or not await ensure():
        return None
    if len(raw) > config.NSFW_MAX_BYTES:
        logger.debug("картинка на %d байт — не смотрим", len(raw))
        return None
    try:
        return await asyncio.to_thread(_score_sync, raw)
    except Exception:
        logger.warning("классификатор не справился с картинкой", exc_info=True)
        return None
