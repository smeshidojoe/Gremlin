"""Единая SQLite-база: чаты, настройки, вайтлисты, стоп-слова, наказания, события, юзеры."""
import os
import random
import re
import time
from dataclasses import dataclass, fields

import glob
import logging
import shutil
import sqlite3

import aiosqlite

from . import config, utils

logger = logging.getLogger("gremlin.db")

_db: aiosqlite.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chats(
    chat_id   INTEGER PRIMARY KEY,
    title     TEXT,
    username  TEXT,
    owner_id  INTEGER,
    added_at  INTEGER,
    active    INTEGER NOT NULL DEFAULT 1,
    net_id    INTEGER                     -- сетка, в которой состоит чат
);
CREATE TABLE IF NOT EXISTS nets(
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id  INTEGER NOT NULL,         -- сетки принадлежат владельцу чатов
    title     TEXT    NOT NULL,
    sync_mask INTEGER NOT NULL DEFAULT 9,   -- NET_BAN | NET_LIFT
    lift_mode TEXT    NOT NULL DEFAULT 'any',  -- any | source: кто снимает наказание
    created   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nets_owner ON nets(owner_id);
CREATE TABLE IF NOT EXISTS settings(
    chat_id         INTEGER PRIMARY KEY,
    inline_on       INTEGER NOT NULL DEFAULT 1,
    inline_punish   TEXT    NOT NULL DEFAULT 'mute',
    inline_mute_min INTEGER NOT NULL DEFAULT 60,
    inline_spam     INTEGER NOT NULL DEFAULT 40,
    links_on        INTEGER NOT NULL DEFAULT 1,
    extlinks_on     INTEGER NOT NULL DEFAULT 1,
    links_punish    TEXT    NOT NULL DEFAULT 'delete',
    links_mute_min  INTEGER NOT NULL DEFAULT 60,
    links_guest_punish   TEXT    NOT NULL DEFAULT 'delete',
    links_guest_mute_min INTEGER NOT NULL DEFAULT 60,
    -- наказания по типам: l* — участники, g* — не участники
    lp_tg         TEXT    NOT NULL DEFAULT 'delete',
    lm_tg         INTEGER NOT NULL DEFAULT 60,
    lp_ext        TEXT    NOT NULL DEFAULT 'delete',
    lm_ext        INTEGER NOT NULL DEFAULT 60,
    lp_men        TEXT    NOT NULL DEFAULT 'delete',
    lm_men        INTEGER NOT NULL DEFAULT 60,
    lp_fwd        TEXT    NOT NULL DEFAULT 'delete',
    lm_fwd        INTEGER NOT NULL DEFAULT 60,
    gp_tg         TEXT    NOT NULL DEFAULT 'delete',
    gm_tg         INTEGER NOT NULL DEFAULT 60,
    gp_ext        TEXT    NOT NULL DEFAULT 'delete',
    gm_ext        INTEGER NOT NULL DEFAULT 60,
    gp_men        TEXT    NOT NULL DEFAULT 'delete',
    gm_men        INTEGER NOT NULL DEFAULT 60,
    gp_fwd        TEXT    NOT NULL DEFAULT 'delete',
    gm_fwd        INTEGER NOT NULL DEFAULT 60,
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
    cmds_anywhere   INTEGER NOT NULL DEFAULT 0,
    cmds_bare       INTEGER NOT NULL DEFAULT 0,
    rates_on        INTEGER NOT NULL DEFAULT 0,
    rates_cd        INTEGER NOT NULL DEFAULT 30,
    digest_to       INTEGER NOT NULL DEFAULT 0,
    service_join    INTEGER NOT NULL DEFAULT 0,
    service_leave   INTEGER NOT NULL DEFAULT 0,
    service_other   INTEGER NOT NULL DEFAULT 0,
    misuse_mute     INTEGER NOT NULL DEFAULT 5,
    mute_reactions  INTEGER NOT NULL DEFAULT 1,
    warns_on        INTEGER NOT NULL DEFAULT 0,
    warns_limit     INTEGER NOT NULL DEFAULT 3,
    warns_punish    TEXT    NOT NULL DEFAULT 'mute',
    warns_mute_min  INTEGER NOT NULL DEFAULT 1440,
    rules_on        INTEGER NOT NULL DEFAULT 0,
    trust_on        INTEGER NOT NULL DEFAULT 0,
    trust_soften    INTEGER NOT NULL DEFAULT 1,
    trust_days      INTEGER NOT NULL DEFAULT 3,
    trust_msgs      INTEGER NOT NULL DEFAULT 20,
    trust_mask      INTEGER NOT NULL DEFAULT 31,
    games_on        INTEGER NOT NULL DEFAULT 0,
    games_adm       INTEGER NOT NULL DEFAULT 0,
    rus_punish      TEXT    NOT NULL DEFAULT 'mute',
    rus_min         INTEGER NOT NULL DEFAULT 5,
    duel_punish     TEXT    NOT NULL DEFAULT 'mute',
    duel_min        INTEGER NOT NULL DEFAULT 10,
    battle_punish   TEXT    NOT NULL DEFAULT 'mute',
    battle_min      INTEGER NOT NULL DEFAULT 5,
    court_punish    TEXT    NOT NULL DEFAULT 'mute',
    court_min       INTEGER NOT NULL DEFAULT 15,
    nn_mode         INTEGER NOT NULL DEFAULT 1,
    nn_threshold    INTEGER NOT NULL DEFAULT 85,
    sem_on          INTEGER NOT NULL DEFAULT 0,
    sem_threshold   INTEGER NOT NULL DEFAULT 75,
    sem_punish      TEXT    NOT NULL DEFAULT 'delete',
    sem_mute_min    INTEGER NOT NULL DEFAULT 60,
    sem_guests      INTEGER NOT NULL DEFAULT 0,
    burst_on        INTEGER NOT NULL DEFAULT 0,
    burst_users     INTEGER NOT NULL DEFAULT 3,
    burst_punish    TEXT    NOT NULL DEFAULT 'delete',
    burst_mute_min  INTEGER NOT NULL DEFAULT 60,
    nn_net          INTEGER NOT NULL DEFAULT 1,
    nn_seed         INTEGER NOT NULL DEFAULT 1,
    watch_nn        INTEGER NOT NULL DEFAULT 0,
    watch_react     INTEGER NOT NULL DEFAULT 0,
    cas_on          INTEGER NOT NULL DEFAULT 0,
    cas_join        INTEGER NOT NULL DEFAULT 1,
    cas_suspect     INTEGER NOT NULL DEFAULT 1,
    cas_score       INTEGER NOT NULL DEFAULT 60,
    ban_wipe        INTEGER NOT NULL DEFAULT 3,
    ocr_on          INTEGER NOT NULL DEFAULT 0,
    ocr_langs       TEXT    NOT NULL DEFAULT 'rus+eng',
    asr_on          INTEGER NOT NULL DEFAULT 0,
    asr_max_sec     INTEGER NOT NULL DEFAULT 120,
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
    name     TEXT,          -- имя на момент добавления: боту человек мог не писать
    added    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS watch_profiles(
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    sig     TEXT NOT NULL,          -- подпись профиля (имя|фамилия|username)
    flagged INTEGER NOT NULL DEFAULT 0,  -- карточка «подозрительный» уже отправлена
    score      INTEGER NOT NULL DEFAULT 0,  -- накопленные очки за сообщения
    score_ts   INTEGER NOT NULL DEFAULT 0,  -- когда копилку последний раз трогали
    card_score INTEGER NOT NULL DEFAULT 0,  -- с каким счётом ушла последняя карточка
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
CREATE TABLE IF NOT EXISTS phrases(
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    text    TEXT NOT NULL,
    hits    INTEGER NOT NULL DEFAULT 0,   -- сколько раз поймала
    created INTEGER NOT NULL DEFAULT 0,
    vec     BLOB
);
CREATE INDEX IF NOT EXISTS idx_phrases_chat ON phrases(chat_id);
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
    active   INTEGER NOT NULL DEFAULT 1,
    was_member INTEGER NOT NULL DEFAULT 1   -- состоял ли в чате на момент наказания
);
CREATE TABLE IF NOT EXISTS warns(
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id  INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    username TEXT,
    name     TEXT,
    reason   TEXT,
    by_id    INTEGER,
    created  INTEGER NOT NULL,
    active   INTEGER NOT NULL DEFAULT 1   -- 0 = сгорел при наказании или снят вручную
);
CREATE INDEX IF NOT EXISTS idx_warns_chat ON warns(chat_id, user_id, active);
CREATE TABLE IF NOT EXISTS samples(
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id  INTEGER NOT NULL,
    user_id  INTEGER,
    ts       INTEGER NOT NULL,
    origin   TEXT    NOT NULL,      -- auto | card | manual | random
    feature  TEXT,                  -- какое правило сработало
    label    TEXT    NOT NULL,      -- spam | ok | unknown
    pid      INTEGER,               -- наказание, к которому относится улика
    text     TEXT    NOT NULL,
    extra    TEXT,                  -- подписи и ссылки с кнопок
    vec      BLOB                   -- эмбеддинг, считаем лениво
);
CREATE INDEX IF NOT EXISTS idx_samples_chat ON samples(chat_id, id);
CREATE INDEX IF NOT EXISTS idx_samples_pick ON samples(label, origin);
CREATE INDEX IF NOT EXISTS idx_samples_pid ON samples(pid);
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
-- Ответы CAS (чужой список спамеров). Держим отдельно, а не в kv: ответ живёт
-- неделями, а спрашивать одного и того же человека приходится в каждом чате.
CREATE TABLE IF NOT EXISTS cas_cache(
    user_id INTEGER PRIMARY KEY,
    listed  INTEGER NOT NULL,
    ts      INTEGER NOT NULL
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
    inline_spam: int = 40
    links_on: int = 1
    extlinks_on: int = 1
    links_punish: str = "delete"
    links_mute_min: int = 60
    links_guest_punish: str = "delete"
    links_guest_mute_min: int = 60
    lp_tg: str = "delete"
    lm_tg: int = 60
    lp_ext: str = "delete"
    lm_ext: int = 60
    lp_men: str = "delete"
    lm_men: int = 60
    lp_fwd: str = "delete"
    lm_fwd: int = 60
    gp_tg: str = "delete"
    gm_tg: int = 60
    gp_ext: str = "delete"
    gm_ext: int = 60
    gp_men: str = "delete"
    gm_men: int = 60
    gp_fwd: str = "delete"
    gm_fwd: int = 60
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
    cmds_anywhere: int = 0
    cmds_bare: int = 0
    rates_on: int = 0
    rates_cd: int = 30
    digest_to: int = 0
    service_join: int = 0
    service_leave: int = 0
    service_other: int = 0
    misuse_mute: int = 5
    mute_reactions: int = 1
    warns_on: int = 0
    warns_limit: int = 3
    warns_punish: str = "mute"
    warns_mute_min: int = 1440
    rules_on: int = 0
    trust_on: int = 0
    trust_soften: int = 1
    trust_days: int = 3
    trust_msgs: int = 20
    trust_mask: int = 31
    games_on: int = 0
    games_adm: int = 0
    rus_punish: str = "mute"
    rus_min: int = 5
    duel_punish: str = "mute"
    duel_min: int = 10
    battle_punish: str = "mute"
    battle_min: int = 5
    court_punish: str = "mute"
    court_min: int = 15
    nn_mode: int = 1
    nn_threshold: int = 85
    sem_on: int = 0
    sem_threshold: int = 75
    sem_punish: str = "delete"
    sem_mute_min: int = 60
    sem_guests: int = 0
    burst_on: int = 0
    burst_users: int = 3
    burst_punish: str = "delete"
    burst_mute_min: int = 60
    nn_net: int = 1
    nn_seed: int = 1
    watch_nn: int = 0
    watch_react: int = 0
    cas_on: int = 0
    cas_join: int = 1
    cas_suspect: int = 1
    cas_score: int = 60
    ban_wipe: int = 3
    ocr_on: int = 0
    ocr_langs: str = "rus+eng"
    asr_on: int = 0
    asr_max_sec: int = 120
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
    "inline_spam": "INTEGER NOT NULL DEFAULT 40",
    "cmds_anywhere": "INTEGER NOT NULL DEFAULT 0",
    "misuse_mute": "INTEGER NOT NULL DEFAULT 5",
    "mute_reactions": "INTEGER NOT NULL DEFAULT 1",
    "warns_on": "INTEGER NOT NULL DEFAULT 0",
    "warns_limit": "INTEGER NOT NULL DEFAULT 3",
    "warns_punish": "TEXT NOT NULL DEFAULT 'mute'",
    "warns_mute_min": "INTEGER NOT NULL DEFAULT 1440",
    "rules_on": "INTEGER NOT NULL DEFAULT 0",
    "trust_on": "INTEGER NOT NULL DEFAULT 0",
    "trust_soften": "INTEGER NOT NULL DEFAULT 1",
    "trust_days": "INTEGER NOT NULL DEFAULT 3",
    "trust_msgs": "INTEGER NOT NULL DEFAULT 20",
    "trust_mask": "INTEGER NOT NULL DEFAULT 31",
    "nn_mode": "INTEGER NOT NULL DEFAULT 1",
    "nn_threshold": "INTEGER NOT NULL DEFAULT 85",
    "sem_on": "INTEGER NOT NULL DEFAULT 0",
    "sem_threshold": "INTEGER NOT NULL DEFAULT 75",
    "sem_punish": "TEXT NOT NULL DEFAULT 'delete'",
    "sem_mute_min": "INTEGER NOT NULL DEFAULT 60",
    "sem_guests": "INTEGER NOT NULL DEFAULT 0",
    "burst_on": "INTEGER NOT NULL DEFAULT 0",
    "burst_users": "INTEGER NOT NULL DEFAULT 3",
    "burst_punish": "TEXT NOT NULL DEFAULT 'delete'",
    "burst_mute_min": "INTEGER NOT NULL DEFAULT 60",
    "nn_net": "INTEGER NOT NULL DEFAULT 1",
    "nn_seed": "INTEGER NOT NULL DEFAULT 1",
    "watch_nn": "INTEGER NOT NULL DEFAULT 0",
    "watch_react": "INTEGER NOT NULL DEFAULT 0",
    "cas_on": "INTEGER NOT NULL DEFAULT 0",
    "cas_join": "INTEGER NOT NULL DEFAULT 1",
    "cas_suspect": "INTEGER NOT NULL DEFAULT 1",
    "cas_score": "INTEGER NOT NULL DEFAULT 60",
    "ban_wipe": "INTEGER NOT NULL DEFAULT 3",
    "ocr_on": "INTEGER NOT NULL DEFAULT 0",
    "ocr_langs": "TEXT NOT NULL DEFAULT 'rus+eng'",
    "asr_on": "INTEGER NOT NULL DEFAULT 0",
    "asr_max_sec": "INTEGER NOT NULL DEFAULT 120",
    "games_on": "INTEGER NOT NULL DEFAULT 0",
    "games_adm": "INTEGER NOT NULL DEFAULT 0",
    "rus_punish": "TEXT NOT NULL DEFAULT 'mute'",
    "rus_min": "INTEGER NOT NULL DEFAULT 5",
    "duel_punish": "TEXT NOT NULL DEFAULT 'mute'",
    "duel_min": "INTEGER NOT NULL DEFAULT 10",
    "battle_punish": "TEXT NOT NULL DEFAULT 'mute'",
    "battle_min": "INTEGER NOT NULL DEFAULT 5",
    "court_punish": "TEXT NOT NULL DEFAULT 'mute'",
    "court_min": "INTEGER NOT NULL DEFAULT 15",
    "links_guest_punish": "TEXT NOT NULL DEFAULT 'delete'",
    "links_guest_mute_min": "INTEGER NOT NULL DEFAULT 60",
    "lp_tg": "TEXT NOT NULL DEFAULT 'delete'",
    "lm_tg": "INTEGER NOT NULL DEFAULT 60",
    "lp_ext": "TEXT NOT NULL DEFAULT 'delete'",
    "lm_ext": "INTEGER NOT NULL DEFAULT 60",
    "lp_men": "TEXT NOT NULL DEFAULT 'delete'",
    "lm_men": "INTEGER NOT NULL DEFAULT 60",
    "lp_fwd": "TEXT NOT NULL DEFAULT 'delete'",
    "lm_fwd": "INTEGER NOT NULL DEFAULT 60",
    "gp_tg": "TEXT NOT NULL DEFAULT 'delete'",
    "gm_tg": "INTEGER NOT NULL DEFAULT 60",
    "gp_ext": "TEXT NOT NULL DEFAULT 'delete'",
    "gm_ext": "INTEGER NOT NULL DEFAULT 60",
    "gp_men": "TEXT NOT NULL DEFAULT 'delete'",
    "gm_men": "INTEGER NOT NULL DEFAULT 60",
    "gp_fwd": "TEXT NOT NULL DEFAULT 'delete'",
    "gm_fwd": "INTEGER NOT NULL DEFAULT 60",
    "words_guests": "INTEGER NOT NULL DEFAULT 1",
    "extlinks_on": "INTEGER NOT NULL DEFAULT 1",
    "cmds_bare": "INTEGER NOT NULL DEFAULT 0",
    "rates_on": "INTEGER NOT NULL DEFAULT 0",
    "rates_cd": "INTEGER NOT NULL DEFAULT 30",
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
    "punishments": {"was_member": "INTEGER NOT NULL DEFAULT 1"},
    "answers": {"last_used": "INTEGER NOT NULL DEFAULT 0"},
    "chats": {"net_id": "INTEGER"},
    "watch_profiles": {"score": "INTEGER NOT NULL DEFAULT 0",
                       "score_ts": "INTEGER NOT NULL DEFAULT 0",
                       "card_score": "INTEGER NOT NULL DEFAULT 0"},
}


async def _migrate() -> None:
    # векторы, посчитанные до нормализации текста, больше не сопоставимы
    # с новыми — считаем заново, благо это фоновая работа
    cur = await _db.execute("SELECT v FROM kv WHERE k = 'mig_norm_vec'")
    if await cur.fetchone() is None:
        await _db.execute("UPDATE samples SET vec = NULL")
        await _db.execute("UPDATE phrases SET vec = NULL")
        await _db.execute("INSERT INTO kv (k, v) VALUES ('mig_norm_vec', '1')")
        await _db.commit()

    # имя в списке доступа: раньше про добавленного по нику мы не знали ничего,
    # и в списке висел голый @ник
    cur = await _db.execute("PRAGMA table_info(access)")
    if "name" not in {r["name"] for r in await cur.fetchall()}:
        await _db.execute("ALTER TABLE access ADD COLUMN name TEXT")

    # первая версия сеток была одна на владельца: таблица nets с owner_id вместо
    # id и флаг chats.net_on. Перетаскиваем в именованные сетки.
    cur = await _db.execute("PRAGMA table_info(nets)")
    net_cols = {r["name"] for r in await cur.fetchall()}
    if net_cols and "id" not in net_cols:
        cur = await _db.execute("PRAGMA table_info(chats)")
        if "net_id" not in {r["name"] for r in await cur.fetchall()}:
            await _db.execute("ALTER TABLE chats ADD COLUMN net_id INTEGER")
        await _db.execute("ALTER TABLE nets RENAME TO nets_old")
        await _db.execute("""
            CREATE TABLE nets(
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id  INTEGER NOT NULL,
                title     TEXT    NOT NULL,
                sync_mask INTEGER NOT NULL DEFAULT 9,
                lift_mode TEXT    NOT NULL DEFAULT 'any',
                created   INTEGER NOT NULL
            )""")
        cur = await _db.execute("SELECT * FROM nets_old")
        for row in await cur.fetchall():
            await _db.execute(
                """INSERT INTO nets (owner_id, title, sync_mask, lift_mode, created)
                   VALUES (?, 'Моя сетка', ?, ?, ?)""",
                (row["owner_id"], row["sync_mask"], row["lift_mode"], row["created"]),
            )
            cur2 = await _db.execute("SELECT last_insert_rowid() AS id")
            new_id = (await cur2.fetchone())["id"]
            await _db.execute(
                "UPDATE chats SET net_id = ? WHERE owner_id = ? AND net_on = 1",
                (new_id, row["owner_id"]),
            )
        await _db.execute("DROP TABLE nets_old")
        await _db.commit()

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
    # у чатов, заведённых до появления развилки «перенести настройки», её быть
    # не должно: они уже настроены руками
    cur = await _db.execute("SELECT v FROM kv WHERE k = 'mig_setup_done'")
    if await cur.fetchone() is None:
        await _db.execute(
            "INSERT OR IGNORE INTO kv (k, v) "
            "SELECT 'setup_done:' || chat_id, '1' FROM chats"
        )
        await _db.execute("INSERT INTO kv (k, v) VALUES ('mig_setup_done', '1')")

    # наказание за ссылки разъехалось по типам: раскладываем старое значение
    cur = await _db.execute("SELECT v FROM kv WHERE k = 'mig_links_split'")
    if await cur.fetchone() is None:
        for t in ("tg", "ext", "men", "fwd"):
            await _db.execute(
                f"UPDATE settings SET lp_{t} = links_punish, lm_{t} = links_mute_min, "
                f"gp_{t} = links_guest_punish, gm_{t} = links_guest_mute_min"
            )
        await _db.execute("INSERT INTO kv (k, v) VALUES ('mig_links_split', '1')")

    # таблицы удалённых функций: список для жалоб и отдельный вайтлист анонимов
    # (его содержимое давно переехало в общий whitelist).
    # warns тут когда-то тоже был, но функция вернулась: на свежей базе эта
    # чистка сносила только что созданную таблицу, и варны падали с ошибкой.
    cur = await _db.execute("SELECT v FROM kv WHERE k = 'mig_drop_dead'")
    if await cur.fetchone() is None:
        for dead in ("report_wl", "anon_wl"):
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


def _healthy(path: str) -> bool:
    """Файл базы читается и не побит."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            con.close()
    except Exception:
        return False


def _restore_from_backup() -> str | None:
    """Подменить битую базу свежей целой копией. Возвращает имя копии.

    База лежит на примонтированном томе, а SQLite в режиме WAL плохо переносит
    вмешательство снаружи: одна запись чужим процессом — и файл больше не
    читается. Раньше бот в такой ситуации просто падал при старте и молчал,
    хотя рядом лежали суточные копии.

    Битый файл не удаляем: кладём рядом с пометкой, вдруг из него ещё что-то
    выцарапают.
    """
    backups = sorted(glob.glob(os.path.join(config.BACKUP_DIR, "gremlin-*.sqlite3")),
                     reverse=True)
    for path in backups:
        if not _healthy(path):
            logger.warning("копия %s тоже битая", os.path.basename(path))
            continue
        stamp = time.strftime("%Y%m%d-%H%M%S")
        if os.path.exists(config.DB_PATH):
            os.replace(config.DB_PATH, f"{config.DB_PATH}.malformed-{stamp}")
        for tail in ("-wal", "-shm"):          # хвосты от битой базы не нужны
            leftover = config.DB_PATH + tail
            if os.path.exists(leftover):
                os.remove(leftover)
        shutil.copy2(path, config.DB_PATH)
        logger.error("база была повреждена — восстановлена из копии %s",
                     os.path.basename(path))
        return os.path.basename(path)
    return None


# что случилось с базой при старте: показываем владельцу, чтобы потеря данных
# не прошла незамеченной
restored_from: str | None = None


async def init() -> None:
    global _db, restored_from
    if os.path.exists(config.DB_PATH) and not _healthy(config.DB_PATH):
        restored_from = _restore_from_backup()
        if restored_from is None:
            logger.error("база повреждена, целых копий нет — начинаем с пустой")
            stamp = time.strftime("%Y%m%d-%H%M%S")
            os.replace(config.DB_PATH, f"{config.DB_PATH}.malformed-{stamp}")
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


async def clear_log_refs(chat_id: int) -> None:
    """Убрать ссылки на чат как на лог-чат — он больше не рабочий."""
    await _db.execute("UPDATE settings SET log_chat_id = NULL WHERE log_chat_id = ?",
                      (chat_id,))
    await _db.commit()


async def set_chat_active(chat_id: int, active: bool) -> None:
    await _db.execute("UPDATE chats SET active = ? WHERE chat_id = ?", (int(active), chat_id))
    await _db.commit()


# всё, что привязано к чату: при повышении группы до супергруппы Telegram
# меняет ей id, и без переноса чат для бота превращается в чужой
_CHAT_TABLES = ("settings", "triggers", "chat_cmds", "watch_profiles", "whitelist",
                "link_wl", "inline_wl", "words", "punishments", "warns",
                "samples", "events", "msg_stats", "phrases")


async def migrate_chat(old_id: int, new_id: int) -> bool:
    """Перенести чат на новый id после повышения до супергруппы.

    Telegram выдаёт супергруппе другой id (−123 становится −100…123) и присылает
    об этом служебное сообщение. Если его не обработать, бот перестаёт узнавать
    чат: настройки, вайтлисты, стоп-слова и копилка улик остаются висеть на
    старом id, а в новом чате бот молчит, считая его чужим.

    False — переносить нечего (чат не был зарегистрирован).
    """
    cur = await _db.execute("SELECT 1 FROM chats WHERE chat_id = ?", (old_id,))
    if await cur.fetchone() is None:
        return False
    # новая запись могла успеть появиться (бота добавили в уже супергруппу):
    # тогда старую просто убираем, данные в ней всё равно пустые
    cur = await _db.execute("SELECT 1 FROM chats WHERE chat_id = ?", (new_id,))
    if await cur.fetchone() is not None:
        await _db.execute("DELETE FROM chats WHERE chat_id = ?", (old_id,))
        for table in _CHAT_TABLES:
            await _db.execute(f"DELETE FROM {table} WHERE chat_id = ?", (old_id,))
        await _db.commit()
        return True

    await _db.execute("UPDATE chats SET chat_id = ? WHERE chat_id = ?", (new_id, old_id))
    for table in _CHAT_TABLES:
        # OR REPLACE: у settings и msg_stats chat_id входит в первичный ключ,
        # и строка под новым id теоретически может уже существовать
        await _db.execute(f"UPDATE OR REPLACE {table} SET chat_id = ? WHERE chat_id = ?",
                          (new_id, old_id))
    # чат мог быть чьим-то лог-чатом или адресатом сводки — эти ссылки тоже правим
    await _db.execute("UPDATE settings SET log_chat_id = ? WHERE log_chat_id = ?",
                      (new_id, old_id))
    await _db.execute("UPDATE settings SET digest_to = ? WHERE digest_to = ?",
                      (new_id, old_id))
    await _db.commit()
    return True


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


async def chats_for(user_id: int) -> list[aiosqlite.Row]:
    """Чаты, которыми человек вправе управлять.

    Владелец бота видит все, остальные — только те, куда бота позвали они сами.
    Лог-чаты в список не попадают: они настраиваются внутри своего чата.
    """
    chats = await moderated_chats()
    if user_id in config.ADMIN_IDS:
        return chats
    return [c for c in chats if c["owner_id"] == user_id]


async def owns_chat(user_id: int, chat_id: int) -> bool:
    """Может ли человек трогать этот чат."""
    if user_id in config.ADMIN_IDS:
        return True
    ch = await get_chat(chat_id)
    return ch is not None and ch["owner_id"] == user_id


async def log_chat_still_needed(log_id: int, leaving: int) -> str | None:
    """Кому ещё нужен этот лог-чат, кроме уходящего. None — никому.

    Возвращает причину строкой: её показываем человеку, чтобы он понимал,
    почему бот остался в логе, а не молча проигнорировал просьбу.
    """
    if not log_id or log_id == leaving:
        return "это сам чат"
    if await global_log() == log_id:
        return "это общий лог бота"
    cur = await _db.execute(
        """SELECT COUNT(*) AS n FROM settings s JOIN chats c USING (chat_id)
           WHERE s.log_chat_id = ? AND s.chat_id != ? AND c.active = 1""",
        (log_id, leaving))
    row = await cur.fetchone()
    if row and row["n"]:
        return f"туда шлют карточки ещё {row['n']} чат(ов)"
    # Чистый лог-чат — это чат, где бот только пишет карточки. Если у него
    # есть собственная жизнь (свои наказания, свои стоп-слова, свой лог),
    # значит его модерируют, и уходить оттуда нельзя.
    for table, what in (("punishments", "там есть свои наказания"),
                        ("words", "там свои стоп-слова"),
                        ("triggers", "там свои триггеры"),
                        ("chat_cmds", "там свои счётчики")):
        cur = await _db.execute(
            f"SELECT 1 FROM {table} WHERE chat_id = ? LIMIT 1", (log_id,))
        if await cur.fetchone():
            return f"это рабочий чат: {what}"
    cur = await _db.execute(
        "SELECT log_chat_id FROM settings WHERE chat_id = ?", (log_id,))
    row = await cur.fetchone()
    if row and row["log_chat_id"]:
        return "у него самого настроен лог-чат, значит его модерируют"
    return None


async def moderated_chats() -> list[aiosqlite.Row]:
    """Рабочие чаты для меню: без тех, что служат лог-чатом для другого чата."""
    cur = await _db.execute(
        """SELECT * FROM chats WHERE active = 1 AND chat_id NOT IN (
               SELECT log_chat_id FROM settings
               WHERE log_chat_id IS NOT NULL AND log_chat_id != chat_id
           ) ORDER BY added_at"""
    )
    rows = await cur.fetchall()
    gl = await global_log()          # общий лог тоже не рабочий чат
    return [r for r in rows if r["chat_id"] != gl]


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
    поэтому искать по ним нельзя.

    Совпадением считаем и id, и ник: человека могли добавить по нику, когда
    id узнать не вышло, а сегодня он уже известен — это всё равно он, и второй
    записи быть не должно.
    """
    uname = (username or "").lower().lstrip("@") or None
    for e in await wl_entries(chat_id):
        if user_id is not None and e["user_id"] == user_id:
            return e
        if uname and e["username"] == uname:
            return e
    return None


async def wl_attach_id(chat_id: int, username: str, user_id: int,
                       title: str | None = None) -> None:
    """Дописать id к записи, заведённой по нику.

    Ник меняют, id — нет: как только он стал известен, запись надо привязать
    к нему, иначе после смены ника игнор перестанет работать.
    """
    uname = username.lower().lstrip("@")
    await _db.execute(
        """UPDATE whitelist SET user_id = ?, title = COALESCE(title, ?)
           WHERE chat_id = ? AND username = ? AND user_id IS NULL""",
        (user_id, title, chat_id, uname))
    await _db.commit()


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
                      title: str | None = None) -> bool:
    """Разрешить чат или канал в ссылках. False — он уже в списке.

    Совпадением считаем и id, и ник: один и тот же канал не должен лежать
    в списке дважды, даже если добавляли его по-разному.
    """
    uname = (username or None) and username.lower().lstrip("@")
    for r in await link_wl_list(chat_id):
        if target_id is not None and r["target_id"] == target_id:
            if uname and not r["username"]:      # знаем теперь и ник — допишем
                await _db.execute("UPDATE link_wl SET username = ? WHERE id = ?",
                                  (uname, r["id"]))
                await _db.commit()
            return False
        if uname and r["username"] == uname:
            if target_id is not None and r["target_id"] is None:
                await _db.execute("UPDATE link_wl SET target_id = ? WHERE id = ?",
                                  (target_id, r["id"]))
                await _db.commit()
            return False
    await _db.execute(
        "INSERT INTO link_wl (chat_id, target_id, username, title) VALUES (?, ?, ?, ?)",
        (chat_id, target_id, uname, title),
    )
    await _db.commit()
    return True


async def link_wl_remove(row_id: int) -> None:
    await _db.execute("DELETE FROM link_wl WHERE id = ?", (row_id,))
    await _db.commit()


async def link_wl_list(chat_id: int) -> list[aiosqlite.Row]:
    cur = await _db.execute("SELECT * FROM link_wl WHERE chat_id = ? ORDER BY id", (chat_id,))
    return await cur.fetchall()


# ---------- разрешённые инлайн-боты ----------

async def inline_wl_add(chat_id: int, username: str,
                        bot_id: int | None = None) -> bool:
    """Разрешить инлайн-бота. False — он уже в списке."""
    uname = username.lower().lstrip("@")
    for r in await inline_wl_list(chat_id):
        if r["username"] == uname or (bot_id and r["bot_id"] == bot_id):
            if bot_id and not r["bot_id"]:
                await _db.execute("UPDATE inline_wl SET bot_id = ? WHERE id = ?",
                                  (bot_id, r["id"]))
                await _db.commit()
            return False
    await _db.execute(
        "INSERT INTO inline_wl (chat_id, bot_id, username) VALUES (?, ?, ?)",
        (chat_id, bot_id, uname),
    )
    await _db.commit()
    return True


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
                         by_id: int | None, was_member: bool = True) -> int:
    """was_member — состоял ли человек в чате. Комментатор под постом канала в
    чате не состоит, и при разбане ссылка на возврат ему ни к чему."""
    # прошлые активные наказания того же юзера в этом чате гасим
    await _db.execute(
        "UPDATE punishments SET active = 0 WHERE chat_id = ? AND user_id = ? AND active = 1",
        (chat_id, user_id),
    )
    cur = await _db.execute(
        """INSERT INTO punishments (chat_id, user_id, username, name, kind, reason,
                                    until_ts, by_id, created, active, was_member)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
        (chat_id, user_id, username, name, kind, reason, until, by_id, _now(),
         int(was_member)),
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


# ---------- варны ----------

async def warn_add(chat_id: int, user, reason: str, by_id: int) -> int:
    """Записать варн, вернуть текущее число активных у этого человека."""
    await _db.execute(
        """INSERT INTO warns(chat_id, user_id, username, name, reason, by_id, created)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (chat_id, user.id, user.username, user.full_name, reason, by_id, _now()),
    )
    await _db.commit()
    return await warn_count(chat_id, user.id)


