"""Обученная оценка: спам ли аккаунт — по всем признакам сразу, с весами из исходов.

Зачем. Единая оценка (verdict.py) складывает сигналы по очкам, придуманным
руками: «обфускация — 25», «не участник — +15». Здесь те же вопросы, но веса
подбирает логистическая регрессия по тому, чем случаи на самом деле кончились:
админ снял наказание, забанил из карточки, нажал «Не трогать».

На чём учится. На случаях из cases.py: у каждого снимок признаков на момент
решения и исход. Исход от человека — главная метка; автобан, который за три
дня никто не снял, — слабая: бот мог ошибиться, а админ не заметить. Проверка
качества (tools/measure.py) идёт только по исходам от человека.

Чего здесь нет — и нарочно. Какое правило сработало: у автобана оно и есть
метка, модель выучила бы «сработало правило — спам» и повторяла бы правила.
Всего, что меняет само наказание, — это отрезано ещё при записи случая.

Пока ничего не решает: пишется рядом с вердиктом в журнал. Переключать бота
на неё — отдельное решение, когда замер покажет, что она лучше правил.
"""
import json
import logging
import math
import time

from .. import db

logger = logging.getLogger("gremlin.score")

KV_KEY = "score_weights"
MIN_EACH = 50         # меньше — не учимся: модель запомнит людей, а не спам
WEAK_AFTER = 3 * 86400  # автобан без снятия за это время — слабая метка «спам»


def _num(v, default=0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _log(v, top: float) -> float:
    return min(math.log1p(max(_num(v), 0.0)) / math.log1p(top), 1.0)


def _face_margin(f: dict) -> float:
    got = f.get("face")
    if not got:
        return 0.0
    spam, ok = got[0], got[1] if len(got) > 1 else None
    return max(min((spam - (ok if ok is not None else 0)) / 20.0, 1.0), -1.0)


# Признаки по порядку: имя и как достать число из снимка. Всё — от 0 до 1
# (перевес профиля — от −1 до 1): иначе регрессия с одним шагом на всех
# сходилась бы по «дням в чате» вечность, а по флажкам — за такт.
FEATURES = (
    ("text", lambda f: _num(f.get("text"), 50.0) / 100),
    ("has_text", lambda f: 1.0 if f.get("text") is not None else 0.0),
    ("face", _face_margin),
    # id до ~9 млрд и растёт по порядку: доля от 10 млрд — «насколько свежий»
    ("acc_id", lambda f: min(_num(f.get("id")) / 1e10, 1.0)),
    ("photo", lambda f: 1.0 if f.get("photo") else 0.0),
    ("nsfw", lambda f: _num(f.get("nsfw")) / 100),
    ("channel", lambda f: 1.0 if f.get("channel") else 0.0),
    ("bio", lambda f: 1.0 if f.get("bio") else 0.0),
    ("premium", lambda f: 1.0 if f.get("premium") else 0.0),
    ("ru", lambda f: 1.0 if (f.get("lang") or "").startswith("ru") else 0.0),
    ("member", lambda f: 1.0 if f.get("member") else 0.0),
    ("days", lambda f: _log(f.get("days"), 365)),
    ("msgs", lambda f: _log(f.get("msgs"), 1000)),
    ("pun", lambda f: min(_num(f.get("pun")), 5.0) / 5),
    ("name_hard", lambda f: min(_num(f.get("name_hard")), 100.0) / 100),
    ("name_cos", lambda f: min(_num(f.get("name_cos")), 100.0) / 100),
    ("msg_hard", lambda f: min(_num(f.get("msg_hard")), 100.0) / 100),
    ("msg_cos", lambda f: min(_num(f.get("msg_cos")), 100.0) / 100),
)
NAMES = tuple(name for name, _ in FEATURES)


def vector(features: dict) -> list[float]:
    """Снимок признаков случая -> числа в порядке FEATURES."""
    f = features or {}
    return [fn(f) for _name, fn in FEATURES]


def _sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(min(z, 30.0), -30.0)))


