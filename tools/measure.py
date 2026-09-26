"""Замер качества: сколько спама ловит модель и на скольких людях ошибается.

Зачем. Без замера любая правка оценивается на глаз: «вроде стало лучше».
Здесь один раз откладывается тестовый набор, и каждый прогон меряет модель
на нём — теми же nn.check и nn.face_score, что работают в боте.

Живую базу скрипт не трогает: снимает копию только на чтение во временную
папку, работает с ней и удаляет. Бота останавливать не нужно.

    python tools/measure.py            # замер
    python tools/measure.py --new      # пересобрать тестовый набор

Почему почти одинаковые тексты не делятся. Спам идёт волнами одного шаблона.
При случайном разбиении в копилке остаётся почти та же строка, что в тесте,
и модель «узнаёт» её с первого взгляда — замер показывает не то, как она
поймает новую волну, а то, что она помнит старую. Поэтому тексты с
близостью выше SAME держатся вместе, а из копилки при замере убирается всё,
что так же близко к любому тестовому, — даже если оно пришло позже.

Профили не замораживаются: спам-профилей пока два десятка, и в отложенном
наборе их было бы четыре — один промах давал бы 25%. Каждый профиль
проверяется по всем остальным, кроме своих почти-копий.

Набор и история замеров лежат в data/eval/ — в git они не идут: там тексты
людей из чатов.
"""
import argparse
import asyncio
import hashlib
import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

LIVE_DB = os.path.join(ROOT, "data", "gremlin.sqlite3")
EVAL_DIR = os.path.join(ROOT, "data", "eval")
TESTSET = os.path.join(EVAL_DIR, "testset.jsonl")
HISTORY = os.path.join(EVAL_DIR, "history.jsonl")

SAME = 0.92          # близость, с которой тексты считаем одним шаблоном
TEST_SHARE = 0.2     # доля копилки в тесте
SEED = 20260926      # разбиение повторяемо
THRESHOLDS = (70, 80, 90)


def _copy_live(tmp: str) -> str:
    """Копия живой базы через backup: читаем, ничего не пишем и не блокируем."""
    path = os.path.join(tmp, "copy.sqlite3")
    src = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    dst = sqlite3.connect(path)
    src.backup(dst)
    src.close()
    dst.close()
    return path


def _setup_env(tmp: str, db_path: str) -> None:
    """Все пишущие пути — во временную папку, до импорта gremlin."""
    os.environ.update(DB_PATH=db_path, LOG_PATH=os.path.join(tmp, "bot.log"),
                      STATS_DB=os.path.join(tmp, "stats.db"))
    for name in ("MEDIA_DIR", "TRIG_DIR", "BACKUP_DIR", "NN_LOG_DIR", "UNI_LOG_DIR"):
        os.environ[name] = os.path.join(tmp, name.lower())
    os.environ.setdefault("NN_MODEL_DIR", os.path.join(ROOT, "tools", "models", "rubert-tiny2"))
    os.environ.setdefault("NSFW_MODEL_DIR", os.path.join(ROOT, "tools", "models", "nsfw"))


# ---------- метрики ----------

def auc(spam: list[int], ok: list[int]) -> float | None:
    """Вероятность, что случайный спам оценён выше случайной нормы."""
    if not spam or not ok:
        return None
    ok_sorted = sorted(ok)
    import bisect
    win = 0.0
    for s in spam:
        lo = bisect.bisect_left(ok_sorted, s)
        hi = bisect.bisect_right(ok_sorted, s)
        win += lo + (hi - lo) / 2
    return round(win / (len(spam) * len(ok)), 4)


def pct(part: int, whole: int) -> str:
    return f"{part}/{whole} ({100 * part / whole:.1f}%)" if whole else "—"


# ---------- тестовый набор ----------

def _groups(vecs, np) -> list[int]:
    """Номер группы для каждого текста: близкие выше SAME — одна группа."""
    n = len(vecs)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for start in range(0, n, 512):
        block = vecs[start:start + 512] @ vecs.T
        for bi, j in zip(*np.nonzero(block >= SAME)):
            i = start + int(bi)
            if i < j:
                a, b = find(i), find(int(j))
                if a != b:
                    parent[a] = b
    return [find(i) for i in range(n)]


def build_testset(rows, vecs, np) -> list[dict]:
    """Отложить TEST_SHARE спама и нормы целыми группами почти-копий."""
    groups = _groups(vecs, np)
    members: dict[int, list[int]] = {}
    for i, g in enumerate(groups):
        members.setdefault(g, []).append(i)
    order = list(members)
    random.Random(SEED).shuffle(order)
    want = {lab: TEST_SHARE * sum(1 for r in rows if r["label"] == lab)
            for lab in ("spam", "ok")}
    got = {"spam": 0, "ok": 0}
    picked = []
    for g in order:
        idx = members[g]
        labs = [rows[i]["label"] for i in idx]
        # группа нужна, если её основной метки ещё не хватает
        main = max(set(labs), key=labs.count)
        if got[main] >= want[main]:
            continue
        for i in idx:
            got[rows[i]["label"]] += 1
            picked.append(i)
    return [{"id": rows[i]["id"], "label": rows[i]["label"], "origin": rows[i]["origin"],
             "text": rows[i]["text"]} for i in sorted(picked)]


