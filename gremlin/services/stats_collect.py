"""Дозапись статистики чата в базу скраппера (data/chat_stats.db).

Схема та же, что у TGChatScrapper: users / totals / daily / meta — поэтому
собранная им история продолжает пополняться, а недельные сводки видят всё вместе.
"""
import logging
import sqlite3
from datetime import datetime, timezone

from .. import config, utils

logger = logging.getLogger("gremlin.stats")


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(config.STATS_DB, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def record(user_id: int, username: str | None, first_name: str | None,
           last_name: str | None, is_bot: bool, msg_id: int, text_len: int,
           has_media: bool, is_reply: bool, when: datetime | None = None) -> None:
    """Учесть одно сообщение. Вызывать в отдельном потоке (sqlite синхронный)."""
    dt = when or datetime.now(timezone.utc)
    day = utils.day_str(dt)          # сутки местные, а не UTC
    iso = dt.isoformat()
    try:
        con = _con()
    except Exception:
        logger.warning("stats db open failed", exc_info=True)
        return
    try:
        # is_member НЕ трогаем: факт сообщения не доказывает, что человек всё ещё
        # в чате (мог написать и выйти). Состав определяет только save_members().
        con.execute(
            """INSERT INTO users(user_id, username, first_name, last_name, is_bot,
                                 is_deleted, is_admin, is_member, is_channel)
               VALUES(?,?,?,?,?,0,0,0,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 username=excluded.username,
                 first_name=excluded.first_name,
                 last_name=excluded.last_name""",
            (user_id, username, first_name, last_name, int(is_bot), int(user_id < 0)),
        )
        con.execute(
            """INSERT INTO totals(user_id, msgs, chars, media, replies, reactions,
                                  service_msgs, first_date, first_id, last_date, last_id)
               VALUES(?,1,?,?,?,0,0,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 msgs=totals.msgs+1,
                 chars=totals.chars+excluded.chars,
                 media=totals.media+excluded.media,
                 replies=totals.replies+excluded.replies,
                 last_date=excluded.last_date,
                 last_id=CASE WHEN excluded.last_id > COALESCE(totals.last_id, 0)
                              THEN excluded.last_id ELSE totals.last_id END""",
            (user_id, text_len, int(has_media), int(is_reply), iso, msg_id, iso, msg_id),
        )
        con.execute(
            """INSERT INTO daily(user_id, day, msgs, chars) VALUES(?,?,1,?)
               ON CONFLICT(user_id, day) DO UPDATE SET
                 msgs=daily.msgs+1, chars=daily.chars+excluded.chars""",
            (user_id, day, text_len),
        )
        con.execute(
            "INSERT INTO meta(key,value) VALUES('updated_at',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (iso,),
        )
        con.commit()
    except Exception:
        logger.warning("stats record failed for %s", user_id, exc_info=True)
    finally:
        con.close()


def save_members(rows: list[tuple], present: set[int]) -> None:
    """Записать состав чата. Вышедшим снимаем is_member — иначе они навсегда
    останутся «Участник» и будут висеть в списке молчунов."""
    try:
        con = _con()
    except Exception:
        logger.warning("stats db open failed", exc_info=True)
        return
    try:
        con.executemany(
            """INSERT INTO users(user_id, username, first_name, last_name, is_bot,
                                 is_deleted, is_admin, is_member, is_channel,
                                 joined_date, last_online)
               VALUES(?,?,?,?,?,?,?,1,0,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 username=excluded.username, first_name=excluded.first_name,
                 last_name=excluded.last_name, is_bot=excluded.is_bot,
                 is_deleted=excluded.is_deleted, is_admin=excluded.is_admin,
                 is_member=1,
                 joined_date=COALESCE(excluded.joined_date, users.joined_date),
                 last_online=excluded.last_online""",
            rows,
        )
        gone = [
            (r[0],) for r in con.execute("SELECT user_id FROM users WHERE is_member = 1")
            if r[0] not in present
        ]
        if gone:
            con.executemany(
                "UPDATE users SET is_member = 0, is_admin = 0 WHERE user_id = ?", gone
            )
        con.execute(
            "INSERT INTO meta(key,value) VALUES('updated_at',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (datetime.now(timezone.utc).isoformat(),),
        )
        con.commit()
        logger.info("состав сохранён: %s в чате, %s вышли", len(rows), len(gone))
    except Exception:
        logger.warning("save_members failed", exc_info=True)
    finally:
        con.close()


def tracked_chat_id() -> int | None:
    """chat_id, по которому собрана база (из meta). Нужен, чтобы писать только его."""
    try:
        con = _con()
    except Exception:
        return None
    try:
        row = con.execute("SELECT value FROM meta WHERE key='chat_id'").fetchone()
        if not row or not row[0]:
            return None
        raw = str(row[0]).lstrip("-")
        # скраппер хранит внутренний id без префикса -100
        return int(f"-100{raw}") if not raw.startswith("100") else int(f"-{raw}")
    except Exception:
        return None
    finally:
        con.close()
