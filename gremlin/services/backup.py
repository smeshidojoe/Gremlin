"""Автобэкап базы.

VACUUM INTO делает целостную копию прямо на работающей базе — в отличие от
копирования файла, которое ловит его в середине транзакции (ровно так база и
билась). Копии складываются в data/backups и подчищаются по возрасту.
"""
import asyncio
import glob
import logging
import os
import sqlite3

from .. import config, utils

logger = logging.getLogger("gremlin.backup")


def _dump(path: str) -> None:
    """Синхронная часть: делается в отдельном потоке."""
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    try:
        con.execute("VACUUM INTO ?", (path,))
    finally:
        con.close()


def _rotate() -> int:
    """Удалить копии сверх лимита. Возвращает, сколько удалено."""
    files = sorted(glob.glob(os.path.join(config.BACKUP_DIR, "gremlin-*.sqlite3")))
    extra = files[:-config.BACKUP_KEEP] if config.BACKUP_KEEP > 0 else []
    for f in extra:
        try:
            os.remove(f)
        except OSError:
            logger.warning("не удалось удалить старую копию %s", f)
    return len(extra)


async def make() -> str | None:
    """Снять копию за сегодня. Возвращает путь или None при ошибке.

    Копия за сегодня уже есть — переписываем: так в папке ровно один файл на дату.
    """
    os.makedirs(config.BACKUP_DIR, exist_ok=True)
    path = os.path.join(config.BACKUP_DIR, f"gremlin-{utils.day_str()}.sqlite3")
    tmp = path + ".part"
    try:
        for leftover in (tmp, path):
            if os.path.exists(leftover):
                os.remove(leftover)          # VACUUM INTO отказывается писать поверх
        await asyncio.to_thread(_dump, tmp)
        os.replace(tmp, path)
    except Exception:
        logger.warning("бэкап не удался", exc_info=True)
        return None
    removed = await asyncio.to_thread(_rotate)
    size = os.path.getsize(path) // 1024
    logger.info("бэкап: %s (%s КБ), удалено старых: %s", os.path.basename(path), size, removed)
    return path


async def scheduler() -> None:
    """Копия при старте и дальше раз в сутки."""
    if not config.BACKUP_ON:
        logger.info("автобэкап выключен (BACKUP_ON=0)")
        return
    while True:
        await make()
        await asyncio.sleep(24 * 3600)


async def last_info() -> tuple[str, str] | None:
    """(имя файла, размер) последней копии — для меню."""
    files = sorted(glob.glob(os.path.join(config.BACKUP_DIR, "gremlin-*.sqlite3")))
    if not files:
        return None
    last = files[-1]
    return os.path.basename(last), f"{os.path.getsize(last) // 1024} КБ"
