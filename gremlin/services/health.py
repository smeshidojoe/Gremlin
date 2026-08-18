"""Техническая сводка о процессе и данных — для раздела «Состояние».

Всё считается на месте и без запросов в Telegram: размеры файлов, счётчики из
своей базы, состояние кэшей и юзербота.
"""
import glob
import os
import platform
import shutil

import aiogram

from .. import config, db, runtime, utils
from . import adm_cache, stats_collect


def _size(path: str) -> str:
    try:
        return _human(os.path.getsize(path))
    except OSError:
        return "нет файла"


def _human(size: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ТБ"


def _rss() -> str:
    """Память процесса. В докере читаем /proc, на Windows — как получится."""
    try:
        with open("/proc/self/statm") as f:
            pages = int(f.read().split()[1])
        return _human(pages * os.sysconf("SC_PAGE_SIZE"))
    except Exception:
        try:
            import resource
            return _human(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024)
        except Exception:
            return "—"


def _uptime() -> str:
    up = runtime.uptime_seconds()
    d, rem = divmod(up, 86400)
    h, rem = divmod(rem, 3600)
    m, sec = divmod(rem, 60)
    return (f"{d}д {h}ч {m}м" if d else f"{h}ч {m}м {sec}с")


async def report() -> str:
    """Текст для меню «⚙️ Состояние»."""
    from .. import userbot

    chats = await db.moderated_chats()
    # лог-чаты в списке чатов не показываются, но бот в них сидит — считаем отдельно
    logs = len(await db.all_chats(active_only=True)) - len(chats)
    counts = await db.table_counts()
    backups = sorted(glob.glob(os.path.join(config.BACKUP_DIR, "gremlin-*.sqlite3")))
    last_backup = os.path.basename(backups[-1]) if backups else "нет"

    client = userbot._client_ref
    if not config.USERBOT_ON:
        ub = "выключен настройкой"
    elif client is None:
        ub = "не запущен"
    else:
        ub = "на связи" if client.is_connected() else "отключён"

    tracked = stats_collect.tracked_chat_id()
    tracked_title = "—"
    if tracked:
        ch = await db.get_chat(tracked)
        tracked_title = (ch["title"] if ch else str(tracked))

    try:
        free = _human(shutil.disk_usage(os.path.dirname(config.DB_PATH) or ".").free)
    except OSError:
        free = "—"

    lines = [
        "<b>⚙️ Состояние</b>\n",
        f"⏱ Аптайм: <b>{_uptime()}</b>",
        f"🕒 Местное время: <b>{utils.local_now():%d.%m %H:%M}</b> (UTC+{config.TZ_OFFSET})",
        f"🧠 Память: <b>{_rss()}</b>",
        f"🐍 Python <b>{platform.python_version()}</b> · aiogram <b>{aiogram.__version__}</b>",
        "",
        f"💬 Чатов: <b>{len(chats)}</b>" + (f" (+{logs} лог-чата)" if logs else "")
        + f" · наказаний активно: <b>{counts['punishments_active']}</b>",
        f"🧨 Стоп-слов: <b>{counts['words']}</b> · вайтлист: <b>{counts['whitelist']}</b> · "
        f"триггеров: <b>{counts['triggers']}</b> · счётчиков: <b>{counts['chat_cmds']}</b>",
        f"📜 Событий в журнале: <b>{counts['events']}</b> · известных людей: <b>{counts['users']}</b>",
        "",
        f"🗄 База: <b>{_size(config.DB_PATH)}</b> · копий: <b>{len(backups)}</b> "
        f"(последняя: {last_backup})",
        f"📊 База статистики: <b>{_size(config.STATS_DB)}</b> · профильный чат: "
        f"<b>{utils.esc(tracked_title)}</b>",
        f"📝 Лог-файл: <b>{_size(config.LOG_PATH)}</b> · свободно на диске: <b>{free}</b>",
        "",
        f"👁 Юзербот: <b>{ub}</b>",
        f"⚡ Кэши: админы {len(adm_cache._admins)} · участники {len(adm_cache._members)} · "
        f"ники {len(adm_cache._mentions)} · привязки {len(adm_cache._linked)}",
    ]
    return "\n".join(lines)
