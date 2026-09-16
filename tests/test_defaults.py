"""Значения по умолчанию для новых чатов и кому уходят карточки."""
import dataclasses
import sqlite3

import pytest

from gremlin import config, db
from gremlin.services import moderation

from conftest import CHAT, OWNER

LOG_CHAT, GLOBAL_LOG, FRESH = CHAT - 30, CHAT - 31, CHAT - 32


class LogBot:
    """Бот, который помнит, куда слал карточки."""

    id = 1

    def __init__(self):
        self.sent = []

    async def send_message(self, cid, text, **kw):
        self.sent.append(cid)
        import types
        return types.SimpleNamespace(message_id=len(self.sent))


@pytest.fixture
async def logs(chat):
    await db.set_setting(chat, "log_chat_id", LOG_CHAT)
    await db.set_global_log(GLOBAL_LOG)
    return chat


async def send(chat, bit=config.BIT_SUB):
    bot = LogBot()
    await moderation.send_card(bot, chat, bit, "карточка")
    return bot.sent


# ---------- глобальный лог слушает маску чата ----------

async def test_both_logs_get_enabled_card(logs):
    assert await send(logs) == [LOG_CHAT, GLOBAL_LOG]


async def test_disabled_type_goes_nowhere(logs):
    s = await db.get_settings(logs)
    await db.set_setting(logs, "card_mask", s.card_mask & ~config.BIT_SUB)
    assert await send(logs) == []
    assert await send(logs, config.BIT_BAN) == [LOG_CHAT, GLOBAL_LOG]


async def test_cards_off_silences_global_log_too(logs):
    await db.set_setting(logs, "cards_on", 0)
    assert await send(logs) == []


async def test_without_own_log_global_still_gets_it(logs):
    await db.set_setting(logs, "log_chat_id", None)
    assert await send(logs) == [GLOBAL_LOG]


# ---------- значения по умолчанию ----------

async def test_new_chat_gets_code_defaults(database):
    await db.get_settings(FRESH)
    s = await db.get_settings(FRESH)
    assert s.card_mask == db.Settings.card_mask and s.prof_photo_min == 97
    assert s.warns_punish == "mute"


def _old_settings_table() -> str:
    """Та же таблица настроек, но с умолчаниями, застрявшими в живой базе с
    первых версий. Берём схему из кода, чтобы колонки были все: часть из них
    миграции не добавляют, и запросы при старте на куцей таблице падают."""
    sql = db._SCHEMA[db._SCHEMA.index("CREATE TABLE IF NOT EXISTS settings("):]
    sql = sql[:sql.index(");") + 2]
    for now, before in ((f"card_mask       INTEGER NOT NULL DEFAULT {db.Settings.card_mask}",
                         "card_mask       INTEGER NOT NULL DEFAULT 255"),
                        ("prof_photo_min  INTEGER NOT NULL DEFAULT 97",
                         "prof_photo_min  INTEGER NOT NULL DEFAULT 85"),
                        ("warns_punish    TEXT    NOT NULL DEFAULT 'mute'",
                         "warns_punish    TEXT    NOT NULL DEFAULT 'ban'")):
        assert now in sql, now
        sql = sql.replace(now, before)
    return sql


async def test_defaults_written_explicitly_over_old_schema(tmp_path, monkeypatch):
    """Строку чата заводим значениями из кода: иначе новый чат молча получает
    умолчания старой схемы — маску 255, порог 85 и бан за варны."""
    path = tmp_path / "old.sqlite3"
    con = sqlite3.connect(path)
    con.executescript(_old_settings_table())
    con.commit()
    con.close()

    monkeypatch.setattr(config, "DB_PATH", str(path))
    db._db = None
    try:
        await db.init()
        await db.get_settings(FRESH)
        cur = await db._db.execute(
            "SELECT card_mask, prof_photo_min, warns_punish FROM settings "
            "WHERE chat_id = ?", (FRESH,))
        assert tuple(await cur.fetchone()) == (db.Settings.card_mask, 97, "mute")
    finally:
        await db.close()


def test_defaults_cover_every_setting():
    fields = {f.name for f in dataclasses.fields(db.Settings)} - {"chat_id"}
    assert set(db._SETTINGS_DEFAULTS) == fields


# ---------- разовая починка старых значений ----------

async def test_stale_values_fixed_once(chat):
    await db._db.execute(
        "UPDATE settings SET prof_photo_min = 85, warns_punish = 'ban'")
    await db._db.commit()
    assert await db.raise_photo_min() == 1
    assert await db.fix_warns_punish() == 1
    s = await db.get_settings(chat)
    assert (s.prof_photo_min, s.warns_punish) == (97, "mute")
    assert await db.raise_photo_min() == 0
    assert await db.fix_warns_punish() == 0


async def test_chosen_photo_threshold_is_kept(chat):
    await db.set_setting(chat, "prof_photo_min", 99)
    await db.raise_photo_min()
    assert (await db.get_settings(chat)).prof_photo_min == 99
