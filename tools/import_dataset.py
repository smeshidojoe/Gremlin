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


async def main() -> None:
    ap = argparse.ArgumentParser(description="Импорт датасета в стартовый набор")
    ap.add_argument("path", help="файл датасета (.jsonl, .csv, .parquet)")
    ap.add_argument("--text-col", help="колонка с текстом")
    ap.add_argument("--label-col", help="колонка с меткой")
    ap.add_argument("--limit", type=int, default=0, help="взять не больше N строк")
    ap.add_argument("--clear", action="store_true", help="сначала очистить набор")
    args = ap.parse_args()

    from gremlin import config, db
    if not os.path.exists(config.DB_PATH):
        sys.exit(f"базы нет: {config.DB_PATH}")

    await db.init()
    if args.clear:
        print("очищено:", await db.seed_clear())

    text_col = label_col = None
    added = dupes = skipped = 0
    for row in read_rows(args.path):
        text_col = pick(row, TEXT_COLS, text_col or args.text_col)
        label_col = pick(row, LABEL_COLS, label_col or args.label_col)
        if not text_col:
            sys.exit(f"не нашёл колонку с текстом, есть: {list(row)}")
        text = str(row.get(text_col) or "")
        raw = str(row.get(label_col, "")).strip().lower() if label_col else ""
        label = "spam" if raw in SPAM_VALUES else "ok"
        if await db.seed_add(text, label):
            added += 1
        elif len(" ".join(text.split())) < 10:
            skipped += 1
        else:
            dupes += 1
        if args.limit and added >= args.limit:
            break
    await db.seed_commit()

    stats = await db.seed_stats()
    print(f"добавлено: {added}, дубли: {dupes}, слишком короткие: {skipped}")
    print(f"в наборе теперь: спам {stats['spam']}, норма {stats['ok']}, "
          f"всего {stats['total']}")
    print("векторы посчитаются сами при первом сравнении")
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
