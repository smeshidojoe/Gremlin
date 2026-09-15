"""Проверка статуса: кто человек, где встречался, что на нём висит и что было.

Своё берём из базы (наказания, варны, прощения, сообщения, лог), живое — у
Telegram: статус в каждом чате, срок мута или бана прямо сейчас, описание
профиля и прикреплённый канал. Дату вступления Bot API не отдаёт, её узнаёт
только юзербот, и то в чатах, где сидит сам.

Смотрим только чаты того, кто спрашивает, и из них — только те, где человек
хоть как-то встречался: состоит, забанен, писал, упоминается в логе. Пустые
строки «в чате нет» по двадцати чатам ничего не говорят.
"""
import asyncio
import re
import time
from datetime import datetime

from aiogram import Bot

from .. import config, db, utils
from . import adm_cache

_ID = re.compile(r"^\d{1,15}$")
_NAME = re.compile(
    r"^(?:@|(?:https?://)?(?:t\.me|telegram\.me)/)?([A-Za-z][A-Za-z0-9_]{3,31})/?$",
    re.IGNORECASE)

# счётчики в карточке: вид наказания -> подпись
COUNT_LABELS = (
    ("mute", "🔇 Муты"),
    ("ban", "⛔ Баны"),
    ("kick", "👢 Кики"),
    ("banchan", "📛 Баны канала"),
)
_KIND_WORD = {"ban": "бан", "mute": "мут", "banchan": "бан канала", "kick": "кик"}
TEXT_LIMIT = 4000

PROMPT = (
    "<b>🔎 Проверка статуса</b>\n\n"
    "Пришлите id, @username, ссылку t.me/… или перешлите сообщение человека.\n"
    "Покажу, в каких ваших чатах он встречался и как давно, что на нём висит "
    "сейчас, сколько наказаний было и последние события."
)


async def parse_target(bot: Bot, text: str | None,
                       message=None) -> tuple[int | None, str]:
    """Кого проверять. Вернуть (id, текст ошибки)."""
    origin = getattr(message, "forward_origin", None) if message is not None else None
    if origin is not None:
        user = getattr(origin, "sender_user", None)
        if user is not None:
            return user.id, ""
        if getattr(origin, "sender_user_name", None):
            return None, ("Человек скрыл аккаунт в пересылке — по такому "
                          "сообщению его не узнать. Пришлите id или @username.")
        return None, "Это пересылка из канала или чата, а не от человека."
    raw = (text or "").strip()
    if _ID.match(raw):
        return int(raw), ""
    m = _NAME.match(raw)
    if not m:
        return None, ("Не понял. Нужен id, @username, ссылка t.me/… "
                      "или пересланное сообщение.")
    from . import resolve
    uid, _name = await resolve.by_username(bot, m.group(1))
    if uid is None:
        return None, (f"Не нашёл @{m.group(1)}. По нику бот узнаёт тех, кого "
                      "видел в чатах, или кого находит юзербот. Пришлите id.")
    if uid < 0:
        return None, "Это канал или группа, а не человек."
    return uid, ""


def _until(member) -> int | None:
    """Срок мута или бана из ответа Telegram. None — навсегда."""
    raw = getattr(member, "until_date", None)
    if isinstance(raw, datetime):
        raw = int(raw.timestamp())
    return raw if raw and raw > 0 else None


def _term(ts: int | None) -> str:
    return f"до {utils.fmt_ts(ts)}" if ts else "навсегда"


def _state(m) -> tuple[str, bool, str | None]:
    """(подпись, состоит ли в чате, какое наказание видит Telegram)."""
    status = m.status
    if status == "creator":
        return "👑 владелец", True, None
    if status == "administrator":
        return "🛡 админ", True, None
    if status == "member":
        return "✅ состоит", True, None
    if status == "kicked":
        return f"⛔ забанен {_term(_until(m))}", False, "ban"
    if status == "restricted":
        inside = bool(getattr(m, "is_member", False))
        muted = not getattr(m, "can_send_messages", True)
        what = "🔇 мут" if muted else "⚠️ ограничен"
        tail = "" if inside else " · в чате не состоит"
        return f"{what} {_term(_until(m))}{tail}", inside, "mute" if muted else None
    return "🚪 в чате нет", False, None


def _date(ts: int) -> str:
    return utils._local(ts).strftime("%d.%m.%Y")