def load_testset() -> list[dict]:
    with open(TESTSET, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def testset_hash(items: list[dict]) -> str:
    raw = "\n".join(f"{t['label']}\t{t['text']}" for t in items)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


# ---------- замер ----------

async def measure_messages(db, nn, np, rebuild: bool) -> dict:
    rows = list(await db.samples_pool("msg"))
    vecs = await nn.embed([r["text"] for r in rows])
    if rebuild or not os.path.exists(TESTSET):
        items = build_testset(rows, vecs, np)
        os.makedirs(EVAL_DIR, exist_ok=True)
        with open(TESTSET, "w", encoding="utf-8") as f:
            for t in items:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")
        print(f"тестовый набор собран заново: {len(items)} текстов → {TESTSET}")
    items = load_testset()
    tvecs = await nn.embed([t["text"] for t in items])

    # из копилки — тестовые и всё, что близко к любому из них
    near = (vecs @ tvecs.T).max(axis=1) >= SAME
    test_texts = {t["text"] for t in items}
    keep_ids = {r["id"] for r, n in zip(rows, near)
                if not n and r["text"] not in test_texts}
    real_pool = db.samples_pool

    async def pool(kind="msg", limit=None):
        got = await real_pool(kind) if limit is None else await real_pool(kind, limit)
        return [r for r in got if r["id"] in keep_ids] if kind == "msg" else got

    db.samples_pool = pool
    nn.invalidate()
    try:
        scored = []
        for t in items:
            got = await nn.check(0, t["text"])
            if got is not None:
                scored.append((t, got["score"]))
    finally:
        db.samples_pool = real_pool
        nn.invalidate()

    spam = [s for t, s in scored if t["label"] == "spam"]
    ok = [s for t, s in scored if t["label"] == "ok"]
    live_ok = [s for t, s in scored if t["label"] == "ok" and t["origin"] == "random"]
    out = {"pool": len(keep_ids),
           "removed_near": sum(1 for r, n in zip(rows, near)
                               if n and r["text"] not in test_texts),
           "test": len(items), "scored": len(scored),
           "spam": len(spam), "ok": len(ok), "live_ok": len(live_ok),
           "auc": auc(spam, ok), "auc_live": auc(spam, live_ok), "at": {}}
    for th in THRESHOLDS:
        out["at"][th] = {"caught": sum(s >= th for s in spam),
                         "false": sum(s >= th for s in ok),
                         "false_live": sum(s >= th for s in live_ok)}
    top_ok = max(ok, default=0)
    out["clean_caught"] = sum(s > top_ok for s in spam)
    out["clean_from"] = top_ok + 1
    out["worst_ok"] = [t["text"][:90] for t, s in sorted(
        ((t, s) for t, s in scored if t["label"] == "ok"), key=lambda x: -x[1])[:3]]
    return out


async def measure_profiles(db, nn, np) -> dict:
    rows = list(await db.samples_pool("prof"))
    if sum(r["label"] == "spam" for r in rows) < 6:
        return {"profiles": len(rows)}
    vecs = await nn.embed([r["text"] for r in rows])
    sims = vecs @ vecs.T
    real_pool = db.samples_pool
    # две оценки: голое сходство со спамом и перевес спама над нормой —
    # по перевесу face_hit и решает, похож ли профиль сильнее на плохих
    spam_s, ok_s, spam_m, ok_m, hits, false = [], [], [], [], 0, 0
    try:
        for i, r in enumerate(rows):
            # без себя и без своих почти-копий: иначе профиль узнаёт сам себя
            keep = {rows[j]["id"] for j in range(len(rows))
                    if j != i and sims[i, j] < SAME}

            async def pool(kind="msg", limit=None, keep=keep):
                got = await real_pool(kind) if limit is None else await real_pool(kind, limit)
                return [x for x in got if x["id"] in keep] if kind == "prof" else got

            db.samples_pool = pool
            nn._faces.clear()
            got = await nn.face_score(0, r["text"])
            if got is None:
                continue
            hit = nn.face_hit(got)
            margin = got[0] - (got[1] if got[1] is not None else 0)
            if r["label"] == "spam":
                spam_s.append(got[0])
                spam_m.append(margin)
                hits += hit
            else:
                ok_s.append(got[0])
                ok_m.append(margin)
                false += hit
    finally:
        db.samples_pool = real_pool
        nn.invalidate()
    return {"profiles": len(rows), "spam": len(spam_s), "ok": len(ok_s),
            "auc": auc(spam_s, ok_s), "auc_margin": auc(spam_m, ok_m),
            "hit": hits, "false": false}


async def measure_learned(db) -> dict:
    """Обученная оценка против нынешних правил — на свежих случаях.

    Учим на старых случаях, проверяем на новых 30% с исходом от человека:
    так же, как будет в жизни, — модель всегда судит тех, кого не видела.
    Правила: единая оценка из журнала вердиктов и «наказал ли бот сам».
    """
    from gremlin.services import score
    rows = await score.dataset()
    human = sorted((r for r in rows if r["human"]), key=lambda r: r["ts"])
    test = human[int(len(human) * 0.7):]
    start = test[0]["ts"] if test else 0
    train = [r for r in rows if r["ts"] < start]
    ok_train, n_spam, n_ok = score.enough(train)
    t_spam = sum(r["label"] == "spam" for r in test)
    out = {"train_spam": n_spam, "train_ok": n_ok, "test_spam": t_spam,
           "test_ok": len(test) - t_spam}
    if not ok_train or not t_spam or t_spam == len(test):
        return out
    model = score.fit(train)
    if model is None:
        return out
    spam_l, ok_l, spam_v, ok_v = [], [], [], []
    rule = {"caught": 0, "false": 0}
    for r in test:
        learned = score.score_with(model, r["features"])
        cur = await db._db.execute(
            "SELECT total FROM verdicts WHERE case_id = ? ORDER BY id DESC LIMIT 1",
            (r["case"],))
        v = await cur.fetchone()
        cur = await db._db.execute(
            """SELECT 1 FROM samples s JOIN punishments p ON p.id = s.pid
               WHERE s.case_id = ? AND p.by_id IS NULL LIMIT 1""", (r["case"],))
        by_bot = await cur.fetchone() is not None
        if r["label"] == "spam":
            spam_l.append(learned)
            if v:
                spam_v.append(v["total"])
            rule["caught"] += by_bot
        else:
            ok_l.append(learned)
            if v:
                ok_v.append(v["total"])
            rule["false"] += by_bot
    out.update(auc=auc(spam_l, ok_l), auc_verdict=auc(spam_v, ok_v),
               caught=sum(x >= 50 for x in spam_l), false=sum(x >= 50 for x in ok_l),
               rule=rule, weights=dict(zip(model["features"],
                                           [round(w, 2) for w in model["w"]])))
    return out


async def count_cases(db) -> dict:
    """Сколько случаев с исходом накопилось — материал для единой оценки."""
    q = lambda sql: db._db.execute(sql)   # noqa: E731
    cur = await q("""SELECT
        COUNT(DISTINCT case_id),
        COUNT(DISTINCT CASE WHEN labeled_by IS NOT NULL THEN case_id END),
        COUNT(DISTINCT CASE WHEN label = 'unknown' THEN case_id END)
        FROM samples WHERE case_id IS NOT NULL AND chat_id != 0""")
    total, human, open_ = await cur.fetchone()
    cur = await q("SELECT COUNT(*) FROM verdicts WHERE case_id IS NOT NULL")
    (linked,) = await cur.fetchone()
    return {"cases": total, "human": human, "open": open_, "verdicts": linked}


def _commit() -> str:
    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "gremlin"], cwd=ROOT,
                               capture_output=True, text=True).stdout.strip()
        return rev + ("+правки" if dirty else "")
    except Exception:
        return "?"


