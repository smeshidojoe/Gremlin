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
from collections import deque

from .. import config, db, utils

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
# профиль чата: chat_id -> (когда собран, матрица векторов, метки, id улик, веса)
_profile: dict[int, tuple] = {}
# посчитанная разбивка на кучки: chat_id -> (когда, [список id в кучке], [описания])
_clusters: dict[int, tuple] = {}
# фразы-образцы: chat_id -> (когда собраны, матрица, [строки таблицы])
_phrases: dict[int, tuple] = {}
# последние сообщения чата для поиска всплесков: chat_id -> deque[(ts, uid, вектор)]
_recent: dict[int, deque] = {}
# спам-профили: chat_id -> (когда собраны, матрица, [тексты])
_faces: dict[int, tuple] = {}


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
    """Векторы для списка текстов: усреднение по токенам + нормировка.

    Текст сначала нормализуем: подменённые буквы и невидимые символы модель
    иначе не понимает, и «кaзuно» для неё не похоже на «казино» — то есть
    ровно на обфускации, ради которой спам её и использует, сравнение
    переставало работать.
    """
    enc = _tok.encode_batch([utils.normalize_text(t) for t in texts])
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
    # Чужие чаты в копилку не идут: у чата про рыбалку и у чата про крипту
    # разное представление о норме. Исключение — своя сетка: эти чаты
    # принадлежат одному человеку и похожи, там объединять честно (nn_net).
    # Пока улик меньше NN_MIN_SAMPLES — фильтр молчит и копит.
    s = await db.get_settings(chat_id)
    rows = (await db.samples_profile_net(chat_id) if s.nn_net
            else await db.samples_profile(chat_id))
    own = len(rows)
    # Пока своих улик мало, добавляем стартовый набор — чужие примеры спама
    # и обычных сообщений. Иначе новый чат месяц не понимает вообще ничего.
    # Как только своих набирается NN_SEED_UNTIL, набор отпадает: своя норма
    # всегда точнее чужой.
    if s.nn_seed and own < config.NN_SEED_UNTIL:
        rows = list(rows) + list(await db.samples_seed(config.NN_SEED_LIMIT))
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
    weights = None
    if len(ids) >= config.NN_LOGREG_MIN:
        weights = await asyncio.to_thread(_fit, matrix, labels)
    logger.debug("профиль чата %s: %d улик (своих %d), регрессия %s",
                 chat_id, len(ids), own, "есть" if weights else "рано")
    return matrix, labels, ids, weights


async def profile(chat_id: int):
    """Профиль из кэша; раз в PROFILE_TTL перечитываем — улики прибавляются."""
    cached = _profile.get(chat_id)
    now = time.monotonic()
    if cached and now - cached[0] < PROFILE_TTL:
        return cached[1:]
    built = await _build_profile(chat_id)
    if built is None:
        _profile[chat_id] = (now, None, [], [], None)
        return None, [], [], None
    _profile[chat_id] = (now, *built)
    return built


def invalidate(chat_id: int | None = None) -> None:
    """Сбросить кэш: улику переразметили — профиль устарел."""
    if chat_id is None:
        _profile.clear()
        _clusters.clear()
        _faces.clear()
    else:
        _profile.pop(chat_id, None)
        _clusters.pop(chat_id, None)
        _faces.pop(chat_id, None)


def invalidate_phrases(chat_id: int) -> None:
    """Список фраз изменился — пересчитаем их векторы при следующей проверке."""
    _phrases.pop(chat_id, None)


