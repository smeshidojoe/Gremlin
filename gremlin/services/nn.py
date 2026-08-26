"""Нейрофильтр: похоже ли сообщение на то, за что уже наказывали.

Модель rubert-tiny2 превращает текст в вектор из 312 чисел. Тексты близкие
по смыслу дают близкие векторы — даже если слова разные («казино» / «кaзuно» /
«заработок на ставках»). Дальше просто косинусная близость к уликам из базы:
ближайшие соседи голосуют, спам это или норма.

Никакой генерации здесь нет, поэтому:
  * быстро — 3 слоя, десятки миллисекунд на CPU;
  * предсказуемо — «игнорируй все инструкции» внутри сообщения ничего не значит,
    текст для модели просто набор токенов.

Пока фильтр работает в теневом режиме: вердикт пишется в журнал, на реальную
модерацию не влияет (см. nn_mode в настройках чата).
"""
import asyncio
import logging
import os
import time

from .. import config, db

logger = logging.getLogger("gremlin.nn")

MAX_TOKENS = 256          # длиннее не нужно: спам весь в первых строках
PROFILE_TTL = 300         # как часто перечитываем улики из базы
TOP_K = 5                 # сколько соседей голосует
VEC_BATCH = 64            # столько текстов считаем за один прогон модели

_sess = None              # onnxruntime.InferenceSession
_tok = None               # tokenizers.Tokenizer
_np = None                # numpy, грузится вместе с onnxruntime
_state: str | None = None  # None — ещё не пробовали, "ok" | причина отказа
_load_lock = asyncio.Lock()
# профиль чата: chat_id -> (когда собран, матрица векторов, метки, id улик)
_profile: dict[int, tuple[float, object, list[str], list[int]]] = {}


def status() -> str:
    """Строка для меню: готов фильтр или чего не хватает."""
    return _state or "не загружался"


def _load_sync() -> str:
    """Поднять модель. Возвращает 'ok' или причину, почему не вышло."""
    global _sess, _tok, _np
    onnx = os.path.join(config.NN_MODEL_DIR, "model.onnx")
    tokenizer = os.path.join(config.NN_MODEL_DIR, "tokenizer.json")
    if not os.path.exists(onnx):
        return "нет model.onnx (соберите: python tools/export_onnx.py)"
    if not os.path.exists(tokenizer):
        return "нет tokenizer.json (скачайте: python tools/download_model.py)"
    try:
        import numpy
        import onnxruntime
        from tokenizers import Tokenizer
    except ImportError as e:
        return f"нет библиотеки: {e.name}"

    opts = onnxruntime.SessionOptions()
    # бот и так живёт в одном процессе с сеткой — не даём ей забрать все ядра
    opts.intra_op_num_threads = 1
    opts.inter_op_num_threads = 1
    opts.log_severity_level = 3
    sess = onnxruntime.InferenceSession(onnx, opts, providers=["CPUExecutionProvider"])
    tok = Tokenizer.from_file(tokenizer)
    tok.enable_truncation(max_length=MAX_TOKENS)
    tok.enable_padding()
    _sess, _tok, _np = sess, tok, numpy
    return "ok"


async def ensure() -> bool:
    """Загрузить модель один раз. Повторных попыток не делаем: если файлов
    нет, они не появятся сами, а сообщать об этом каждое сообщение незачем."""
    global _state
    if _state is not None:
        return _state == "ok"
    async with _load_lock:
        if _state is None:
            _state = await asyncio.to_thread(_load_sync)
            if _state == "ok":
                logger.info("нейрофильтр загружен из %s", config.NN_MODEL_DIR)
            else:
                logger.warning("нейрофильтр недоступен: %s", _state)
    return _state == "ok"