def _last_run(tset: str) -> dict | None:
    if not os.path.exists(HISTORY):
        return None
    with open(HISTORY, encoding="utf-8") as f:
        runs = [json.loads(line) for line in f if line.strip()]
    same = [r for r in runs if r.get("testset") == tset]
    return same[-1] if same else None


def _delta(now, before) -> str:
    if before is None or now is None:
        return ""
    d = now - before
    return "" if abs(d) < 1e-9 else f"  ({'+' if d > 0 else ''}{round(d, 4)})"


def report(msg: dict, prof: dict, cases: dict, learned: dict,
           before: dict | None) -> None:
    bm = (before or {}).get("msg", {})
    bp = (before or {}).get("prof", {})
    print("\n== Сообщения ==")
    print(f"копилка для замера: {msg['pool']} (убрано пришедших позже почти-копий "
          f"теста: {msg['removed_near']})")
    print(f"тест: {msg['test']}, оценено {msg['scored']} — спам {msg['spam']}, "
          f"норма {msg['ok']} (из них живых {msg['live_ok']})")
    print(f"AUC: {msg['auc']}{_delta(msg['auc'], bm.get('auc'))}"
          f" · по живой норме: {msg['auc_live']}{_delta(msg['auc_live'], bm.get('auc_live'))}")
    for th in THRESHOLDS:
        a = msg["at"][th]
        b = bm.get("at", {}).get(str(th), {})
        print(f"  порог {th}: поймано {pct(a['caught'], msg['spam'])}"
              f"{_delta(a['caught'], b.get('caught'))}, ложных {pct(a['false'], msg['ok'])}"
              f"{_delta(a['false'], b.get('false'))}, из них живых {a['false_live']}")
    print(f"без единого ложного (с {msg['clean_from']}): поймано "
          f"{pct(msg['clean_caught'], msg['spam'])}{_delta(msg['clean_caught'], bm.get('clean_caught'))}")
    if msg["worst_ok"]:
        print("норма с самой высокой оценкой:")
        for t in msg["worst_ok"]:
            print(f"  · {t}")
    print("\n== Профили (каждый по остальным) ==")
    if "auc" not in prof:
        print(f"мало спам-профилей для замера: всего профилей {prof['profiles']}")
    else:
        print(f"спам {prof['spam']}, норма {prof['ok']}")
        print(f"AUC по сходству со спамом: {prof['auc']}{_delta(prof['auc'], bp.get('auc'))}"
              f" · по перевесу над нормой: {prof['auc_margin']}"
              f"{_delta(prof['auc_margin'], bp.get('auc_margin'))}")
        print(f"попаданий: {pct(prof['hit'], prof['spam'])}{_delta(prof['hit'], bp.get('hit'))}, "
              f"ложных: {pct(prof['false'], prof['ok'])}{_delta(prof['false'], bp.get('false'))}")
    print("\n== Случаи (для единой оценки) ==")
    print(f"случаев: {cases['cases']}, с исходом от человека: {cases['human']}, "
          f"ждут исхода: {cases['open']}, вердиктов со случаем: {cases['verdicts']}")
    print("\n== Обученная оценка против правил ==")
    from gremlin.services.score import MIN_EACH
    if "auc" not in learned:
        print(f"рано: для обучения спама {learned['train_spam']}, нормы "
              f"{learned['train_ok']} (нужно по {MIN_EACH}); для проверки спама "
              f"{learned['test_spam']}, нормы {learned['test_ok']}")
    else:
        bl = (before or {}).get("learned", {})
        n_s, n_o = learned["test_spam"], learned["test_ok"]
        print(f"проверка: спам {n_s}, норма {n_o} (свежие 30% с исходом от человека)")
        print(f"обученная: AUC {learned['auc']}{_delta(learned['auc'], bl.get('auc'))}, "
              f"с 50: поймано {pct(learned['caught'], n_s)}, "
              f"ложных {pct(learned['false'], n_o)}")
        print(f"единая оценка: AUC {learned['auc_verdict']}")
        print(f"правила (наказал ли бот): поймано {pct(learned['rule']['caught'], n_s)}, "
              f"ложных {pct(learned['rule']['false'], n_o)}")
        print("веса: " + ", ".join(f"{k} {v:+}" for k, v in sorted(
            learned["weights"].items(), key=lambda kv: -abs(kv[1]))))
    if before:
        print(f"\nсравнение с прогоном {before['date']} ({before['commit']})")