def _fit(matrix, labels):
    """Подобрать веса, отделяющие спам от нормы. Обычная логистическая регрессия.

    Соседи отвечают на вопрос «на что это похоже», регрессия — «что общего
    у всего спама сразу». Второе устойчивее: один нетипичный сосед больше
    не решает судьбу сообщения, а перекос выборки (спама всегда больше)
    гасится весом класса.

    Считаем руками на numpy: ради 312 чисел тащить в образ sklearn не стоит,
    а сам подбор — это триста раз умножить матрицу на вектор.
    """
    y = _np.array([1.0 if lab == "spam" else 0.0 for lab in labels])
    if y.min() == y.max():
        return None                    # один класс — разделять нечего
    n, dim = matrix.shape
    # вес класса: если спама вдесятеро больше нормы, каждая улика нормы
    # считается за десять, иначе регрессия просто скажет «всё спам»
    pos, neg = float(y.sum()), float(n - y.sum())
    w_pos, w_neg = n / (2 * pos), n / (2 * neg)
    sample_w = _np.where(y > 0.5, w_pos, w_neg)

    w = _np.zeros(dim)
    b = 0.0
    lr, l2 = 1.0, 1e-3
    for _ in range(config.NN_LOGREG_STEPS):
        z = matrix @ w + b
        p = 1.0 / (1.0 + _np.exp(-_np.clip(z, -30, 30)))
        err = (p - y) * sample_w
        w -= lr * ((matrix.T @ err) / n + l2 * w)
        b -= lr * float(err.mean())
    return w, b


def _score_logreg(vec, weights) -> int:
    w, b = weights
    z = float(vec @ w + b)
    p = 1.0 / (1.0 + 2.718281828459045 ** (-max(min(z, 30.0), -30.0)))
    return int(round(100 * p))


async def check(chat_id: int, text: str) -> dict | None:
    """Вердикт по тексту или None, если сравнивать не с чем.

    Считаем голоса TOP_K ближайших улик, взвешенные их близостью. score —
    насколько сообщение похоже на спам, в процентах от суммы голосов.
    """
    text = (text or "").strip()
    if len(text) < 10 or not await ensure():
        return None
    matrix, labels, ids, weights = await profile(chat_id)
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
    by = "соседи"
    if weights is not None:
        # регрессия считает оценку, соседи всё равно нужны: только они могут
        # показать, на какую конкретно улику это похоже
        score = _score_logreg(vec, weights)
        by = "регрессия"
    best = int(top[0])
    return {
        "score": score,
        "by": by,
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
        f"    оценку дала {verdict.get('by', 'соседи')}",
        f"    ближайшая улика #{verdict['nearest_id']} "
        f"[{near['label'] if near else '?'}"
        f"{'/' + near['feature'] if near and near['feature'] else ''}] "
        f"схожесть {verdict['nearest']}% · в профиле {verdict['profile']}",
        f"    > {_one_line(text)}",
    ]
    if near is not None:
        lines.append(f"    ~ {_one_line(near['text'], 120)}")
    await asyncio.to_thread(_append, "\n".join(lines) + "\n\n")


async def shadow(chat_id: int, message, s, extra: str = "") -> None:
    """Теневой прогон: вердикт пишем в файл, на модерацию не влияем.

    Живой модерации фильтр не касается сознательно — сперва надо посмотреть,
    что он ловит и на чём ошибается, и только потом давать ему права.
    """
    if s.nn_mode < 2:
        return
    # extra — распознанное в картинке или голосовом: для фильтра это обычный
    # текст, и сравнивать надо именно вместе с ним
    text = " ".join(filter(None, [message.text or message.caption or "", extra]))
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


def _kmeans(matrix, k: int, steps: int = 25):
    """Разложить векторы по k кучкам. Обычный k-means на косинусной близости.

    Векторы нормированы, поэтому «ближе» = больше скалярное произведение,
    и всё сводится к умножению матриц. Начальные центры берём подряд через
    равные промежутки: копилка отсортирована по времени, так в старт попадают
    улики из разных периодов, а не десять подряд про одно и то же.
    """
    n = matrix.shape[0]
    k = max(1, min(k, n))
    centers = matrix[_np.linspace(0, n - 1, k).astype(int)].copy()
    assign = _np.zeros(n, dtype=int)
    for _ in range(steps):
        sims = matrix @ centers.T
        new = sims.argmax(axis=1)
        if (new == assign).all():
            break
        assign = new
        for c in range(k):
            members = matrix[assign == c]
            if len(members):
                v = members.mean(axis=0)
                norm = float(_np.linalg.norm(v)) or 1.0
                centers[c] = v / norm
    return assign, centers


