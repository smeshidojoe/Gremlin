"""Загрузить чужой датасет в стартовый набор улик.

Зачем: новый чат первое время ничего не понимает — сравнивать не с чем.
Стартовый набор даёт ему готовые примеры спама и обычных сообщений, а как
только чат накопит свои двести, набор отключается сам: своя норма точнее.

Бот при этом должен быть остановлен. База лежит на примонтированном томе,
и запись в неё снаружи во время работы бота ломает файл — проверено.

    docker compose stop gremlin
    python tools/import_dataset.py путь/к/файлу.jsonl
    docker compose start gremlin

Формат: JSONL, CSV или parquet с колонкой текста и колонкой метки. Имена
колонок скрипт угадывает сам (text/message/content, label/is_spam/target),
можно задать явно: --text-col text --label-col label.

Метка: 1 / spam / true — спам, всё остальное — норма.

Лицензию датасета проверяйте сами: например, telegram_spam_ru лежит под
CC BY-NC 4.0 — некоммерческое использование со ссылкой на автора.
"""
import argparse
import asyncio
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEXT_COLS = ("text", "message", "content", "body", "sentence")
LABEL_COLS = ("label", "is_spam", "spam", "target", "class")
SPAM_VALUES = {"1", "spam", "true", "yes", "спам"}


def _rows_jsonl(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def _rows_csv(path):
    import csv
    with open(path, encoding="utf-8", newline="") as f:
        yield from csv.DictReader(f)


def _rows_parquet(path):
    try:
        import pandas as pd
    except ImportError:
        sys.exit("для parquet нужен pandas:  pip install pandas pyarrow")
    for row in pd.read_parquet(path).to_dict("records"):
        yield row


def read_rows(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".jsonl", ".json"):
        return _rows_jsonl(path)
    if ext == ".csv":
        return _rows_csv(path)
    if ext == ".parquet":
        return _rows_parquet(path)
    sys.exit(f"не знаю формат {ext}: нужен .jsonl, .csv или .parquet")


def pick(row, names, given):
    if given:
        return given
    for n in names:
        if n in row:
            return n
    return None


def prepare(rows, args) -> list[tuple[str, str]]:
    """Строки датасета -> [(текст, метка)], перемешанные и уравненные.

    Перемешиваем всегда: датасеты почти всегда лежат отсортированными по метке
    (сначала вся норма, потом весь спам), и любая выборка «первых N» дала бы
    один класс. Порядок фиксированный — повторный запуск даст ту же выборку.

    С --limit берём поровну спама и нормы: перекос выборки регрессия гасит
    весом класса, но лишние примеры одного класса всё равно ничего не добавляют.
    """
    text_col = label_col = None
    spam: list[str] = []
    ok: list[str] = []
    for row in rows:
        text_col = text_col or pick(row, TEXT_COLS, args.text_col)
        label_col = label_col or pick(row, LABEL_COLS, args.label_col)
        if not text_col:
            sys.exit(f"не нашёл колонку с текстом, есть: {list(row)}")
        text = str(row.get(text_col) or "")
        raw = str(row.get(label_col, "")).strip().lower() if label_col else ""
        (spam if raw in SPAM_VALUES else ok).append(text)

    rnd = random.Random(20260901)
    rnd.shuffle(spam)
    rnd.shuffle(ok)
    if args.limit:
        half = max(args.limit // 2, 1)
        spam, ok = spam[:half], ok[:half]
    # чередуем классы: тогда и наивная выборка «первых N по id» останется
    # сбалансированной, чем бы её потом ни делали
    out: list[tuple[str, str]] = []
    for i in range(max(len(spam), len(ok))):
        if i < len(spam):
            out.append((spam[i], "spam"))
        if i < len(ok):
            out.append((ok[i], "ok"))
    print(f"в файле: спам {len(spam)}, норма {len(ok)}")
    return out


async def main() -> None:
    ap = argparse.ArgumentParser(description="Импорт датасета в стартовый набор")
    ap.add_argument("path", help="файл датасета (.jsonl, .csv, .parquet)")
    ap.add_argument("--text-col", help="колонка с текстом")
    ap.add_argument("--label-col", help="колонка с меткой")
    ap.add_argument("--limit", type=int, default=0,
                    help="взять не больше N строк, поровну спама и нормы")
    ap.add_argument("--clear", action="store_true", help="сначала очистить набор")
    args = ap.parse_args()

    from gremlin import config, db
    if not os.path.exists(config.DB_PATH):
        sys.exit(f"базы нет: {config.DB_PATH}")

    await db.init()
    if args.clear:
        print("очищено:", await db.seed_clear())

    pairs = list(prepare(read_rows(args.path), args))
    added = dupes = skipped = 0
    for text, label in pairs:
        if await db.seed_add(text, label):
            added += 1
        elif len(" ".join(text.split())) < 10:
            skipped += 1
        else:
            dupes += 1
    await db.seed_commit()

    stats = await db.seed_stats()
    print(f"добавлено: {added}, дубли: {dupes}, слишком короткие: {skipped}")
    print(f"в наборе теперь: спам {stats['spam']}, норма {stats['ok']}, "
          f"всего {stats['total']}")
    print("векторы посчитаются сами при первом сравнении")
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