async def main(rebuild: bool) -> None:
    tmp = tempfile.mkdtemp(prefix="gremlin-measure-")
    try:
        _setup_env(tmp, _copy_live(tmp))
        import numpy as np

        from gremlin import db
        from gremlin.services import nn
        await db.init()
        if not await nn.ensure():
            sys.exit(f"модель не загрузилась: {nn.status()}")

        # один текст — один прогон модели: проверка по тесту и профилям
        # спрашивает одни и те же строки десятки раз
        real_embed, memo = nn.embed, {}

        async def embed(texts):
            fresh = [t for t in dict.fromkeys(texts) if t not in memo]
            if fresh:
                for t, v in zip(fresh, await real_embed(fresh)):
                    memo[t] = v
            return np.stack([memo[t] for t in texts]) if texts else None
        nn.embed = embed

        t0 = time.time()
        msg = await measure_messages(db, nn, np, rebuild)
        prof = await measure_profiles(db, nn, np)
        cases = await count_cases(db)
        learned = await measure_learned(db)
        await db.close()

        tset = testset_hash(load_testset())
        before = _last_run(tset)
        report(msg, prof, cases, learned, before)
        run = {"date": time.strftime("%Y-%m-%d %H:%M"), "commit": _commit(),
               "testset": tset, "msg": msg, "prof": prof, "cases": cases,
               "learned": learned}
        with open(HISTORY, "a", encoding="utf-8") as f:
            f.write(json.dumps(run, ensure_ascii=False) + "\n")
        print(f"\nзамер за {time.time() - t0:.0f} с, записан в {HISTORY}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--new", action="store_true", help="пересобрать тестовый набор")
    asyncio.run(main(ap.parse_args().new))