def score_with(model: dict, features: dict) -> int:
    z = sum(w * x for w, x in zip(model["w"], vector(features))) + model["b"]
    return int(round(100 * _sigmoid(z)))


# ---------- набор для обучения ----------

async def dataset(now: int | None = None) -> list[dict]:
    """Случаи с исходом: {"case", "user", "ts", "label", "human", "features"}.

    Метка случая — спам, если хоть одна его часть спам; норма — если все
    размеченные части норма. От одного человека берём последний случай с
    каждой меткой: сто банов одного аккаунта — это один пример, а не сто.
    """
    now = now or int(time.time())
    cur = await db._db.execute(
        """SELECT case_id, user_id, ts, label, labeled_by, data FROM samples
           WHERE case_id IS NOT NULL AND chat_id != 0 ORDER BY id""")
    cases: dict[int, dict] = {}
    for r in await cur.fetchall():
        c = cases.setdefault(r["case_id"], {"case": r["case_id"], "user": r["user_id"],
                                            "ts": r["ts"], "labels": set(),
                                            "human": False, "features": None})
        c["labels"].add(r["label"])
        c["human"] = c["human"] or r["labeled_by"] is not None
        if c["features"] is None and r["data"]:
            c["features"] = json.loads(r["data"]).get("features")
    out, seen = [], {}
    for c in cases.values():
        labels = c.pop("labels")
        if "spam" in labels:
            c["label"] = "spam"
        elif labels == {"ok"}:
            c["label"] = "ok"
        else:
            continue                      # исхода ещё нет
        if c["features"] is None:
            continue
        # слабая метка спама — только у автобана, который успели бы снять
        if not c["human"] and c["label"] == "spam" and now - c["ts"] < WEAK_AFTER:
            continue
        key = (c["user"], c["label"])
        if key in seen:
            out[seen[key]] = c            # последний случай человека
        else:
            seen[key] = len(out)
            out.append(c)
    return out


def enough(rows: list[dict]) -> tuple[bool, int, int]:
    spam = sum(1 for r in rows if r["label"] == "spam")
    ok = len(rows) - spam
    return spam >= MIN_EACH and ok >= MIN_EACH, spam, ok


def fit(rows: list[dict]) -> dict | None:
    """Подобрать веса. None — один класс, разделять нечего."""
    import numpy as np

    from . import nn
    if nn._np is None:
        nn._np = np                       # регрессия нейрофильтра считает на нём
    matrix = np.array([vector(r["features"]) for r in rows], dtype=np.float64)
    got = nn._fit(matrix, [r["label"] for r in rows])
    if got is None:
        return None
    w, b = got
    return {"w": [float(x) for x in w], "b": float(b), "features": list(NAMES)}


# ---------- веса в базе ----------

_cached: tuple[float, dict | None] = (0.0, None)


async def load() -> dict | None:
    """Веса из базы. Набор признаков сменился — старые веса не годятся."""
    global _cached
    if time.monotonic() - _cached[0] < 600:
        return _cached[1]
    raw = await db.kv_get(KV_KEY)
    model = json.loads(raw) if raw else None
    if model and model.get("features") != list(NAMES):
        model = None
    _cached = (time.monotonic(), model)
    return model


async def save(model: dict) -> None:
    global _cached
    await db.kv_set(KV_KEY, json.dumps(model, ensure_ascii=False))
    _cached = (time.monotonic(), model)


async def predict(features: dict | None) -> int | None:
    """Оценка 0–100 или None, если модели ещё нет."""
    if not features:
        return None
    model = await load()
    return score_with(model, features) if model else None


async def retrain() -> str:
    """Переобучить на всех случаях. Возвращает, что вышло, — для лога."""
    rows = await dataset()
    ok, n_spam, n_ok = enough(rows)
    if not ok:
        return f"рано: спама {n_spam}, нормы {n_ok}, нужно по {MIN_EACH}"
    from .. import utils
    model = await utils.in_model_thread(fit, rows)
    if model is None:
        return "не вышло: один класс"
    model.update(n_spam=n_spam, n_ok=n_ok, ts=int(time.time()))
    await save(model)
    return f"обучена: спама {n_spam}, нормы {n_ok}"
