"""Единая SQLite-база: чаты, настройки, вайтлисты, стоп-слова, наказания, события, юзеры."""
import os
import random
import time
from dataclasses import dataclass, fields

import aiosqlite

from . import config, utils

_db: aiosqlite.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chats(
    chat_id   INTEGER PRIMARY KEY,
    title     TEXT,
    username  TEXT,
    owner_id  INTEGER,
    added_at  INTEGER,
    active    INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS settings(
    chat_id         INTEGER PRIMARY KEY,
    inline_on       INTEGER NOT NULL DEFAULT 1,
    inline_punish   TEXT    NOT NULL DEFAULT 'mute',
    inline_mute_min INTEGER NOT NULL DEFAULT 60,
    links_on        INTEGER NOT NULL DEFAULT 1,
    extlinks_on     INTEGER NOT NULL DEFAULT 1,
    links_punish    TEXT    NOT NULL DEFAULT 'delete',
    links_mute_min  INTEGER NOT NULL DEFAULT 60,
    mentions_check  INTEGER NOT NULL DEFAULT 0,
    anon_on         INTEGER NOT NULL DEFAULT 1,
    forwards_on     INTEGER NOT NULL DEFAULT 0,
    words_on        INTEGER NOT NULL DEFAULT 0,
    words_punish    TEXT    NOT NULL DEFAULT 'ban',
    words_mute_min  INTEGER NOT NULL DEFAULT 1440,
    words_guests    INTEGER NOT NULL DEFAULT 1,
    flood_on        INTEGER NOT NULL DEFAULT 0,
    flood_msgs      INTEGER NOT NULL DEFAULT 5,
    flood_window    INTEGER NOT NULL DEFAULT 10,
    flood_mute_min  INTEGER NOT NULL DEFAULT 10,
    captcha_on      INTEGER NOT NULL DEFAULT 0,
    captcha_timeout INTEGER NOT NULL DEFAULT 120,
    watch_on        INTEGER NOT NULL DEFAULT 0,
    watch_bots      INTEGER NOT NULL DEFAULT 1,
    watch_suspect   INTEGER NOT NULL DEFAULT 40,
    watch_ban       INTEGER NOT NULL DEFAULT 80,
    welcome_on      INTEGER NOT NULL DEFAULT 0,
    welcome_text    TEXT,
    media_on        INTEGER NOT NULL DEFAULT 0,
    media_mask      INTEGER NOT NULL DEFAULT 0,
    trig_on         INTEGER NOT NULL DEFAULT 0,
    cmds_on         INTEGER NOT NULL DEFAULT 0,
    cmds_guest_cd   INTEGER NOT NULL DEFAULT 3600,
    digest_to       INTEGER NOT NULL DEFAULT 0,
    service_join    INTEGER NOT NULL DEFAULT 0,
    service_leave   INTEGER NOT NULL DEFAULT 0,
    service_other   INTEGER NOT NULL DEFAULT 0,
    cards_on        INTEGER NOT NULL DEFAULT 1,
    card_mask       INTEGER NOT NULL DEFAULT 4095,
    log_chat_id     INTEGER
);
CREATE TABLE IF NOT EXISTS triggers(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    phrase     TEXT NOT NULL,
    text       TEXT,            -- текстовый ответ / подпись к медиа
    file_path  TEXT,            -- медиа-ответ: файл на диске (config.TRIG_DIR)
    media_type TEXT,            -- photo|video|animation|sticker|voice|video_note|document|audio
    cooldown   INTEGER NOT NULL DEFAULT 30
);
CREATE TABLE IF NOT EXISTS chat_cmds(
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id  INTEGER NOT NULL,
    cmd      TEXT NOT NULL,            -- с префиксом, в нижнем регистре: !черви
    template TEXT NOT NULL,            -- заготовка без [N] — счётчик дописывается сам
    cooldown INTEGER NOT NULL DEFAULT 30,
    count    INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cmds_uniq ON chat_cmds(chat_id, cmd);
CREATE TABLE IF NOT EXISTS answers(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner      TEXT NOT NULL,            -- 'trig' | 'cmd'
    owner_id   INTEGER NOT NULL,
    text       TEXT,                     -- текст ответа / подпись к медиа
    file_path  TEXT,                     -- медиа-вариант: файл в config.TRIG_DIR
    media_type TEXT,
    last_used  INTEGER NOT NULL DEFAULT 0   -- когда выпадал в последний раз
);
CREATE INDEX IF NOT EXISTS idx_answers_owner ON answers(owner, owner_id);
CREATE TABLE IF NOT EXISTS msg_stats(
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    day     INTEGER NOT NULL,   -- номер местных суток (utils.day_num)
    cnt     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, user_id, day)
);
CREATE TABLE IF NOT EXISTS access(
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER,
    username TEXT,
    added    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS watch_profiles(
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    sig     TEXT NOT NULL,          -- подпись профиля (имя|фамилия|username)
    flagged INTEGER NOT NULL DEFAULT 0,  -- карточка «подозрительный» уже отправлена
    PRIMARY KEY (chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS whitelist(
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id  INTEGER NOT NULL,
    user_id  INTEGER,          -- id юзера ИЛИ канала
    username TEXT,
    title    TEXT,             -- подпись для каналов: у них имени в users нет
    scope    TEXT NOT NULL DEFAULT 'all'
);
CREATE TABLE IF NOT EXISTS link_wl(
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id   INTEGER NOT NULL,
    target_id INTEGER,          -- id чата/канала, если удалось определить
    username  TEXT,             -- без @, в нижнем регистре
    title     TEXT
);
CREATE TABLE IF NOT EXISTS inline_wl(
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id  INTEGER NOT NULL,
    bot_id   INTEGER,           -- если удалось определить
    username TEXT NOT NULL      -- без @, в нижнем регистре
);
CREATE TABLE IF NOT EXISTS words(
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    word    TEXT NOT NULL,
    mode    TEXT NOT NULL DEFAULT 'strict'
);
CREATE TABLE IF NOT EXISTS punishments(
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id  INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    username TEXT,
    name     TEXT,
    kind     TEXT NOT NULL,          -- mute | ban | banchan
    reason   TEXT,
    until_ts INTEGER,                -- NULL = навсегда
    by_id    INTEGER,                -- кто наказал (NULL = бот сам)
    created  INTEGER NOT NULL,
    active   INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS events(
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    ts      INTEGER NOT NULL,
    kind    TEXT NOT NULL,
    text    TEXT
);
CREATE TABLE IF NOT EXISTS users(
    user_id    INTEGER PRIMARY KEY,
    username   TEXT,
    first_name TEXT,
    first_seen INTEGER,
    last_seen  INTEGER,
    banned     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS kv(
    k TEXT PRIMARY KEY,
    v TEXT
);
CREATE INDEX IF NOT EXISTS idx_wl_chat ON whitelist(chat_id);
CREATE INDEX IF NOT EXISTS idx_words_chat ON words(chat_id);
CREATE INDEX IF NOT EXISTS idx_pun_chat ON punishments(chat_id, active);
CREATE INDEX IF NOT EXISTS idx_events_chat ON events(chat_id, ts);
"""


@dataclass
class Settings:
    chat_id: int = 0
    inline_on: int = 1
    inline_punish: str = "mute"
    inline_mute_min: int = 60
    links_on: int = 1
    extlinks_on: int = 1
    links_punish: str = "delete"
    links_mute_min: int = 60
    mentions_check: int = 0
    anon_on: int = 1
    forwards_on: int = 0
    words_on: int = 0
    words_punish: str = "ban"
    words_mute_min: int = 1440
    words_guests: int = 1
    flood_on: int = 0
    flood_msgs: int = 5
    flood_window: int = 10
    flood_mute_min: int = 10
    captcha_on: int = 0
    captcha_timeout: int = 120
    watch_on: int = 0
    watch_bots: int = 1
    watch_suspect: int = 40
    watch_ban: int = 80
    welcome_on: int = 0
    welcome_text: str | None = None
    media_on: int = 0
    media_mask: int = 0
    trig_on: int = 0
    cmds_on: int = 0
    cmds_guest_cd: int = 3600
    digest_to: int = 0
    service_join: int = 0
    service_leave: int = 0
    service_other: int = 0
    cards_on: int = 1
    card_mask: int = 4095
    log_chat_id: int | None = None


_SETTINGS_FIELDS = {f.name for f in fields(Settings)} - {"chat_id"}


# колонки settings, которые могли отсутствовать в старых базах (имя -> DDL-хвост)
_SETTINGS_MIGRATIONS = {
    "captcha_on": "INTEGER NOT NULL DEFAULT 0",
    "captcha_timeout": "INTEGER NOT NULL DEFAULT 120",
    "forwards_on": "INTEGER NOT NULL DEFAULT 0",
    "mentions_check": "INTEGER NOT NULL DEFAULT 0",
    "watch_on": "INTEGER NOT NULL DEFAULT 0",
    "watch_bots": "INTEGER NOT NULL DEFAULT 1",
    "watch_suspect": "INTEGER NOT NULL DEFAULT 40",
    "watch_ban": "INTEGER NOT NULL DEFAULT 80",
    "welcome_on": "INTEGER NOT NULL DEFAULT 0",
    "welcome_text": "TEXT",
    "media_on": "INTEGER NOT NULL DEFAULT 0",
    "media_mask": "INTEGER NOT NULL DEFAULT 0",
    "trig_on": "INTEGER NOT NULL DEFAULT 0",
    "cmds_on": "INTEGER NOT NULL DEFAULT 0",
    "digest_to": "INTEGER NOT NULL DEFAULT 0",
    "cmds_guest_cd": "INTEGER NOT NULL DEFAULT 3600",
    "words_guests": "INTEGER NOT NULL DEFAULT 1",
    "extlinks_on": "INTEGER NOT NULL DEFAULT 1",
    "service_join": "INTEGER NOT NULL DEFAULT 0",
    "service_leave": "INTEGER NOT NULL DEFAULT 0",
    "service_other": "INTEGER NOT NULL DEFAULT 0",
}

# разовые включения новых card-битов в существующих card_mask: kv-флаг -> бит
_MASK_MIGRATIONS = {"mig_watch_bit": 1024, "mig_report_bit": 2048}


# колонки других таблиц, появившиеся позже: таблица -> {колонка: DDL}
_TABLE_MIGRATIONS = {
    "triggers": {"file_path": "TEXT", "media_type": "TEXT",
                 "cooldown": "INTEGER NOT NULL DEFAULT 30"},
    "whitelist": {"title": "TEXT"},
    "answers": {"last_used": "INTEGER NOT NULL DEFAULT 0"},
}


async def _migrate() -> None:
    cur = await _db.execute("PRAGMA table_info(settings)")
    cols = {r["name"] for r in await cur.fetchall()}
    for name, ddl in _SETTINGS_MIGRATIONS.items():
        if name not in cols:
            await _db.execute(f"ALTER TABLE settings ADD COLUMN {name} {ddl}")
    # старый общий тумблер service_on разъехался на вход/выход
    if "service_on" in cols and "service_join" not in cols:
        await _db.execute(
            "UPDATE settings SET service_join = service_on, service_leave = service_on"
        )
    for table, add in _TABLE_MIGRATIONS.items():
        cur = await _db.execute(f"PRAGMA table_info({table})")
        have = {r["name"] for r in await cur.fetchall()}
        for name, ddl in add.items():
            if name not in have:
                await _db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    for flag, bit in _MASK_MIGRATIONS.items():
        cur = await _db.execute("SELECT v FROM kv WHERE k = ?", (flag,))
        if await cur.fetchone() is None:
            await _db.execute(f"UPDATE settings SET card_mask = card_mask | {bit}")
            await _db.execute("INSERT INTO kv (k, v) VALUES (?, '1')", (flag,))
    # таблицы удалённых функций: варны, список для жалоб, отдельный вайтлист анонимов
    # (его содержимое давно переехало в общий whitelist)
    cur = await _db.execute("SELECT v FROM kv WHERE k = 'mig_drop_dead'")
    if await cur.fetchone() is None:
        for dead in ("warns", "report_wl", "anon_wl"):
            await _db.execute(f"DROP TABLE IF EXISTS {dead}")
        await _db.execute("INSERT INTO kv (k, v) VALUES ('mig_drop_dead', '1')")

    # ответы триггеров и счётчиков переехали в answers: теперь их может быть несколько
    cur = await _db.execute("SELECT v FROM kv WHERE k = 'mig_answers'")
    if await cur.fetchone() is None:
        await _db.execute(
            """INSERT INTO answers (owner, owner_id, text, file_path, media_type)
               SELECT 'trig', id, text, file_path, media_type FROM triggers"""
        )
        await _db.execute(
            """INSERT INTO answers (owner, owner_id, text, file_path, media_type)
               SELECT 'cmd', id, template, NULL, NULL FROM chat_cmds"""
        )
        await _db.execute("INSERT INTO kv (k, v) VALUES ('mig_answers', '1')")

    # внешние ссылки стали включёнными по умолчанию — доводим существующие чаты
    cur = await _db.execute("SELECT v FROM kv WHERE k = 'mig_extlinks_on'")
    if await cur.fetchone() is None:
        await _db.execute("UPDATE settings SET extlinks_on = 1")
        await _db.execute("INSERT INTO kv (k, v) VALUES ('mig_extlinks_on', '1')")
    await _db.commit()


async def init() -> None:
    global _db
    _db = await aiosqlite.connect(config.DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.execute("PRAGMA synchronous=NORMAL")
    await _db.executescript(_SCHEMA)
    await _db.commit()
    await _migrate()


async def close() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


def _now() -> int:
    return int(time.time())


# ---------- чаты ----------

async def upsert_chat(chat_id: int, title: str | None, username: str | None,
                      owner_id: int | None) -> None:
    await _db.execute(
        """INSERT INTO chats (chat_id, title, username, owner_id, added_at, active)
           VALUES (?, ?, ?, ?, ?, 1)
           ON CONFLICT(chat_id) DO UPDATE SET
             title = excluded.title,
             username = excluded.username,
             owner_id = COALESCE(chats.owner_id, excluded.owner_id),
             active = 1""",
        (chat_id, title, username, owner_id, _now()),
    )
    await _db.execute(
        "INSERT OR IGNORE INTO settings (chat_id) VALUES (?)", (chat_id,)
    )
    await _db.commit()


async def update_chat_title(chat_id: int, title: str | None, username: str | None) -> None:
    await _db.execute(
        "UPDATE chats SET title = ?, username = ? WHERE chat_id = ?",
        (title, username, chat_id),
    )
    await _db.commit()


async def set_chat_active(chat_id: int, active: bool) -> None:
    await _db.execute("UPDATE chats SET active = ? WHERE chat_id = ?", (int(active), chat_id))
    await _db.commit()


async def set_owner(chat_id: int, owner_id: int) -> None:
    await _db.execute("UPDATE chats SET owner_id = ? WHERE chat_id = ?", (owner_id, chat_id))
    await _db.commit()


async def get_chat(chat_id: int) -> aiosqlite.Row | None:
    cur = await _db.execute("SELECT * FROM chats WHERE chat_id = ?", (chat_id,))
    return await cur.fetchone()


async def chats_of(owner_id: int) -> list[aiosqlite.Row]:
    cur = await _db.execute(
        "SELECT * FROM chats WHERE owner_id = ? AND active = 1 ORDER BY added_at", (owner_id,)
    )
    return await cur.fetchall()


async def all_chats(active_only: bool = False) -> list[aiosqlite.Row]:
    q = "SELECT * FROM chats" + (" WHERE active = 1" if active_only else "") + " ORDER BY added_at"
    cur = await _db.execute(q)
    return await cur.fetchall()


async def moderated_chats() -> list[aiosqlite.Row]:
    """Рабочие чаты для меню: без тех, что служат лог-чатом для другого чата."""
    cur = await _db.execute(
        """SELECT * FROM chats WHERE active = 1 AND chat_id NOT IN (
               SELECT log_chat_id FROM settings
               WHERE log_chat_id IS NOT NULL AND log_chat_id != chat_id
           ) ORDER BY added_at"""
    )
    return await cur.fetchall()


# ---------- настройки ----------

async def get_settings(chat_id: int) -> Settings:
    cur = await _db.execute("SELECT * FROM settings WHERE chat_id = ?", (chat_id,))
    row = await cur.fetchone()
    if row is None:
        await _db.execute("INSERT OR IGNORE INTO settings (chat_id) VALUES (?)", (chat_id,))
        await _db.commit()
        return Settings(chat_id=chat_id)
    # берём только известные поля: в старых базах остаются осиротевшие колонки
    # (sqlite не умеет DROP COLUMN без пересборки таблицы)
    known = {k: row[k] for k in row.keys() if k in _SETTINGS_FIELDS}
    return Settings(chat_id=chat_id, **known)


async def set_setting(chat_id: int, field: str, value) -> None:
    if field not in _SETTINGS_FIELDS:
        raise ValueError(f"unknown settings field: {field}")
    await _db.execute(f"UPDATE settings SET {field} = ? WHERE chat_id = ?", (value, chat_id))
    await _db.commit()


# ---------- вайтлист людей ----------

async def wl_list(chat_id: int) -> list[aiosqlite.Row]:
    cur = await _db.execute("SELECT * FROM whitelist WHERE chat_id = ? ORDER BY id", (chat_id,))
    return await cur.fetchall()


def _wl_key_sql(user_id: int | None, username: str | None) -> tuple[str, tuple]:
    """Условие «эта же запись вайтлиста»: по id (в обоих форматах) либо по нику."""
    if user_id is not None:
        ids = id_variants(user_id)
        return f"user_id IN ({','.join('?' * len(ids))})", ids
    return "user_id IS NULL AND username = ?", ((username or "").lower().lstrip("@"),)


async def wl_entries(chat_id: int) -> list[dict]:
    """Вайтлист по объектам, а не по строкам: у одного юзера может быть несколько
    уровней игнора, в списке он должен быть одной записью."""
    rows = await wl_list(chat_id)
    out: dict[tuple, dict] = {}
    for r in rows:
        key = (r["user_id"], None) if r["user_id"] is not None else (None, r["username"])
        item = out.setdefault(key, {
            "row_id": r["id"], "user_id": r["user_id"], "username": r["username"],
            "title": r["title"], "scopes": set(),
        })
        item["scopes"].add(r["scope"])
        item["title"] = item["title"] or r["title"]
    return list(out.values())


async def wl_entry(chat_id: int, row_id: int) -> dict | None:
    """Запись вайтлиста со всеми её уровнями — по id любой из её строк."""
    cur = await _db.execute(
        "SELECT * FROM whitelist WHERE id = ? AND chat_id = ?", (row_id, chat_id)
    )
    row = await cur.fetchone()
    if row is None:
        return None
    for e in await wl_entries(chat_id):
        same_id = row["user_id"] is not None and e["user_id"] == row["user_id"]
        same_name = row["user_id"] is None and e["username"] == row["username"]
        if same_id or same_name:
            return e
    return None


async def wl_entry_by_key(chat_id: int, user_id: int | None,
                          username: str | None) -> dict | None:
    """Запись по самому объекту. После перезаписи уровней id строк меняются,
    поэтому искать по ним нельзя."""
    for e in await wl_entries(chat_id):
        if user_id is not None and e["user_id"] == user_id:
            return e
        if user_id is None and e["user_id"] is None and e["username"] == username:
            return e
    return None


async def wl_set_scopes(chat_id: int, user_id: int | None, username: str | None,
                        title: str | None, scopes: set[str]) -> None:
    """Переписать уровни игнора для одного объекта. Пустой набор — убрать из списка."""
    cond, args = _wl_key_sql(user_id, username)
    await _db.execute(f"DELETE FROM whitelist WHERE chat_id = ? AND ({cond})", (chat_id, *args))
    uname = (username or None) and username.lower().lstrip("@")
    for scope in scopes:
        await _db.execute(
            "INSERT INTO whitelist (chat_id, user_id, username, title, scope) VALUES (?,?,?,?,?)",
            (chat_id, user_id, uname, title, scope),
        )
    await _db.commit()


async def wl_scopes_for(chat_id: int, user_id: int, username: str | None) -> set[str]:
    """Все scope, под которые попадает юзер (или канал) в этом чате."""
    uname = (username or "").lower()
    ids = id_variants(user_id)
    ph = ",".join("?" * len(ids))
    cur = await _db.execute(
        f"""SELECT scope FROM whitelist WHERE chat_id = ?
            AND (user_id IN ({ph}) OR (username IS NOT NULL AND username = ?))""",
        (chat_id, *ids, uname),
    )
    return {r["scope"] for r in await cur.fetchall()}


# ---------- разрешённые для ссылок чаты и каналы ----------

async def link_wl_add(chat_id: int, target_id: int | None, username: str | None,
                      title: str | None = None) -> None:
    await _db.execute(
        "INSERT INTO link_wl (chat_id, target_id, username, title) VALUES (?, ?, ?, ?)",
        (chat_id, target_id, (username or None) and username.lower().lstrip("@"), title),
    )
    await _db.commit()


async def link_wl_remove(row_id: int) -> None:
    await _db.execute("DELETE FROM link_wl WHERE id = ?", (row_id,))
    await _db.commit()


async def link_wl_list(chat_id: int) -> list[aiosqlite.Row]:
    cur = await _db.execute("SELECT * FROM link_wl WHERE chat_id = ? ORDER BY id", (chat_id,))
    return await cur.fetchall()


# ---------- разрешённые инлайн-боты ----------

async def inline_wl_add(chat_id: int, username: str, bot_id: int | None = None) -> None:
    await _db.execute(
        "INSERT INTO inline_wl (chat_id, bot_id, username) VALUES (?, ?, ?)",
        (chat_id, bot_id, username.lower().lstrip("@")),
    )
    await _db.commit()


async def inline_wl_remove(row_id: int) -> None:
    await _db.execute("DELETE FROM inline_wl WHERE id = ?", (row_id,))
    await _db.commit()


async def inline_wl_list(chat_id: int) -> list[aiosqlite.Row]:
    cur = await _db.execute(
        "SELECT * FROM inline_wl WHERE chat_id = ? ORDER BY username", (chat_id,)
    )
    return await cur.fetchall()


async def inline_wl_allowed(chat_id: int, username: str | None, bot_id: int | None) -> bool:
    """Этому инлайн-боту в этом чате можно."""
    cur = await _db.execute(
        """SELECT 1 FROM inline_wl WHERE chat_id = ?
           AND (username = ? OR (bot_id IS NOT NULL AND bot_id = ?))""",
        (chat_id, (username or "").lower().lstrip("@"), bot_id),
    )
    return await cur.fetchone() is not None


# ---------- стоп-слова ----------

async def words_add(chat_id: int, word: str, mode: str) -> bool:
    """Добавить слово. False — такое уже есть (режим при этом обновляем).

    Дубли раньше просто копились и занимали место в списке.
    """
    w = word.lower()
    cur = await _db.execute(
        "SELECT id, mode FROM words WHERE chat_id = ? AND word = ?", (chat_id, w)
    )
    row = await cur.fetchone()
    if row is not None:
        if row["mode"] != mode:
            await _db.execute("UPDATE words SET mode = ? WHERE id = ?", (mode, row["id"]))
            await _db.commit()
        return False
    await _db.execute(
        "INSERT INTO words (chat_id, word, mode) VALUES (?, ?, ?)", (chat_id, w, mode)
    )
    await _db.commit()
    return True


async def words_clear(chat_id: int) -> int:
    """Стереть весь список. Возвращает, сколько удалено."""
    cur = await _db.execute("DELETE FROM words WHERE chat_id = ?", (chat_id,))
    await _db.commit()
    return cur.rowcount or 0


async def words_remove(row_id: int) -> None:
    await _db.execute("DELETE FROM words WHERE id = ?", (row_id,))
    await _db.commit()


async def words_list(chat_id: int) -> list[aiosqlite.Row]:
    """Список стоп-слов по алфавиту — так его проще просматривать глазами."""
    cur = await _db.execute(
        "SELECT * FROM words WHERE chat_id = ? ORDER BY word", (chat_id,)
    )
    return await cur.fetchall()


def id_variants(cid: int) -> tuple[int, ...]:
    """Один и тот же канал записывают по-разному: -1001389201023 и 1389201023.
    Сверяем оба варианта, чтобы вайтлист не молчал из-за формата."""
    s = str(cid)
    out = {cid}
    if s.startswith("-100"):
        out.add(int(s[4:]))
    elif cid > 0:
        out.add(int(f"-100{s}"))
    return tuple(out)


# ---------- наказания ----------

async def add_punishment(chat_id: int, user_id: int, username: str | None, name: str | None,
                         kind: str, reason: str | None, until: int | None,
                         by_id: int | None) -> int:
    # прошлые активные наказания того же юзера в этом чате гасим
    await _db.execute(
        "UPDATE punishments SET active = 0 WHERE chat_id = ? AND user_id = ? AND active = 1",
        (chat_id, user_id),
    )
    cur = await _db.execute(
        """INSERT INTO punishments (chat_id, user_id, username, name, kind, reason,
                                    until_ts, by_id, created, active)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
        (chat_id, user_id, username, name, kind, reason, until, by_id, _now()),
    )
    await _db.commit()
    return cur.lastrowid


async def get_punishment(pid: int) -> aiosqlite.Row | None:
    cur = await _db.execute("SELECT * FROM punishments WHERE id = ?", (pid,))
    return await cur.fetchone()


async def deactivate_punishment(pid: int) -> None:
    await _db.execute("UPDATE punishments SET active = 0 WHERE id = ?", (pid,))
    await _db.commit()


async def deactivate_user_punishments(chat_id: int, user_id: int) -> None:
    await _db.execute(
        "UPDATE punishments SET active = 0 WHERE chat_id = ? AND user_id = ? AND active = 1",
        (chat_id, user_id),
    )
    await _db.commit()


async def active_punishments(chat_id: int, limit: int = 10, offset: int = 0) -> list[aiosqlite.Row]:
    cur = await _db.execute(
        """SELECT * FROM punishments
           WHERE chat_id = ? AND active = 1 AND (until_ts IS NULL OR until_ts > ?)
           ORDER BY created DESC LIMIT ? OFFSET ?""",
        (chat_id, _now(), limit, offset),
    )
    return await cur.fetchall()


async def active_punishment_of(chat_id: int, user_id: int, kind: str) -> aiosqlite.Row | None:
    """Активное наказание нужного вида. id канала сверяем в обоих форматах."""
    ids = id_variants(user_id)
    marks = ",".join("?" * len(ids))
    cur = await _db.execute(
        f"""SELECT * FROM punishments
            WHERE chat_id = ? AND kind = ? AND active = 1 AND user_id IN ({marks})
            ORDER BY created DESC LIMIT 1""",
        (chat_id, kind, *ids),
    )
    return await cur.fetchone()


async def active_punishments_count(chat_id: int) -> int:
    cur = await _db.execute(
        """SELECT COUNT(*) AS c FROM punishments
           WHERE chat_id = ? AND active = 1 AND (until_ts IS NULL OR until_ts > ?)""",
        (chat_id, _now()),
    )
    return (await cur.fetchone())["c"]


# ---------- события ----------

async def add_event(chat_id: int | None, kind: str, text: str) -> None:
    await _db.execute(
        "INSERT INTO events (chat_id, ts, kind, text) VALUES (?, ?, ?, ?)",
        (chat_id, _now(), kind, text),
    )
    await _db.commit()


async def recent_events(limit: int = 20, chat_id: int | None = None) -> list[aiosqlite.Row]:
    if chat_id is None:
        cur = await _db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
    else:
        cur = await _db.execute(
            "SELECT * FROM events WHERE chat_id = ? ORDER BY id DESC LIMIT ?", (chat_id, limit)
        )
    return await cur.fetchall()


# ---------- юзеры бота (для админ-статистики) ----------

async def track_user(user_id: int, username: str | None, first_name: str | None) -> None:
    now = _now()
    await _db.execute(
        """INSERT INTO users (user_id, username, first_name, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
             username = excluded.username,
             first_name = excluded.first_name,
             last_seen = excluded.last_seen""",
        (user_id, username, first_name, now, now),
    )
    await _db.commit()


async def user_label(user_id: int | None, username: str | None = None,
                     fallback: str | None = None) -> str:
    """«Имя @ник» из того, что знаем; иначе @ник, иначе id — чтобы в списках
    не висели голые числа."""
    if user_id:
        row = await get_user(user_id)
        if row:
            name = row["first_name"] or ""
            uname = f"@{row['username']}" if row["username"] else ""
            label = " ".join(x for x in (name, uname) if x)
            if label:
                return label
    if username:
        return f"@{username.lstrip('@')}"
    return fallback or str(user_id or "—")


async def user_by_username(username: str) -> aiosqlite.Row | None:
    cur = await _db.execute(
        "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username.lstrip("@"),)
    )
    return await cur.fetchone()


async def get_user(user_id: int) -> aiosqlite.Row | None:
    cur = await _db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    return await cur.fetchone()


async def all_users() -> list[aiosqlite.Row]:
    cur = await _db.execute("SELECT * FROM users ORDER BY first_seen")
    return await cur.fetchall()


async def set_bot_ban(user_id: int, banned: bool) -> None:
    await _db.execute("UPDATE users SET banned = ? WHERE user_id = ?", (int(banned), user_id))
    await _db.commit()


async def is_bot_banned(user_id: int) -> bool:
    cur = await _db.execute("SELECT banned FROM users WHERE user_id = ?", (user_id,))
    row = await cur.fetchone()
    return bool(row and row["banned"])


# ---------- триггеры ----------

# ---------- варианты ответов (общие для триггеров и счётчиков) ----------
#
# Ответов у одного объекта может быть несколько — бот берёт случайный. Колонки
# text/file_path/media_type в triggers и template в chat_cmds остались от старой
# схемы и больше не читаются: источник правды — answers.

async def ans_add(owner: str, owner_id: int, text: str | None,
                  file_path: str | None = None, media_type: str | None = None) -> int:
    cur = await _db.execute(
        "INSERT INTO answers (owner, owner_id, text, file_path, media_type) VALUES (?,?,?,?,?)",
        (owner, owner_id, text, file_path, media_type),
    )
    await _db.commit()
    return cur.lastrowid


async def ans_list(owner: str, owner_id: int) -> list[aiosqlite.Row]:
    cur = await _db.execute(
        "SELECT * FROM answers WHERE owner = ? AND owner_id = ? ORDER BY id", (owner, owner_id)
    )
    return await cur.fetchall()


async def ans_pick(owner: str, owner_id: int) -> aiosqlite.Row | None:
    """Случайный вариант ответа — из тех, что давно не выпадали.

    Чистый RANDOM повторяется: из 48 вариантов один и тот же легко выпадает
    дважды подряд. Поэтому берём половину списка с самыми старыми показами
    (ещё не показанные имеют last_used = 0, то есть идут первыми) и случайно
    выбираем уже среди них. Показанное вернётся в игру, когда остальные
    догонят его по свежести.
    """
    cur = await _db.execute(
        "SELECT COUNT(*) AS n FROM answers WHERE owner = ? AND owner_id = ?",
        (owner, owner_id),
    )
    total = (await cur.fetchone())["n"]
    if not total:
        return None
    pool = max(1, total // 2)
    cur = await _db.execute(
        """SELECT * FROM answers WHERE owner = ? AND owner_id = ?
           ORDER BY last_used ASC, RANDOM() LIMIT ?""",
        (owner, owner_id, pool),
    )
    rows = await cur.fetchall()
    row = random.choice(rows)
    # метка в миллисекундах: в секундах несколько вызовов подряд получали
    # одинаковую свежесть, и сортировка вырождалась в чистый рандом
    await _db.execute(
        "UPDATE answers SET last_used = ? WHERE id = ?",
        (int(time.time() * 1000), row["id"]),
    )
    await _db.commit()
    return row


async def ans_stats(owner: str, owner_ids: list[int]) -> dict[int, tuple[int, int]]:
    """{owner_id: (сколько вариантов, из них с медиа)} — одним запросом на страницу."""
    if not owner_ids:
        return {}
    marks = ",".join("?" * len(owner_ids))
    cur = await _db.execute(
        f"""SELECT owner_id, COUNT(*) AS n,
                   SUM(CASE WHEN file_path IS NOT NULL THEN 1 ELSE 0 END) AS media
            FROM answers WHERE owner = ? AND owner_id IN ({marks})
            GROUP BY owner_id""",
        (owner, *owner_ids),
    )
    return {r["owner_id"]: (r["n"], r["media"] or 0) for r in await cur.fetchall()}


async def ans_get(row_id: int) -> aiosqlite.Row | None:
    cur = await _db.execute("SELECT * FROM answers WHERE id = ?", (row_id,))
    return await cur.fetchone()


async def ans_remove(row_id: int) -> None:
    """Удаляем вариант вместе с его файлом, чтобы папка не копила мусор."""
    row = await ans_get(row_id)
    if row and row["file_path"]:
        try:
            os.remove(row["file_path"])
        except OSError:
            pass
    await _db.execute("DELETE FROM answers WHERE id = ?", (row_id,))
    await _db.commit()


async def ans_clear(owner: str, owner_id: int) -> None:
    for r in await ans_list(owner, owner_id):
        await ans_remove(r["id"])


async def trig_add(chat_id: int, phrase: str, text: str | None,
                   file_path: str | None = None, media_type: str | None = None) -> int:
    cur = await _db.execute(
        "INSERT INTO triggers (chat_id, phrase, text, file_path, media_type) VALUES (?, ?, ?, ?, ?)",
        (chat_id, phrase.lower(), text, file_path, media_type),
    )
    await _db.commit()
    rid = cur.lastrowid
    await ans_add("trig", rid, text, file_path, media_type)
    return rid


async def trig_remove(row_id: int) -> None:
    """Удаляем триггер вместе со всеми его вариантами и их файлами."""
    await ans_clear("trig", row_id)
    await _db.execute("DELETE FROM triggers WHERE id = ?", (row_id,))
    await _db.commit()


async def trig_get(row_id: int) -> aiosqlite.Row | None:
    cur = await _db.execute("SELECT * FROM triggers WHERE id = ?", (row_id,))
    return await cur.fetchone()


async def trig_set(row_id: int, field: str, value) -> None:
    if field not in ("phrase", "text", "cooldown", "file_path", "media_type"):
        raise ValueError(f"unknown triggers field: {field}")
    await _db.execute(f"UPDATE triggers SET {field} = ? WHERE id = ?", (value, row_id))
    await _db.commit()


async def trig_list(chat_id: int) -> list[aiosqlite.Row]:
    cur = await _db.execute(
        "SELECT * FROM triggers WHERE chat_id = ? ORDER BY phrase", (chat_id,)
    )
    return await cur.fetchall()


# ---------- команды чата (развлекательные, со счётчиком) ----------

async def cmd_add(chat_id: int, cmd: str, template: str, cooldown: int) -> bool:
    """Создать команду. False — если такая в этом чате уже есть."""
    try:
        cur = await _db.execute(
            "INSERT INTO chat_cmds (chat_id, cmd, template, cooldown) VALUES (?, ?, ?, ?)",
            (chat_id, cmd.lower(), template, cooldown),
        )
    except aiosqlite.IntegrityError:
        return False
    await _db.commit()
    await ans_add("cmd", cur.lastrowid, template)
    return True


async def cmd_remove(row_id: int) -> None:
    await ans_clear("cmd", row_id)
    await _db.execute("DELETE FROM chat_cmds WHERE id = ?", (row_id,))
    await _db.commit()


async def cmd_list(chat_id: int) -> list[aiosqlite.Row]:
    cur = await _db.execute(
        "SELECT * FROM chat_cmds WHERE chat_id = ? ORDER BY cmd", (chat_id,)
    )
    return await cur.fetchall()


async def cmd_get(row_id: int) -> aiosqlite.Row | None:
    cur = await _db.execute("SELECT * FROM chat_cmds WHERE id = ?", (row_id,))
    return await cur.fetchone()


async def cmd_find(chat_id: int, cmd: str) -> aiosqlite.Row | None:
    cur = await _db.execute(
        "SELECT * FROM chat_cmds WHERE chat_id = ? AND cmd = ?", (chat_id, cmd.lower())
    )
    return await cur.fetchone()


async def cmd_set(row_id: int, field: str, value) -> None:
    if field not in ("template", "cooldown", "count"):
        raise ValueError(f"unknown chat_cmds field: {field}")
    await _db.execute(f"UPDATE chat_cmds SET {field} = ? WHERE id = ?", (value, row_id))
    await _db.commit()


async def cmd_bump(row_id: int) -> int:
    """Увеличить счётчик вызовов и вернуть новое значение."""
    await _db.execute("UPDATE chat_cmds SET count = count + 1 WHERE id = ?", (row_id,))
    await _db.commit()
    cur = await _db.execute("SELECT count FROM chat_cmds WHERE id = ?", (row_id,))
    row = await cur.fetchone()
    return row["count"] if row else 0


# ---------- статистика сообщений ----------

async def msg_inc(chat_id: int, user_id: int, username: str | None = None,
                  first_name: str | None = None) -> None:
    """Счётчик сообщений + запоминаем имя/ник, иначе в топе будут голые id."""
    now = _now()
    day = utils.day_num(now)
    await _db.execute(
        """INSERT INTO msg_stats (chat_id, user_id, day, cnt) VALUES (?, ?, ?, 1)
           ON CONFLICT(chat_id, user_id, day) DO UPDATE SET cnt = cnt + 1""",
        (chat_id, user_id, day),
    )
    await _db.execute(
        """INSERT INTO users (user_id, username, first_name, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
             username = COALESCE(excluded.username, users.username),
             first_name = COALESCE(excluded.first_name, users.first_name)""",
        (user_id, username, first_name, now, now),
    )
    await _db.commit()


async def chat_stats(chat_id: int) -> dict:
    day = utils.day_num()

    async def one(q: str, args) -> int:
        cur = await _db.execute(q, args)
        return (await cur.fetchone())[0] or 0

    total = await one("SELECT SUM(cnt) FROM msg_stats WHERE chat_id = ?", (chat_id,))
    d1 = await one("SELECT SUM(cnt) FROM msg_stats WHERE chat_id = ? AND day >= ?", (chat_id, day))
    d7 = await one("SELECT SUM(cnt) FROM msg_stats WHERE chat_id = ? AND day >= ?", (chat_id, day - 6))
    cur = await _db.execute(
        """SELECT user_id, SUM(cnt) AS c FROM msg_stats
           WHERE chat_id = ? AND day >= ? GROUP BY user_id ORDER BY c DESC LIMIT 5""",
        (chat_id, day - 6),
    )
    top = [(r["user_id"], r["c"]) for r in await cur.fetchall()]
    week_ts = _now() - 7 * 86400
    joins = await one(
        "SELECT COUNT(*) FROM events WHERE chat_id = ? AND kind = 'join' AND ts >= ?",
        (chat_id, week_ts))
    leaves = await one(
        "SELECT COUNT(*) FROM events WHERE chat_id = ? AND kind = 'leave' AND ts >= ?",
        (chat_id, week_ts))
    pun7 = await one(
        "SELECT COUNT(*) FROM punishments WHERE chat_id = ? AND created >= ?",
        (chat_id, week_ts))
    return {"total": total, "d1": d1, "d7": d7, "top": top,
            "joins": joins, "leaves": leaves, "pun7": pun7}


# ---------- наблюдение за профилями ----------

async def watch_get(chat_id: int, user_id: int) -> aiosqlite.Row | None:
    cur = await _db.execute(
        "SELECT * FROM watch_profiles WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
    )
    return await cur.fetchone()


async def watch_set(chat_id: int, user_id: int, sig: str, flagged: bool) -> None:
    await _db.execute(
        """INSERT INTO watch_profiles (chat_id, user_id, sig, flagged) VALUES (?, ?, ?, ?)
           ON CONFLICT(chat_id, user_id) DO UPDATE SET sig = excluded.sig, flagged = excluded.flagged""",
        (chat_id, user_id, sig, int(flagged)),
    )
    await _db.commit()


# ---------- доступ к боту (кого владелец пустил) ----------

async def access_add(user_id: int | None, username: str | None) -> None:
    await _db.execute(
        "INSERT INTO access (user_id, username, added) VALUES (?, ?, ?)",
        (user_id, (username or None) and username.lower().lstrip("@"), _now()),
    )
    await _db.commit()


async def access_remove(row_id: int) -> None:
    await _db.execute("DELETE FROM access WHERE id = ?", (row_id,))
    await _db.commit()


async def access_list() -> list[aiosqlite.Row]:
    cur = await _db.execute("SELECT * FROM access ORDER BY id")
    return await cur.fetchall()


async def access_allowed(user_id: int, username: str | None) -> bool:
    cur = await _db.execute(
        "SELECT 1 FROM access WHERE user_id = ? OR (username IS NOT NULL AND username = ?)",
        (user_id, (username or "").lower()),
    )
    return await cur.fetchone() is not None


# ---------- kv (глобальные настройки админа) ----------

async def kv_get(key: str) -> str | None:
    cur = await _db.execute("SELECT v FROM kv WHERE k = ?", (key,))
    row = await cur.fetchone()
    return row["v"] if row else None


async def kv_prefix(prefix: str) -> list[tuple[str, str]]:
    """Все пары по префиксу ключа — например, все отложенные правки карточек."""
    cur = await _db.execute(
        "SELECT k, v FROM kv WHERE k LIKE ? ORDER BY k", (prefix + "%",)
    )
    return [(r["k"], r["v"]) for r in await cur.fetchall()]


async def kv_set(key: str, value: str | None) -> None:
    if value is None:
        await _db.execute("DELETE FROM kv WHERE k = ?", (key,))
    else:
        await _db.execute(
            "INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (key, value),
        )
    await _db.commit()