def _embed_sync(texts: list[str]):
    """Векторы для списка текстов: усреднение по токенам + нормировка."""
    enc = _tok.encode_batch(texts)
    ids = _np.array([e.ids for e in enc], dtype=_np.int64)
    mask = _np.array([e.attention_mask for e in enc], dtype=_np.int64)
    out = _sess.run(["last_hidden_state"], {
        "input_ids": ids,
        "attention_mask": mask,
        "token_type_ids": _np.zeros_like(ids),
    })[0]
    m = mask[:, :, None].astype(_np.float32)
    # паддинг в среднее не берём, иначе короткие тексты «размываются» нулями
    vec = (out * m).sum(axis=1) / _np.maximum(m.sum(axis=1), 1e-9)
    norm = _np.linalg.norm(vec, axis=1, keepdims=True)
    return vec / _np.maximum(norm, 1e-9)


async def embed(texts: list[str]):
    """Векторы пачкой. Модель синхронная, поэтому уводим её в поток."""
    if not texts:
        return None
    chunks = []
    for i in range(0, len(texts), VEC_BATCH):
        chunks.append(await asyncio.to_thread(_embed_sync, texts[i:i + VEC_BATCH]))
    return _np.concatenate(chunks) if len(chunks) > 1 else chunks[0]


async def _build_profile(chat_id: int):
    """Собрать матрицу улик чата. Готовые векторы берём из базы, новые считаем
    и туда же кладём — иначе каждый перезапуск прогонял бы всю копилку заново."""
    rows = await db.samples_profile(chat_id)
    scope = chat_id
    if len(rows) < config.NN_MIN_SAMPLES:
        # в новом чате улик ещё нет — сравниваем с общей копилкой
        rows = await db.samples_profile(None)
        scope = None
    if len(rows) < config.NN_MIN_SAMPLES:
        return None

    known = [r for r in rows if r["vec"]]
    fresh = [r for r in rows if not r["vec"]]
    vecs = {}
    for r in known:
        vecs[r["id"]] = _np.frombuffer(r["vec"], dtype=_np.float32)
    if fresh:
        got = await embed([r["text"] for r in fresh])
        for r, v in zip(fresh, got):
            v = v.astype(_np.float32)
            vecs[r["id"]] = v
            await db.sample_set_vec(r["id"], v.tobytes())

    ids = [r["id"] for r in rows]
    labels = [r["label"] for r in rows]
    matrix = _np.stack([vecs[i] for i in ids])
    logger.debug("профиль чата %s: %d улик (scope=%s)", chat_id, len(ids), scope)
    return matrix, labels, ids


async def profile(chat_id: int):
    """Профиль из кэша; раз в PROFILE_TTL перечитываем — улики прибавляются."""
    cached = _profile.get(chat_id)
    now = time.monotonic()
    if cached and now - cached[0] < PROFILE_TTL:
        return cached[1], cached[2], cached[3]
    built = await _build_profile(chat_id)
    if built is None:
        _profile[chat_id] = (now, None, [], [])
        return None, [], []
    _profile[chat_id] = (now, *built)
    return built


def invalidate(chat_id: int | None = None) -> None:
    """Сбросить кэш: улику переразметили — профиль устарел."""
    if chat_id is None:
        _profile.clear()
    else:
        _profile.pop(chat_id, None)


async def check(chat_id: int, text: str) -> dict | None:
    """Вердикт по тексту или None, если сравнивать не с чем.

    Считаем голоса TOP_K ближайших улик, взвешенные их близостью. score —
    насколько сообщение похоже на спам, в процентах от суммы голосов.
    """
    text = (text or "").strip()
    if len(text) < 10 or not await ensure():
        return None
    matrix, labels, ids = await profile(chat_id)
    if matrix is None:
        return None

    vec = (await embed([text]))[0]
    sims = matrix @ vec
    k = min(TOP_K, len(labels))
    top = _np.argsort(-sims)[:k]

    spam = ok = 0.0
    for i in top:
        # отрицательную близость в голос не пускаем: это «совсем не похоже»
        w = float(max(sims[i], 0.0))
        if labels[i] == "spam":
            spam += w
        else:
            ok += w
    total = spam + ok
    score = int(round(100 * spam / total)) if total else 0
    best = int(top[0])
    return {
        "score": score,
        "label": "spam" if labels[best] == "spam" else "ok",
        "nearest": int(round(100 * float(sims[best]))),
        "nearest_id": ids[best],
        "votes": k,
        "profile": len(labels),
    }