async def warn_count(chat_id: int, user_id: int) -> int:
    cur = await _db.execute(
        "SELECT COUNT(*) AS c FROM warns WHERE chat_id = ? AND user_id = ? AND active = 1",
        (chat_id, user_id),
    )
    return (await cur.fetchone())["c"]


async def warn_reset(chat_id: int, user_id: int) -> None:
    """Погасить варны: добрал лимит и получил наказание либо админ снял вручную."""
    await _db.execute(
        "UPDATE warns SET active = 0 WHERE chat_id = ? AND user_id = ? AND active = 1",
        (chat_id, user_id),
    )
    await _db.commit()


async def warn_users(chat_id: int) -> list[aiosqlite.Row]:
    """Кто сейчас с варнами: по человеку строка со счётчиком и последней причиной."""
    cur = await _db.execute(
        """SELECT user_id, COUNT(*) AS cnt, MAX(created) AS last_ts,
                  MAX(username) AS username, MAX(name) AS name
           FROM warns WHERE chat_id = ? AND active = 1
           GROUP BY user_id ORDER BY cnt DESC, last_ts DESC""",
        (chat_id,),
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

# ---------- улики для нейрофильтра ----------
#
# Копим тексты, за которые наказывали, и тексты, которые оказались нормальными.
# origin говорит, откуда взялась оценка, и это важнее самой оценки:
#   auto   — сработало правило автомода. Годится как пример спама.
#   card   — админ нажал кнопку на карточке. Самый честный сигнал: человек
#            посмотрел на конкретное сообщение и решил.
#   manual — !mute/!ban руками. Причины у людей свои («заебал байтами»),
#            к содержимому сообщения они часто отношения не имеют, поэтому
#            в обучающую выборку такие улики НЕ идут — только в историю.
#   random — обычное сообщение из потока, отрицательный пример.

PROFILE_ORIGINS = ("auto", "card", "random")

# Стартовый набор лежит в той же копилке под чатом 0: он ничей и общий.
SEED_CHAT = 0


async def sample_add(chat_id: int, user_id: int | None, origin: str, label: str,
                     text: str, feature: str | None = None,
                     extra: str | None = None, pid: int | None = None) -> int | None:
    """Запомнить улику. Пустой текст не храним — учиться на нём нечему."""
    text = (text or "").strip()[:config.SAMPLE_TEXT_LIMIT]
    if not text:
        return None
    cur = await _db.execute(
        """INSERT INTO samples (chat_id, user_id, ts, origin, feature, label,
                                pid, text, extra)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (chat_id, user_id, _now(), origin, feature, label, pid, text, extra),
    )
    await _db.commit()
    return cur.lastrowid


async def sample_relabel(sample_id: int, label: str, origin: str | None = None) -> None:
    """Переставить оценку: админ снял наказание — значит это был не спам."""
    if origin:
        await _db.execute("UPDATE samples SET label = ?, origin = ?, vec = NULL "
                          "WHERE id = ?", (label, origin, sample_id))
    else:
        await _db.execute("UPDATE samples SET label = ?, vec = NULL WHERE id = ?",
                          (label, sample_id))
    await _db.commit()


async def sample_relabel_by_pid(pid: int, label: str) -> int:
    """То же по id наказания — им помечены улики автомода."""
    cur = await _db.execute(
        "UPDATE samples SET label = ?, origin = 'card', vec = NULL WHERE pid = ?",
        (label, pid))
    await _db.commit()
    return cur.rowcount or 0


async def sample_last_for(chat_id: int, user_id: int, within: int = 3600):
    """Последняя улика по человеку — к ней привязываются нажатия на карточке."""
    cur = await _db.execute(
        """SELECT * FROM samples WHERE chat_id = ? AND user_id = ? AND ts > ?
           ORDER BY id DESC LIMIT 1""",
        (chat_id, user_id, _now() - within))
    return await cur.fetchone()


async def samples_profile(chat_id: int | None = None,
                          limit: int = 2000) -> list[aiosqlite.Row]:
    """Улики, годные для сравнения: без ручных наказаний с их вольными причинами."""
    marks = ",".join("?" * len(PROFILE_ORIGINS))
    q = (f"SELECT * FROM samples WHERE origin IN ({marks}) AND label != 'unknown'"
         f" AND chat_id != {SEED_CHAT}")
    args = list(PROFILE_ORIGINS)
    if chat_id is not None:
        q += " AND chat_id = ?"
        args.append(chat_id)
    cur = await _db.execute(q + " ORDER BY id DESC LIMIT ?", (*args, limit))
    return await cur.fetchall()


async def samples_stats(chat_id: int | None = None) -> dict:
    """Сколько чего накопилось — показываем в меню, чтобы было видно прогресс."""
    # без чата считаем свои улики всех чатов; стартовый набор сюда не идёт —
    # он общий и чужой, в «сколько чат накопил» ему делать нечего
    q = "SELECT origin, label, COUNT(*) AS n FROM samples"
    args: tuple = ()
    if chat_id is not None:
        q += " WHERE chat_id = ?"
        args = (chat_id,)
    else:
        q += f" WHERE chat_id != {SEED_CHAT}"
    cur = await _db.execute(q + " GROUP BY origin, label", args)
    out: dict = {"spam": 0, "ok": 0, "unknown": 0, "profile": 0, "total": 0}
    for r in await cur.fetchall():
        out["total"] += r["n"]
        out[r["label"]] = out.get(r["label"], 0) + r["n"]
        if r["origin"] in PROFILE_ORIGINS and r["label"] != "unknown":
            out["profile"] += r["n"]
    return out


async def samples_without_vec(limit: int = 200) -> list[aiosqlite.Row]:
    cur = await _db.execute(
        f"""SELECT id, text FROM samples
            WHERE vec IS NULL AND label != 'unknown'
              AND origin IN ({",".join("?" * len(PROFILE_ORIGINS))})
            ORDER BY id DESC LIMIT ?""", (*PROFILE_ORIGINS, limit))
    return await cur.fetchall()


async def sample_set_vec(sample_id: int, vec: bytes) -> None:
    await _db.execute("UPDATE samples SET vec = ? WHERE id = ?", (vec, sample_id))
    await _db.commit()


async def phrases_list(chat_id: int) -> list[aiosqlite.Row]:
    cur = await _db.execute(
        "SELECT * FROM phrases WHERE chat_id = ? ORDER BY id", (chat_id,))
    return await cur.fetchall()


async def phrase_add(chat_id: int, text: str) -> int | None:
    """Добавить фразу-образец. None — такая уже есть."""
    text = " ".join((text or "").split())[:config.SAMPLE_TEXT_LIMIT]
    if not text:
        return None
    cur = await _db.execute(
        "SELECT id FROM phrases WHERE chat_id = ? AND lower(text) = lower(?)",
        (chat_id, text))
    if await cur.fetchone():
        return None
    cur = await _db.execute(
        "INSERT INTO phrases (chat_id, text, created) VALUES (?,?,?)",
        (chat_id, text, _now()))
    await _db.commit()
    return cur.lastrowid


async def phrase_del(chat_id: int, phrase_id: int) -> None:
    await _db.execute("DELETE FROM phrases WHERE chat_id = ? AND id = ?",
                      (chat_id, phrase_id))
    await _db.commit()


async def phrase_hit(phrase_id: int) -> None:
    """Счётчик срабатываний: по нему видно, какие фразы работают, а какие зря."""
    await _db.execute("UPDATE phrases SET hits = hits + 1 WHERE id = ?", (phrase_id,))
    await _db.commit()


async def phrase_set_vec(phrase_id: int, vec: bytes) -> None:
    await _db.execute("UPDATE phrases SET vec = ? WHERE id = ?", (vec, phrase_id))
    await _db.commit()


async def samples_profile_net(chat_id: int, limit: int = 4000) -> list[aiosqlite.Row]:
    """Улики чата плюс улики его сетки.

    Чаты одной сетки принадлежат одному человеку и похожи между собой, поэтому
    объединять их копилки честно: молодой чат в сетке начинает работать сразу,
    а не через месяц. С чужими чатами такого не делаем — там своя норма.
    """
    peers = [c["chat_id"] for c in await net_peers(chat_id)]
    if not peers:
        return await samples_profile(chat_id, limit)
    ids = [chat_id, *peers]
    marks = ",".join("?" * len(PROFILE_ORIGINS))
    chats = ",".join("?" * len(ids))
    cur = await _db.execute(
        f"""SELECT * FROM samples
            WHERE origin IN ({marks}) AND label != 'unknown' AND chat_id IN ({chats})
            ORDER BY id DESC LIMIT ?""",
        (*PROFILE_ORIGINS, *ids, limit))
    return await cur.fetchall()


async def seed_add(text: str, label: str) -> bool:
    """Добавить пример в стартовый набор. False — такой уже есть."""
    text = " ".join((text or "").split())[:config.SAMPLE_TEXT_LIMIT]
    if len(text) < 10:
        return False
    cur = await _db.execute(
        "SELECT 1 FROM samples WHERE chat_id = ? AND text = ?", (SEED_CHAT, text))
    if await cur.fetchone():
        return False
    await _db.execute(
        """INSERT INTO samples (chat_id, user_id, ts, origin, feature, label, text)
           VALUES (?, NULL, ?, 'seed', 'набор', ?, ?)""",
        (SEED_CHAT, _now(), label, text))
    return True


async def seed_commit() -> None:
    await _db.commit()


async def seed_stats() -> dict:
    cur = await _db.execute(
        "SELECT label, COUNT(*) AS n FROM samples WHERE chat_id = ? GROUP BY label",
        (SEED_CHAT,))
    out = {"spam": 0, "ok": 0}
    for r in await cur.fetchall():
        out[r["label"]] = r["n"]
    out["total"] = out["spam"] + out["ok"]
    return out


async def seed_clear() -> int:
    cur = await _db.execute("DELETE FROM samples WHERE chat_id = ?", (SEED_CHAT,))
    await _db.commit()
    return cur.rowcount or 0


async def samples_seed(limit: int) -> list[aiosqlite.Row]:
    """Стартовый набор для подмешивания в профиль молодого чата.

    Берём поровну спама и нормы. Чужие датасеты почти всегда лежат
    отсортированными по метке — обычный «первые N по id» дал бы одну только
    норму, и молодой чат научился бы, что спама не существует.

    Отбор всегда один и тот же (по возрастанию id), иначе посчитанные векторы
    пришлось бы считать заново после каждого перезапуска.
    """
    half = max(limit // 2, 1)
    rows: list[aiosqlite.Row] = []
    for label in ("spam", "ok"):
        cur = await _db.execute(
            """SELECT * FROM samples WHERE chat_id = ? AND label = ?
               ORDER BY id LIMIT ?""", (SEED_CHAT, label, half))
        rows.extend(await cur.fetchall())
    return rows


async def samples_of_origin(chat_id: int, origin: str,
                            limit: int = 2000) -> list[aiosqlite.Row]:
    """Улики одного вида — например, спам-профили (origin='profile')."""
    cur = await _db.execute(
        """SELECT * FROM samples WHERE chat_id = ? AND origin = ?
           ORDER BY id DESC LIMIT ?""", (chat_id, origin, limit))
    return await cur.fetchall()


async def samples_unknown(chat_id: int, limit: int = 2000) -> list[aiosqlite.Row]:
    """Улики без оценки — ручные наказания. Лежат мёртвым грузом, пока их
    не разметят: причина у человека своя, и что там было, знает только он."""
    cur = await _db.execute(
        """SELECT * FROM samples WHERE chat_id = ? AND label = 'unknown'
           ORDER BY id DESC LIMIT ?""", (chat_id, limit))
    return await cur.fetchall()


async def samples_relabel_many(ids: list[int], label: str) -> int:
    """Разметить пачку улик разом — по итогам разбивки на кучки.

    origin становится 'card': оценку поставил человек, а такие мы не удаляем
    при подрезке копилки.
    """
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    cur = await _db.execute(
        f"UPDATE samples SET label = ?, origin = 'card' WHERE id IN ({marks})",
        (label, *ids))
    await _db.commit()
    return cur.rowcount or 0


async def sample_by_id(sample_id: int):
    cur = await _db.execute("SELECT * FROM samples WHERE id = ?", (sample_id,))
    return await cur.fetchone()


async def samples_trim(keep: int) -> int:
    """Не даём копилке расти бесконечно: держим последние keep улик на чат.

    База целиком копируется в бэкап каждые сутки и хранится в 14 копиях,
    поэтому лишний гигабайт улик превращается в четырнадцать. Режем самое
    дешёвое — поток автомата и случайные образцы нормы, которых набегает
    сколько угодно. Улики, размеченные человеком кнопкой на карточке
    (origin='card'), не трогаем никогда: их мало и они самые ценные.

    Стартовый набор тоже не трогаем: его загрузили руками и осознанно, а
    «последние keep по id» вырезали бы из отсортированного датасета целый
    класс — метка-то у него идёт подряд.
    """
    cur = await _db.execute(
        f"""DELETE FROM samples
            WHERE origin != 'card' AND chat_id != {SEED_CHAT}
              AND id NOT IN (
                  SELECT id FROM samples s2
                  WHERE s2.chat_id = samples.chat_id AND s2.origin != 'card'
                  ORDER BY s2.id DESC LIMIT ?)""", (keep,))
    await _db.commit()
    return cur.rowcount or 0


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


async def names_in(text: str) -> str:
    """Заменить id после «by» на @ник — id в логе человеку ничего не говорят.

    Делается при выводе, поэтому старые записи тоже читаются нормально.
    """
    if not text:
        return text
    out = text
    for uid in set(re.findall(r"(?<= by )(-?\d+)", text)):
        out = out.replace(f" by {uid}", f" by {await user_handle(int(uid))}")
    return out


async def user_handle(user_id: int | None) -> str:
    """Короткая подпись человека: @ник, а если ника нет — имя, иначе id.

    Для мест, где важна опознаваемость, а не полнота: имена бывают в эмодзи и
    занимают всю строку.
    """
    row = await get_user(user_id) if user_id else None
    if row and row["username"]:
        return f"@{row['username']}"
    if row and row["first_name"]:
        return row["first_name"]
    return str(user_id or "—")


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
        # имя, если его знали при добавлении: «Тыква @sweet_pumpkins» понятнее
        # голого ника, а ник понятнее голого id
        handle = f"@{username.lstrip('@')}"
        return f"{fallback} {handle}" if fallback else handle
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


async def trig_find(chat_id: int, phrase: str):
    """Триггер с такой же фразой. Второй такой не нужен: сработает всё равно
    первый, а в списке будут висеть две одинаковые строки."""
    cur = await _db.execute(
        "SELECT * FROM triggers WHERE chat_id = ? AND phrase = ?",
        (chat_id, phrase.strip().lower()))
    return await cur.fetchone()


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


# ---------- лорбук ----------

async def week_activity(chat_id: int) -> dict[int, dict]:
    """Кто и сколько писал за прошедшую неделю — сырьё для титулов.

    На человека: сколько сообщений за 7 дней, за сколько разных дней он
    отметился, сколько было неделей раньше и с какого дня мы его вообще знаем.
    """
    day = utils.day_num()
    cur = await _db.execute(
        """SELECT user_id,
                  SUM(CASE WHEN day >= ? THEN cnt ELSE 0 END)      AS week,
                  COUNT(DISTINCT CASE WHEN day >= ? THEN day END)  AS days,
                  SUM(CASE WHEN day BETWEEN ? AND ? THEN cnt ELSE 0 END) AS prev,
                  MIN(day) AS first_day
           FROM msg_stats WHERE chat_id = ? AND user_id > 0
           GROUP BY user_id""",
        (day - 6, day - 6, day - 13, day - 7, chat_id),
    )
    return {r["user_id"]: {"week": r["week"] or 0, "days": r["days"] or 0,
                           "prev": r["prev"] or 0, "first_day": r["first_day"]}
            for r in await cur.fetchall()}


async def active_writers(chat_id: int, days: int) -> list[int]:
    """Кто писал в чат за последние N суток. Список участников Bot API не даёт,
    поэтому «живые» люди чата — это те, кого мы видели в статистике."""
    cur = await _db.execute(
        """SELECT DISTINCT user_id FROM msg_stats
           WHERE chat_id = ? AND day >= ? AND user_id > 0""",
        (chat_id, utils.day_num() - days + 1),
    )
    return [r["user_id"] for r in await cur.fetchall()]


async def trust_facts(chat_id: int, user_id: int) -> dict:
    """Сырые факты для уровня доверия: стаж, активность, наказания.

    Всё берём из своей базы — ни одного запроса в Telegram.
    """
    day = utils.day_num()

    async def one(q: str, args) -> int:
        cur = await _db.execute(q, args)
        return (await cur.fetchone())[0] or 0

    msgs30 = await one(
        "SELECT SUM(cnt) FROM msg_stats WHERE chat_id = ? AND user_id = ? AND day >= ?",
        (chat_id, user_id, day - 29))
    first_day = await one(
        "SELECT MIN(day) FROM msg_stats WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id))
    row = await get_user(user_id)
    seen = row["first_seen"] if row and row["first_seen"] else None
    days_known = (day - first_day) if first_day else 0
    if seen:
        days_known = max(days_known, (_now() - seen) // 86400)
    pun30 = await one(
        "SELECT COUNT(*) FROM punishments WHERE chat_id = ? AND user_id = ? AND created >= ?",
        (chat_id, user_id, _now() - 30 * 86400))
    return {"days": int(days_known), "msgs": int(msgs30), "pun": int(pun30)}


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


async def watch_set(chat_id: int, user_id: int, sig: str, flagged: bool,
                    score: int | None = None, card_score: int | None = None) -> None:
    """Состояние наблюдения. score/card_score трогаем, только если переданы."""
    await _db.execute(
        """INSERT INTO watch_profiles (chat_id, user_id, sig, flagged, score, score_ts,
                                       card_score)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(chat_id, user_id) DO UPDATE SET
               sig = excluded.sig,
               flagged = excluded.flagged,
               score = COALESCE(?, watch_profiles.score),
               score_ts = CASE WHEN ? IS NULL THEN watch_profiles.score_ts ELSE ? END,
               card_score = COALESCE(?, watch_profiles.card_score)""",
        (chat_id, user_id, sig, int(flagged), score or 0, _now() if score else 0,
         card_score or 0, score, score, _now(), card_score),
    )
    await _db.commit()


# ---------- доступ к боту (кого владелец пустил) ----------

async def access_add(user_id: int | None, username: str | None,
                     name: str | None = None) -> None:
    """Добавить в список доступа. name — имя, если его удалось узнать сразу.

    Без имени в списке висел голый ник: про человека, который боту ещё
    не писал, мы больше ничего не знаем.
    """
    uname = (username or None) and username.lower().lstrip("@")
    for r in await access_list():
        if user_id is not None and r["user_id"] == user_id:
            return
        if uname and r["username"] == uname:
            # знаем теперь id — привязываем: ник меняют, id нет
            if user_id is not None and r["user_id"] is None:
                await _db.execute(
                    "UPDATE access SET user_id = ?, name = COALESCE(name, ?) "
                    "WHERE id = ?", (user_id, name, r["id"]))
                await _db.commit()
            return
    await _db.execute(
        "INSERT INTO access (user_id, username, name, added) VALUES (?, ?, ?, ?)",
        (user_id, uname, name or None, _now()),
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


# ---------- сетки чатов ----------
#
# Сетка — это именованная группа чатов одного владельца, между которыми
# разъезжаются наказания. Сеток у владельца может быть несколько (лимит
# config.NET_LIMIT): чаты разных сообществ не должны делить баны.

async def nets_of(owner_id: int) -> list[aiosqlite.Row]:
    cur = await _db.execute(
        "SELECT * FROM nets WHERE owner_id = ? ORDER BY created", (owner_id,))
    return await cur.fetchall()


async def nets_all() -> list[aiosqlite.Row]:
    """Все сетки всех владельцев — это видит только владелец бота."""
    cur = await _db.execute("SELECT * FROM nets ORDER BY owner_id, created")
    return await cur.fetchall()


async def net_get(net_id: int | None) -> aiosqlite.Row | None:
    if not net_id:
        return None
    cur = await _db.execute("SELECT * FROM nets WHERE id = ?", (net_id,))
    return await cur.fetchone()


async def net_create(owner_id: int, title: str) -> int | None:
    """Завести сетку. None — упёрлись в лимит."""
    if len(await nets_of(owner_id)) >= config.NET_LIMIT:
        return None
    cur = await _db.execute(
        """INSERT INTO nets (owner_id, title, sync_mask, lift_mode, created)
           VALUES (?, ?, ?, 'any', ?)""",
        (owner_id, title.strip()[:40], config.NET_MASK_DEFAULT, _now()),
    )
    await _db.commit()
    return cur.lastrowid


async def net_delete(net_id: int) -> None:
    await _db.execute("UPDATE chats SET net_id = NULL WHERE net_id = ?", (net_id,))
    await _db.execute("DELETE FROM nets WHERE id = ?", (net_id,))
    await _db.commit()


async def net_set(net_id: int, field: str, value) -> None:
    if field not in ("sync_mask", "lift_mode", "title"):
        raise ValueError(field)
    await _db.execute(f"UPDATE nets SET {field} = ? WHERE id = ?", (value, net_id))
    await _db.commit()


async def net_chats(net_id: int) -> list[aiosqlite.Row]:
    cur = await _db.execute(
        "SELECT * FROM chats WHERE net_id = ? AND active = 1 ORDER BY added_at",
        (net_id,),
    )
    return await cur.fetchall()


async def net_assign(chat_id: int, net_id: int | None) -> None:
    await _db.execute("UPDATE chats SET net_id = ? WHERE chat_id = ?", (net_id, chat_id))
    await _db.commit()


async def net_of_chat(chat_id: int) -> aiosqlite.Row | None:
    """Сетка, в которой состоит чат. Чужую сетку не отдаём: если владелец чата
    сменился, старое членство считается недействительным."""
    ch = await get_chat(chat_id)
    if ch is None or not ch["net_id"]:
        return None
    net = await net_get(ch["net_id"])
    if net is None or net["owner_id"] != ch["owner_id"]:
        return None
    return net


async def net_peers(chat_id: int) -> list[aiosqlite.Row]:
    """Остальные чаты той же сетки — куда разъезжается наказание.

    Лог-чаты сюда не попадают: бот в них сидит, но модерировать там некого.
    """
    net = await net_of_chat(chat_id)
    if net is None:
        return []
    logs = {c["log_chat_id"] for c in await _log_chat_ids() if c["log_chat_id"]}
    return [c for c in await net_chats(net["id"])
            if c["chat_id"] != chat_id and c["chat_id"] not in logs]


async def _log_chat_ids() -> list[aiosqlite.Row]:
    cur = await _db.execute("SELECT log_chat_id FROM settings")
    return await cur.fetchall()


GLOBAL_LOG_KEY = "global_log"


async def global_log() -> int | None:
    """Общий лог-чат владельца бота: копия всех карточек со всех чатов."""
    raw = await kv_get(GLOBAL_LOG_KEY)
    return int(raw) if raw else None


async def set_global_log(chat_id: int | None) -> None:
    await kv_set(GLOBAL_LOG_KEY, str(chat_id) if chat_id else None)


async def table_counts() -> dict[str, int]:
    """Счётчики по таблицам для раздела «Состояние»."""
    out: dict[str, int] = {}
    for t in ("words", "whitelist", "triggers", "chat_cmds", "events", "users", "answers"):
        cur = await _db.execute(f"SELECT COUNT(*) AS c FROM {t}")
        out[t] = (await cur.fetchone())["c"]
    cur = await _db.execute(
        "SELECT COUNT(*) AS c FROM punishments WHERE active = 1 "
        "AND (until_ts IS NULL OR until_ts > ?)", (_now(),)
    )
    out["punishments_active"] = (await cur.fetchone())["c"]
    return out


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


# ---------- кэш ответов CAS ----------

async def cas_get(user_id: int, ttl_listed: int, ttl_clean: int) -> bool | None:
    """Что CAS отвечал про этого человека. None — не спрашивали или пора заново.

    Срок у ответов разный: «в списке» держим дольше, потому что оттуда почти
    не выходят, а «чист» перепроверяем чаще — сегодня чист, завтра попался.
    """
    cur = await _db.execute(
        "SELECT listed, ts FROM cas_cache WHERE user_id = ?", (user_id,))
    row = await cur.fetchone()
    if row is None:
        return None
    ttl = ttl_listed if row["listed"] else ttl_clean
    if _now() - row["ts"] > ttl:
        return None
    return bool(row["listed"])


async def cas_put(user_id: int, listed: bool) -> None:
    await _db.execute(
        """INSERT INTO cas_cache (user_id, listed, ts) VALUES (?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET listed = excluded.listed,
                                              ts = excluded.ts""",
        (user_id, 1 if listed else 0, _now()))
    await _db.commit()


async def cas_prune(older_than: int) -> int:
    """Выкинуть протухшие ответы, чтобы таблица не росла бесконечно."""
    cur = await _db.execute(
        "DELETE FROM cas_cache WHERE ts < ?", (_now() - older_than,))
    await _db.commit()
    return cur.rowcount or 0


async def cas_stats() -> dict:
    cur = await _db.execute(
        "SELECT listed, COUNT(*) AS n FROM cas_cache GROUP BY listed")
    out = {"listed": 0, "clean": 0}
    for r in await cur.fetchall():
        out["listed" if r["listed"] else "clean"] = r["n"]
    return out


# ---------- стартовый набор: просмотр и чистка ----------
#
# Набор общий на весь бот, поэтому и правит его только владелец бота: удалил
# пример — он пропал у всех чатов сразу.

def _like_escape(q: str) -> str:
    """Экранировать спецсимволы LIKE: в поиске это обычные знаки.

    В LIKE «%» значит «что угодно», «_» — «любой один символ». Без экранирования
    поиск по «%» находил весь набор, и кнопка «удалить найденное» стирала его
    целиком — мимо отдельного подтверждения на полную очистку. А «док_р» тихо
    цеплял и «докер», и «докор»: человек удалял не то, что видел.
    """
    for ch in ("\\", "%", "_"):
        q = q.replace(ch, "\\" + ch)
    return q


def _seed_where(label: str | None, q: str | None) -> tuple[str, list]:
    cond = ["chat_id = ?"]
    args: list = [SEED_CHAT]
    if label in ("spam", "ok"):
        cond.append("label = ?")
        args.append(label)
    if q:
        cond.append("text LIKE ? ESCAPE '\\' COLLATE NOCASE")
        args.append(f"%{_like_escape(q)}%")
    return " AND ".join(cond), args


async def seed_count(label: str | None = None, q: str | None = None) -> int:
    where, args = _seed_where(label, q)
    cur = await _db.execute(f"SELECT COUNT(*) FROM samples WHERE {where}", args)
    return (await cur.fetchone())[0] or 0


async def seed_page(label: str | None = None, q: str | None = None,
                    offset: int = 0, limit: int = 5) -> list[aiosqlite.Row]:
    where, args = _seed_where(label, q)
    cur = await _db.execute(
        f"""SELECT id, label, text, vec IS NOT NULL AS has_vec FROM samples
            WHERE {where} ORDER BY id LIMIT ? OFFSET ?""", (*args, limit, offset))
    return await cur.fetchall()


async def seed_delete(ids: list[int]) -> int:
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    cur = await _db.execute(
        f"DELETE FROM samples WHERE chat_id = ? AND id IN ({marks})",
        (SEED_CHAT, *ids))
    await _db.commit()
    return cur.rowcount or 0


async def seed_delete_where(label: str | None = None, q: str | None = None) -> int:
    """Удалить всё, что нашлось по фильтру. Пустой фильтр не чистит набор
    целиком: для этого есть отдельная кнопка со своим подтверждением."""
    if not label and not q:
        return 0
    where, args = _seed_where(label, q)
    cur = await _db.execute(f"DELETE FROM samples WHERE {where}", args)
    await _db.commit()
    return cur.rowcount or 0


async def seed_vec_count() -> int:
    cur = await _db.execute(
        "SELECT COUNT(*) FROM samples WHERE chat_id = ? AND vec IS NOT NULL",
        (SEED_CHAT,))
    return (await cur.fetchone())[0] or 0