def _day_ts(day: int) -> int:
    """Номер местных суток из msg_stats -> начало этих суток."""
    return day * 86400 - config.TZ_OFFSET * 3600


def _day_date(day: int) -> str:
    return _date(_day_ts(day))


def _age(seconds: int) -> str:
    days = max(0, seconds) // 86400
    if days < 1:
        return "меньше дня"
    if days < 31:
        return f"{days} {utils.plural(days, 'день', 'дня', 'дней')}"
    months = days // 30
    if months < 12:
        return f"{months} мес"
    years, months = divmod(months, 12)
    return (f"{years} {utils.plural(years, 'год', 'года', 'лет')}"
            + (f" {months} мес" if months else ""))


async def _chat(bot: Bot, chat, uid: int) -> dict:
    cid = chat["chat_id"]
    out = {"chat_id": cid, "title": chat["title"] or str(cid),
           "state": "❓ Telegram не ответил", "lines": [], "user": None,
           "tg": False}
    try:
        m = await bot.get_chat_member(cid, uid)
    except Exception:
        m = None
    in_chat, tg_kind = False, None
    if m is not None:
        out["state"], in_chat, tg_kind = _state(m)
        adm_cache.note_member(cid, uid, in_chat)   # доверию ниже не спрашивать снова
        out["user"] = getattr(m, "user", None)
        # «в чате нет» — ответ Telegram про любого человека на свете,
        # показывать чат только из-за него незачем
        out["tg"] = m.status != "left"
    lines = out["lines"]
    if in_chat:
        from .. import userbot
        joined = await userbot.joined_at(cid, uid)
        if joined:
            lines.append(f"📅 в чате с {_date(joined)} "
                         f"({_age(int(time.time()) - joined)})")
        s = await db.get_settings(cid)
        if s.trust_on:
            from . import trust
            try:
                lines.append("🎖 доверие: "
                             + trust.label(await trust.level(bot, cid, uid, s)))
            except Exception:
                pass
    facts = await db.user_chat_facts(cid, uid)
    if facts["msgs"]:
        n = facts["msgs"]
        first, last = _day_date(facts["first_day"]), _day_date(facts["last_day"])
        span = first if first == last else f"{first} – {last}"
        lines.append(f"✉️ {n} {utils.plural(n, 'сообщение', 'сообщения', 'сообщений')}"
                     f" · {span}")
    watch = await db.watch_get(cid, uid)
    if watch is not None and watch["flagged"]:
        lines.append(f"👁 была карточка наблюдения ({watch['card_score'] or 0} очков)")
    for p in facts["active"]:
        why, _swapped = utils.short_reason(p["reason"])
        lines.append(f"🔨 в базе: {_KIND_WORD.get(p['kind'], p['kind'])} "
                     f"{_term(p['until_ts'])} — {utils.chunk(why, 80)}")
        # в базе висит, а Telegram не видит: сняли руками в обход бота или
        # наказание не применилось. Кнопка «Снять» в списке тогда ничего не снимет
        if m is not None and p["kind"] in ("ban", "mute") and tg_kind != p["kind"]:
            lines.append("⚠️ Telegram этого наказания не видит")
    scopes = await db.wl_scopes_for(cid, uid, None)
    if scopes:
        lines.append("🕊 вайтлист: " + ", ".join(
            config.WL_SCOPE_LABELS.get(s, s) for s in sorted(scopes)))
    return out


async def _about(bot: Bot, user_id: int) -> list[str]:
    """Описание профиля и прикреплённый канал — там обычно и лежит реклама."""
    from . import profile as prof_svc
    data = await prof_svc.fetch(bot, user_id)
    if not data:
        return []
    out = []
    if data["bio"]:
        out.append(f"📝 {utils.chunk(data['bio'], 150)}")
    if data["channel_title"] or data["channel_username"]:
        out.append("📣 канал: " + (data["channel_title"] or "")
                   + (f" (@{data['channel_username']})" if data["channel_username"] else ""))
    return out