def _one_line(text: str, limit: int = 200) -> str:
    """Переносы в логе ломают чтение: одна улика — одна строка."""
    return " ".join((text or "").split())[:limit]


def _append(line: str) -> None:
    """Дописать в файл, при переполнении отложив старое в .1.

    Держим ровно одну старую копию: файл нужен, чтобы глазами посмотреть,
    что фильтр наловил за неделю, а не как вечный архив.
    """
    path = config.NN_LOG
    try:
        if os.path.exists(path) and os.path.getsize(path) > config.NN_LOG_MAX:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        logger.warning("не пишется %s", path, exc_info=True)


async def log_verdict(chat_id: int, title: str | None, user_id, text: str,
                      verdict: dict, threshold: int) -> None:
    """Строка в файл теневых решений.

    Пишем и то, что порога не добрало: по таким строкам как раз и видно,
    куда порог двигать. Помечаем, сработало бы или нет.
    """
    near = await db.sample_by_id(verdict["nearest_id"])
    hit = verdict["score"] >= threshold
    stamp = time.strftime("%d.%m %H:%M:%S")
    mark = f"СРАБОТАЛО {verdict['score']}%" if hit else f"мимо {verdict['score']}%"
    lines = [
        f"[{stamp}] {mark} · порог {threshold} · "
        f"{title or chat_id} ({chat_id}) · автор {user_id}",
        f"    ближайшая улика #{verdict['nearest_id']} "
        f"[{near['label'] if near else '?'}"
        f"{'/' + near['feature'] if near and near['feature'] else ''}] "
        f"схожесть {verdict['nearest']}% · в профиле {verdict['profile']}",
        f"    > {_one_line(text)}",
    ]
    if near is not None:
        lines.append(f"    ~ {_one_line(near['text'], 120)}")
    await asyncio.to_thread(_append, "\n".join(lines) + "\n\n")


async def shadow(chat_id: int, message, s) -> None:
    """Теневой прогон: вердикт пишем в файл, на модерацию не влияем.

    Живой модерации фильтр не касается сознательно — сперва надо посмотреть,
    что он ловит и на чём ошибается, и только потом давать ему права.
    """
    if s.nn_mode < 2:
        return
    text = message.text or message.caption or ""
    try:
        verdict = await check(chat_id, text)
    except Exception:
        logger.warning("нейрофильтр упал на сообщении в %s", chat_id, exc_info=True)
        return
    if verdict is None or verdict["score"] < config.NN_LOG_FLOOR:
        return
    who = getattr(getattr(message, "from_user", None), "id", None)
    title = getattr(getattr(message, "chat", None), "title", None)
    await log_verdict(chat_id, title, who, text, verdict, s.nn_threshold)
    if verdict["score"] >= s.nn_threshold:
        logger.info("nn shadow %s/%s score=%s", chat_id, who, verdict["score"])


async def keeper() -> None:
    """Фоновая уборка: держим копилку в размере и заранее считаем векторы.

    Векторы считаются один раз и лежат в базе, поэтому первое сообщение после
    перезапуска не ждёт, пока модель прогонит всю копилку.
    """
    while True:
        try:
            gone = await db.samples_trim(config.SAMPLE_KEEP)
            if gone:
                logger.info("копилка улик подрезана: удалено %d", gone)
            if await ensure():
                rows = await db.samples_without_vec()
                if rows:
                    vecs = await embed([r["text"] for r in rows])
                    for r, v in zip(rows, vecs):
                        await db.sample_set_vec(r["id"], v.astype(_np.float32).tobytes())
                    logger.info("посчитано векторов: %d", len(rows))
                    invalidate()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("уборка копилки не удалась", exc_info=True)
        await asyncio.sleep(6 * 3600)
