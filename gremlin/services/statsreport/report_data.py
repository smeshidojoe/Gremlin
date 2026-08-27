"""Чтение собранной статистики из SQLite + расчёт производных метрик."""

import sqlite3
from datetime import datetime, timedelta, timezone


def _parse(dt):
    if not dt:
        return None
    try:
        return datetime.fromisoformat(dt)
    except ValueError:
        return None


def load_meta(db):
    con = sqlite3.connect(db)
    rows = dict(con.execute("SELECT key, value FROM meta").fetchall())
    con.close()
    return rows


def load_rows(db):
    """Возвращает (rows, meta). Одна строка = один участник или экс-участник."""
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    now = datetime.now(timezone.utc)
    d7 = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    d30 = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    d90 = (now - timedelta(days=90)).strftime("%Y-%m-%d")

    WEEKS = 52
    week0 = now - timedelta(weeks=WEEKS)
    windows, active_days, weeks = {}, {}, {}
    for r in con.execute("SELECT user_id, day, msgs FROM daily"):
        uid, day, m = r["user_id"], r["day"], r["msgs"]
        w = windows.setdefault(uid, [0, 0, 0])
        if day >= d7:
            w[0] += m
        if day >= d30:
            w[1] += m
        if day >= d90:
            w[2] += m
        active_days[uid] = active_days.get(uid, 0) + 1

        dt = _parse(day)
        if dt:
            idx = (dt.replace(tzinfo=timezone.utc) - week0).days // 7
            if 0 <= idx < WEEKS:
                weeks.setdefault(uid, [0] * WEEKS)[idx] += m

    chat_username = meta.get("chat_username", "")
    chat_id = meta.get("chat_id", "")

    def link(msg_id):
        if not msg_id:
            return ""
        if chat_username:
            return f"https://t.me/{chat_username}/{msg_id}"
        if chat_id:
            return f"https://t.me/c/{chat_id}/{msg_id}"
        return ""

    rows = []
    q = """SELECT u.*, t.msgs, t.chars, t.media, t.replies, t.reactions,
                  t.service_msgs, t.first_date, t.last_date, t.last_id
           FROM users u LEFT JOIN totals t ON t.user_id = u.user_id"""
    for r in con.execute(q):
        uid = r["user_id"]
        msgs = r["msgs"] or 0
        last = _parse(r["last_date"])
        joined = _parse(r["joined_date"])
        w = windows.get(uid, [0, 0, 0])

        if r["is_channel"]:
            role = "Аноним/канал"
        elif r["is_deleted"]:
            role = "Удалённый аккаунт"
        elif r["is_bot"]:
            role = "Бот"
        elif r["is_admin"]:
            role = "Админ"
        elif not r["is_member"]:
            role = "Уже вышел"
        else:
            role = "Участник"

        name = " ".join(x for x in (r["first_name"], r["last_name"]) if x) or f"id{uid}"
        rows.append({
            "user_id": uid,
            "username": ("@" + r["username"]) if r["username"] else "",
            "name": name,
            "role": role,
            "is_admin": bool(r["is_admin"]),
            "is_bot": bool(r["is_bot"]),
            "is_member": bool(r["is_member"]),
            "joined": joined.strftime("%Y-%m-%d") if joined else "",
            "days_in_chat": (now - joined).days if joined else "",
            "msgs": msgs,
            "first_msg": _parse(r["first_date"]).strftime("%Y-%m-%d") if r["first_date"] else "",
            "last_msg": last.strftime("%Y-%m-%d %H:%M") if last else "",
            "days_silent": (now - last).days if last else "",
            "active_days": active_days.get(uid, 0),
            "m7": w[0], "m30": w[1], "m90": w[2],
            "media": r["media"] or 0,
            "replies": r["replies"] or 0,
            "reactions": r["reactions"] or 0,
            "avg_len": round((r["chars"] or 0) / msgs, 1) if msgs else 0,
            "last_online": r["last_online"] or "",
            "last_link": link(r["last_id"]),
            "weeks": weeks.get(uid, []),
        })

    con.close()
    rows.sort(key=lambda x: (-x["msgs"], x["name"].lower()))
    return rows, meta