async def collect(bot: Bot, user_id: int, chats, first: int | None = None) -> dict:
    """Всё о человеке по списку чатов. Текущий чат — первым."""
    chats = sorted(chats, key=lambda c: c["chat_id"] != first)
    ids = [c["chat_id"] for c in chats]
    rows = list(await asyncio.gather(*(_chat(bot, c, user_id) for c in chats)))
    # имя и ник берём у Telegram: в базе они такие, какими бот видел их
    # последний раз, а человек мог давно переименоваться
    tg = next((r["user"] for r in rows if r["user"] is not None), None)
    seen = await db.user_seen_chats(user_id, ids)
    shown = []
    for r in rows:
        del r["user"]
        if r.pop("tg") or r["chat_id"] in seen:
            shown.append(r)

    known = await db.get_user(user_id)
    name = ((getattr(tg, "full_name", None) if tg else None)
            or (known["first_name"] if known else None) or str(user_id))
    username = ((tg.username if tg else None)
                or (known["username"] if known else None))

    stats = await db.user_status_counts(user_id, ids)
    counts = [{"kind": k, "label": label, "n": stats["kinds"][k]}
              for k, label in COUNT_LABELS if stats["kinds"].get(k)]
    if stats["warns"]:
        counts.append({"kind": "warn", "label": "⚠️ Варны", "n": stats["warns"]})
    if stats["forgiven"]:
        counts.append({"kind": "forgiven", "label": "🕊 Прощён",
                       "n": stats["forgiven"]})

    facts = []
    if stats["first_day"]:
        # Только по чатам спрашивающего: first_seen и last_seen в базе общие на
        # человека, и через них владелец видел активность в чужих чатах
        first, last = _day_date(stats["first_day"]), _day_date(stats["last_day"])
        facts.append(f"✉️ пишет в ваших чатах с {first} · последнее сообщение {last}")
    if stats["cas"]:
        facts.append("🌐 в общем списке спамеров (CAS)")

    titles = {c["chat_id"]: c["title"] or str(c["chat_id"]) for c in chats}
    events = []
    for e in await db.user_events(user_id, ids):
        icon, label, body = utils.event_parts(e["kind"], e["text"])
        # имя с id в каждой строке лога про одного человека — лишний шум
        body = body.replace(f" ({user_id})", "")
        events.append({
            "when": utils._local(e["ts"]).strftime("%d.%m %H:%M"),
            "chat": utils.chunk(titles.get(e["chat_id"], str(e["chat_id"])), 24),
            "icon": icon, "label": label, "body": utils.chunk(body, 90),
        })

    return {
        "user_id": user_id, "name": name, "username": username,
        "premium": bool(getattr(tg, "is_premium", False)),
        "link": (f"https://t.me/{username}" if username
                 else f"tg://user?id={user_id}"),
        "about": await _about(bot, user_id),
        "facts": facts, "counts": counts, "chats": shown, "events": events,
    }


def _tree(items: list[str]) -> list[str]:
    return [("└ " if i == len(items) - 1 else "├ ") + utils.esc(x)
            for i, x in enumerate(items)]


def render(d: dict) -> str:
    """Карточка для меню бота."""
    ident = f"🆔 <code>{d['user_id']}</code>"
    if d["username"]:
        ident += f" · @{utils.esc(d['username'])}"
    if d["premium"]:
        ident += " · ⭐ Premium"
    lines = ["<b>🔎 Проверка статуса</b>", "",
             f"👤 <b>{utils.name_link(d['user_id'], d['name'], d['username'])}</b>",
             ident]
    lines += [utils.esc(x) for x in d["about"] + d["facts"]]
    lines += ["", "<b>⚖️ Наказания в ваших чатах</b>",
              " · ".join(f"{c['label']}: <b>{c['n']}</b>" for c in d["counts"])
              or "Не было."]
    if d["chats"]:
        for c in d["chats"]:
            lines += ["", f"💬 <b>{utils.esc(c['title'])}</b>"]
            lines += _tree([c["state"]] + c["lines"])
    else:
        lines += ["", "💬 Ни в одном из ваших чатов не встречался."]
    text = "\n".join(lines)

    # Лог — в конце и свёрнутой цитатой, как сообщение в карточках. Режем его,
    # а не карточку: обрезка посреди HTML сломала бы разметку всего сообщения
    events = [utils.esc(f"{e['when']} · {e['chat']} · {e['icon']} {e['label']}: "
                        f"{e['body']}") for e in d["events"]]
    while events:
        tail = ("\n\n<b>📜 Последние события</b>\n<blockquote expandable>"
                + "\n".join(events) + "</blockquote>")
        if len(text) + len(tail) <= TEXT_LIMIT:
            return text + tail
        events.pop()
    return text