_STOP_WORDS = {
    "и", "в", "на", "не", "что", "с", "по", "за", "как", "это", "все", "так",
    "но", "у", "к", "из", "то", "же", "а", "для", "от", "или", "бы", "вы",
    "мы", "он", "она", "они", "я", "ты", "тут", "там", "уже", "если", "был",
}


def _top_words(texts: list[str], limit: int = 5) -> list[str]:
    """Частые слова кучки — чтобы человек с одного взгляда понял, что внутри."""
    from collections import Counter
    import re
    counter: Counter = Counter()
    for t in texts:
        for word in re.findall(r"[\w@#]{4,}", (t or "").lower()):
            if word not in _STOP_WORDS:
                counter[word] += 1
    return [w for w, _ in counter.most_common(limit)]


async def clusters(chat_id: int, scope: str = "unknown", k: int | None = None):
    """Разложить улики чата по кучкам похожих. Список описаний, свежий или из кэша.

    scope='unknown' — то, что лежит без пользы: ручные наказания, о которых
    бот не знает, спам это был или личные счёты. Разметив кучку целиком, вы
    одним нажатием превращаете сотню мёртвых записей в обучающие.
    scope='profile' — уже размеченное: смотреть, какие виды спама ходят в чат,
    и оптом исправлять то, что бот понял неправильно.
    """
    cached = _clusters.get(chat_id)
    now = time.monotonic()
    if cached and now - cached[0] < config.NN_CLUSTER_TTL and cached[3] == scope:
        return cached[2]
    if not await ensure():
        return []

    rows = (await db.samples_unknown(chat_id) if scope == "unknown"
            else await db.samples_profile(chat_id))
    if len(rows) < config.NN_MIN_SAMPLES:
        return []

    known = {r["id"]: _np.frombuffer(r["vec"], dtype=_np.float32)
             for r in rows if r["vec"]}
    fresh = [r for r in rows if not r["vec"]]
    if fresh:
        got = await embed([r["text"] for r in fresh])
        for r, v in zip(fresh, got):
            v = v.astype(_np.float32)
            known[r["id"]] = v
            await db.sample_set_vec(r["id"], v.tobytes())

    ids = [r["id"] for r in rows]
    matrix = _np.stack([known[i] for i in ids])
    assign, centers = await asyncio.to_thread(
        _kmeans, matrix, k or config.NN_CLUSTERS)

    out, buckets = [], []
    for c in range(int(assign.max()) + 1):
        idx = [i for i, a in enumerate(assign) if a == c]
        if not idx:
            continue
        # представитель — ближайшая к центру улика: она лучше всего описывает кучку
        sims = matrix[idx] @ centers[c]
        best = idx[int(sims.argmax())]
        texts = [rows[i]["text"] for i in idx]
        labels = [rows[i]["label"] for i in idx]
        buckets.append([ids[i] for i in idx])
        out.append({
            "size": len(idx),
            "sample": rows[best]["text"],
            "words": _top_words(texts),
            "spam": sum(1 for lab in labels if lab == "spam"),
            "ok": sum(1 for lab in labels if lab == "ok"),
            "unknown": sum(1 for lab in labels if lab == "unknown"),
        })
    order = sorted(range(len(out)), key=lambda i: -out[i]["size"])
    out = [out[i] for i in order]
    buckets = [buckets[i] for i in order]
    _clusters[chat_id] = (now, buckets, out, scope)
    return out


async def label_cluster(chat_id: int, index: int, label: str) -> int:
    """Пометить целую кучку. Возвращает, сколько улик переразметили."""
    cached = _clusters.get(chat_id)
    if not cached or index >= len(cached[1]):
        return 0
    ids = cached[1][index]
    moved = await db.samples_relabel_many(ids, label)
    invalidate(chat_id)
    logger.info("кучка %s в чате %s размечена как %s: %d улик",
                index, chat_id, label, moved)
    return moved


