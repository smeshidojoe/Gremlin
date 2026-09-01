"""Еженедельная сводка активности чата из базы скраппера (data/chat_stats.db).

Раз в неделю (воскресенье) выбранному человеку уходит краткая выжимка + кнопка,
по которой формируется полный HTML-отчёт тем же генератором, что и в TGChatScrapper.
"""
import asyncio
import logging
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import config, db, utils

logger = logging.getLogger("gremlin.digest")


def tracked_chat() -> int | None:
    """Чат, по которому ведётся подробная статистика (одна база на один чат).

    Юзербот, сводки и HTML-отчёт работают только для него: тянуть большую базу
    с участниками и историей на каждый новый чат мы не хотим. Остальным чатам
    остаётся базовая статистика бота (раздел «📈 Статистика»).
    """
    from .stats_collect import tracked_chat_id
    return tracked_chat_id()


def _week_start() -> str:
    """Понедельник текущей недели (YYYY-MM-DD) по местному времени.

    Сводка считает календарную неделю, а не последние 7 суток: во вторник
    в ней два дня, в воскресенье — полная неделя.
    """
    now = utils.local_now()
    return (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")


def collect(stats_db: str) -> dict | None:
    """Сводка за 7 дней из базы скраппера. None — базы нет."""
    if not os.path.exists(stats_db):
        return None
    con = sqlite3.connect(f"file:{stats_db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        since = _week_start()
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        # сообщения за неделю по людям (каналы/анонимы с отрицательным id пропускаем)
        per_user = {
            r["user_id"]: r["m"]
            for r in con.execute(
                "SELECT user_id, SUM(msgs) AS m FROM daily WHERE day >= ? GROUP BY user_id",
                (since,),
            )
            if r["user_id"] > 0
        }
        people = {
            r["user_id"]: r
            for r in con.execute(
                """SELECT user_id, username, first_name, last_name FROM users
                   WHERE is_bot = 0 AND is_channel = 0 AND is_deleted = 0
                     AND is_member = 1 AND user_id > 0"""
            )
        }

        def label(uid: int) -> str:
            r = people.get(uid)
            if r is None:
                return f"id{uid}"
            name = " ".join(x for x in (r["first_name"], r["last_name"]) if x) or f"id{uid}"
            return f"{name} (@{r['username']})" if r["username"] else name

        top = sorted(
            ((uid, n) for uid, n in per_user.items() if uid in people),
            key=lambda x: -x[1],
        )[:3]
        silent_ids = [uid for uid in people if per_user.get(uid, 0) == 0]
        silent = sorted((label(uid) for uid in silent_ids), key=str.lower)
        # для кнопки «списком»: ник, если он есть, иначе имя с id — такой список
        # можно скопировать и скормить массовым операциям
        silent_raw = sorted(
            (f"@{people[uid]['username']}" if people[uid]["username"] else f"{label(uid)} — {uid}"
             for uid in silent_ids),
            key=str.lower,
        )
        now = utils.local_now()
        return {
            "chat_title": meta.get("chat_title", "чат"),
            "total": sum(per_user.values()),
            "top": [(label(uid), n) for uid, n in top],
            "silent": silent,
            "silent_raw": silent_raw,
            "members": len(people),
            "updated": utils.utc_iso_to_local(meta.get("updated_at")),
            "since": since,
            "days": now.weekday() + 1,          # сколько дней недели уже прошло
            "period": f"{datetime.fromisoformat(since):%d.%m} – {now:%d.%m}",
        }
    finally:
        con.close()


def render(d: dict) -> str:
    # Полная неделя бывает только в воскресенье — тогда сводка и уходит.
    # Открыли раздел среди недели — это предварительный срез, о чём и пишем.
    days = d.get("days", 7)
    full = days >= 7
    word = "день" if days == 1 else ("дня" if days < 5 else "дней")
    subtitle = (f"неделя {d.get('period', '')}" if full
                else f"предварительно · {d.get('period', '')} · прошло {days} {word}")
    lines = [
        f"<b>📊 Сводка за неделю</b> · {utils.esc(d['chat_title'])}",
        f"<i>{subtitle}</i>\n",
        f"💬 Сообщений: <b>{d['total']}</b>",
        f"👥 Участников: <b>{d['members']}</b>",
    ]
    if d["top"]:
        lines.append("\n<b>🏆 Топ-3 активных:</b>")
        for i, (who, n) in enumerate(d["top"], 1):
            lines.append(f"{i}. {utils.esc(who)} — {n}")
    silent = d["silent"]
    if not full:
        # среди недели список молчунов ничего не значит: за пару дней не написало
        # полчата. Показываем только счётчик, имена — в воскресной сводке.
        lines.append(
            f"\n🤐 Ещё не писали на этой неделе: <b>{len(silent)}</b> из {d['members']}"
            f"\n<i>поимённый список будет в воскресной сводке</i>"
        )
        if d["updated"]:
            lines.append(f"\n<i>данные собраны: {d['updated']}</i>")
        return utils.chunk("\n".join(lines))

    lines.append(f"\n<b>🤐 Молчали всю неделю ({len(silent)}):</b>")
    if silent:
        shown = silent[:40]
        lines.append(", ".join(utils.esc(s) for s in shown))
        if len(silent) > len(shown):
            lines.append(f"…и ещё {len(silent) - len(shown)}")
    else:
        lines.append("нет — писали все")
    if d["updated"]:
        lines.append(f"\n<i>данные собраны: {d['updated']}</i>")
    return utils.chunk("\n".join(lines))


def build_html(stats_db: str) -> bytes:
    """Полный HTML-отчёт (тот же генератор, что в TGChatScrapper)."""
    from .statsreport.export_html import build
    tmp = os.path.join(tempfile.gettempdir(), "gremlin_report.html")
    build(stats_db, tmp)
    with open(tmp, "rb") as f:
        data = f.read()
    try:
        os.remove(tmp)
    except OSError:
        pass
    return data


async def send_digest(bot: Bot, chat_id: int, to_user: int,
                      notify: bool = False) -> bool:
    """Показать сводку получателю. True — получилось.

    notify=False — правим прежнее сообщение на месте: так открытая сводка
    обновляется по кнопке, не засоряя личку.

    notify=True — присылаем новое, а старое убираем. Так работает еженедельная
    рассылка: правка сообщения проходит без уведомления, и человек про сводку
    попросту не узнавал — именно так она и «терялась» по воскресеньям.
    """
    from .. import userbot
    await userbot.refresh_members()          # состав перед подсчётом молчунов
    d = await asyncio.to_thread(collect, config.STATS_DB)
    if d is None:
        logger.warning("stats db not found: %s", config.STATS_DB)
        return False

    b = InlineKeyboardBuilder()
    b.button(text="📄 Отправить файл", callback_data=f"d:file:{chat_id}")
    b.button(text="📋 Молчуны списком", callback_data=f"d:silent:{chat_id}")
    b.button(text="✖️ Закрыть", callback_data=f"d:close:{chat_id}")
    b.adjust(1)
    kb = b.as_markup()
    text = render(d)

    key = f"digest_msg:{chat_id}"
    raw = await db.kv_get(key)
    if raw and notify:
        # старую сводку убираем: она уже неактуальна, а новая придёт отдельным
        # сообщением — с уведомлением, ради которого всё и затевалось
        prev_user, _, prev_msg = raw.partition(":")
        if prev_user == str(to_user) and prev_msg.isdigit():
            try:
                await bot.delete_message(to_user, int(prev_msg))
            except Exception:
                pass          # уже удалили или старше 48 часов
    elif raw:
        prev_user, _, prev_msg = raw.partition(":")
        if prev_user == str(to_user) and prev_msg.isdigit():
            try:
                await bot.edit_message_text(
                    text, chat_id=to_user, message_id=int(prev_msg), reply_markup=kb
                )
                await db.add_event(chat_id, "digest", f"сводка обновлена у {to_user}")
                return True
            except Exception:
                pass  # сообщение удалили или получатель сменился — пришлём новое
    try:
        sent = await bot.send_message(to_user, text, reply_markup=kb)
    except Exception:
        logger.warning("digest send to %s failed", to_user, exc_info=True)
        return False
    await db.kv_set(key, f"{to_user}:{sent.message_id}")
    await db.add_event(chat_id, "digest", f"недельная сводка отправлена юзеру {to_user}")
    return True


async def scheduler(bot: Bot) -> None:
    """Раз в час проверяем: воскресенье и сводка за эту неделю ещё не уходила.

    Состояние в kv — перезапуск бота не приводит к повторной отправке.
    """
    while True:
        try:
            now = utils.local_now()      # воскресенье по местному, не по UTC-серверу
            if now.weekday() == 6:
                stamp = f"{now.isocalendar().year}-{now.isocalendar().week}"
                only = tracked_chat()
                for ch in await db.all_chats(active_only=True):
                    if only is None or ch["chat_id"] != only:
                        continue                       # сводка живёт для одного чата
                    s = await db.get_settings(ch["chat_id"])
                    if not s.digest_to:
                        continue
                    key = f"digest_sent:{ch['chat_id']}"
                    if await db.kv_get(key) == stamp:
                        continue
                    if await send_digest(bot, ch["chat_id"], s.digest_to,
                                         notify=True):
                        await db.kv_set(key, stamp)
        except Exception:
            logger.warning("digest scheduler tick failed", exc_info=True)
        await asyncio.sleep(3600)