async def _phrase_matrix(chat_id: int):
    """Матрица векторов фраз-образцов чата. Считается один раз и лежит в базе."""
    cached = _phrases.get(chat_id)
    rows = await db.phrases_list(chat_id)
    if cached and cached[0] == len(rows) and all(
            r["id"] == old["id"] for r, old in zip(rows, cached[2])):
        return cached[1], cached[2]
    if not rows:
        _phrases[chat_id] = (0, None, [])
        return None, []

    fresh = [r for r in rows if not r["vec"]]
    vecs = {r["id"]: _np.frombuffer(r["vec"], dtype=_np.float32)
            for r in rows if r["vec"]}
    if fresh:
        got = await embed([r["text"] for r in fresh])
        for r, v in zip(fresh, got):
            v = v.astype(_np.float32)
            vecs[r["id"]] = v
            await db.phrase_set_vec(r["id"], v.tobytes())
    matrix = _np.stack([vecs[r["id"]] for r in rows])
    _phrases[chat_id] = (len(rows), matrix, list(rows))
    return matrix, list(rows)


async def match_phrase(chat_id: int, text: str, threshold: int):
    """Похоже ли сообщение на одну из фраз-образцов чата.

    Список слов ловит буквы, и спам переписывают под него быстрее, чем список
    пополняют. Фраза-образец ловит смысл: «заработок от 5000 в день» поймает
    и «доход десять тысяч ежедневно, пиши в личку».

    Возвращает (строка фразы, близость в процентах) или None.
    """
    text = (text or "").strip()
    if len(text) < 10 or not await ensure():
        return None
    matrix, rows = await _phrase_matrix(chat_id)
    if matrix is None:
        return None
    vec = (await embed([text]))[0]
    sims = matrix @ vec
    best = int(sims.argmax())
    score = int(round(100 * float(sims[best])))
    return (rows[best], score) if score >= threshold else None


async def burst(chat_id: int, user_id: int, text: str, min_users: int):
    """Рассылка: одно и то же по смыслу от разных людей за короткое время.

    Каждое сообщение по отдельности безобидно, поэтому правилами такое
    не ловится вовсе. Держим в памяти хвост последних сообщений чата и
    считаем, сколько разных авторов написали похожее.

    Возвращает {'users': сколько человек, 'msgs': сколько сообщений} или None.
    """
    text = (text or "").strip()
    if len(text) < 10 or not await ensure():
        return None
    vec = (await embed([text]))[0]
    now = time.monotonic()
    tail = _recent.setdefault(chat_id, deque(maxlen=config.BURST_KEEP))
    while tail and now - tail[0][0] > config.BURST_WINDOW:
        tail.popleft()

    same_users, msgs = {user_id}, 1
    for _ts, uid, old in tail:
        if float(old @ vec) >= config.BURST_SIM:
            same_users.add(uid)
            msgs += 1
    tail.append((now, user_id, vec))
    if len(same_users) >= min_users:
        return {"users": len(same_users), "msgs": msgs}
    return None


def forget_burst(chat_id: int) -> None:
    """Забыть хвост сообщений — после того, как всплеск уже наказан,
    иначе следующее сообщение сработает по тем же следам ещё раз."""
    _recent.pop(chat_id, None)


async def remember_face(chat_id: int, user_id: int, name: str, label: str) -> None:
    """Запомнить профиль: имя и ник того, кого забанили (или не тронули).

    Хранится в той же копилке, но с origin='profile' — в текстовый профиль
    такие улики не попадают, они сравниваются только с профилями.
    """
    await db.sample_add(chat_id, user_id, "profile", label, name, feature="профиль")
    _faces.pop(chat_id, None)


async def face_score(chat_id: int, name: str) -> int | None:
    """Насколько имя похоже на профили, за которые уже банили (в процентах).

    «Анна | 18+ ЛС» и «Кристина ❤️ пиши в лс» для эвристик разные, для модели —
    одно и то же. None — сравнивать не с чем.
    """
    name = " ".join((name or "").split())
    if len(name) < 4 or not await ensure():
        return None
    cached = _faces.get(chat_id)
    now = time.monotonic()
    if not cached or now - cached[0] > PROFILE_TTL:
        rows = [r for r in await db.samples_of_origin(chat_id, "profile")
                if r["label"] == "spam"]
        if len(rows) < 5:            # на трёх примерах сравнивать нечего
            _faces[chat_id] = (now, None, [])
            return None
        vecs = await embed([r["text"] for r in rows])
        _faces[chat_id] = (now, vecs, [r["text"] for r in rows])
        cached = _faces[chat_id]
    if cached[1] is None:
        return None
    vec = (await embed([name]))[0]
    return int(round(100 * float((cached[1] @ vec).max())))


async def suggest_threshold(chat_id: int) -> int | None:
    """Порог, при котором ни одна улика-норма не сочлась бы спамом.

    Человек иначе тыкает пресеты вслепую. Здесь же считаем по собственной
    копилке чата: прогоняем через фильтр то, что размечено как норма, и берём
    ближайший пресет выше самой высокой её оценки.
    """
    matrix, labels, ids, weights = await profile(chat_id)
    if matrix is None:
        return None
    ok_rows = [i for i, lab in enumerate(labels) if lab == "ok"]
    if len(ok_rows) < 5:
        return None
    worst = 0
    for i in ok_rows:
        vec = matrix[i]
        if weights is not None:
            score = _score_logreg(vec, weights)
        else:
            sims = matrix @ vec
            sims[i] = -1.0            # сам с собой не сравниваем
            top = _np.argsort(-sims)[:min(TOP_K, len(labels) - 1)]
            spam = sum(max(float(sims[j]), 0.0) for j in top if labels[j] == "spam")
            total = sum(max(float(sims[j]), 0.0) for j in top)
            score = int(round(100 * spam / total)) if total else 0
        worst = max(worst, score)
    for preset in config.NN_THRESHOLD_PRESETS:
        if preset > worst:
            return preset
    return config.NN_THRESHOLD_PRESETS[-1]


async def doubtful(chat_id: int, limit: int | None = None):
    """Улики, на которых фильтр колеблется. Их и стоит разметить руками.

    Берём неразмеченное (ручные наказания) и оцениваем каждую; в ответ идут
    те, что попали в полосу неуверенности. Десяток таких ответов двигает
    качество сильнее сотни очевидных.
    """
    limit = limit or config.NN_DOUBT_LIMIT
    if not await ensure():
        return []
    matrix, labels, ids, weights = await profile(chat_id)
    if matrix is None:
        return []
    rows = await db.samples_unknown(chat_id, 300)
    if not rows:
        return []
    vecs = await embed([r["text"] for r in rows])
    low, high = config.NN_DOUBT
    out = []
    for row, vec in zip(rows, vecs):
        if weights is not None:
            score = _score_logreg(vec, weights)
        else:
            sims = matrix @ vec
            top = _np.argsort(-sims)[:min(TOP_K, len(labels))]
            spam = sum(max(float(sims[j]), 0.0) for j in top if labels[j] == "spam")
            total = sum(max(float(sims[j]), 0.0) for j in top)
            score = int(round(100 * spam / total)) if total else 0
        if low <= score <= high:
            out.append({"id": row["id"], "text": row["text"], "score": score})
    out.sort(key=lambda x: abs(x["score"] - 50))
    return out[:limit]


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
                # стартовый набор считаем отдельно: он общий, в samples_without_vec
                # не попадает (там только свои улики чатов), а без готовых векторов
                # первое сообщение в молодом чате ждало бы прогон всех четырёхсот
                rows = [r for r in await db.samples_seed(config.NN_SEED_LIMIT)
                        if not r["vec"]]
                rows += await db.samples_without_vec()
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
