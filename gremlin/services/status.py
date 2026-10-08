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


# Права на отправку, которые снимают по одному: поле Telegram -> что пропало.
# Закреплять, приглашать, менять инфо не берём — их часто закрывают всему
# чату, и строка у каждого второго твердила бы про них
_SEND_RIGHTS = (
    ("can_send_photos", "фото"),
    ("can_send_videos", "видео"),
    ("can_send_audios", "аудио"),
    ("can_send_voice_notes", "голосовых"),
    ("can_send_video_notes", "кружков"),
    ("can_send_documents", "файлов"),
    ("can_send_polls", "опросов"),
    ("can_send_other_messages", "стикеров/GIF/инлайна"),
    ("can_add_web_page_previews", "превью ссылок"),
)


def _limits(m) -> str:
    """Что именно снято у того, кто писать может. «Ограничен» без пояснения
    читался как наказание, а там бывает одна галка «стикеры и GIF»."""
    off = [word for field, word in _SEND_RIGHTS if getattr(m, field, True) is False]
    if not off:
        return "⚠️ ограничен в правах"
    if len(off) == len(_SEND_RIGHTS):
        return "⚠️ только текст"
    return "⚠️ без " + ", ".join(off)


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
        what = "🔇 мут" if muted else _limits(m)
        tail = "" if inside else " · в чате не состоит"
        return f"{what} {_term(_until(m))}{tail}", inside, "mute" if muted else None
    return "🚪 в чате нет", False, None


def _date(ts: int) -> str:
    return utils._local(ts).strftime("%d.%m.%Y")


def _day_ts(day: int) -> int:
    """Номер местных суток из msg_stats -> начало этих суток."""
    return utils.day_ts(day)


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


_GONE = ("member not found", "PARTICIPANT_ID_INVALID")


async def _chat(bot: Bot, chat, uid: int) -> dict:
    cid = chat["chat_id"]
    out = {"chat_id": cid, "title": chat["title"] or str(cid),
           "state": "❓ Telegram не ответил", "lines": [], "user": None,
           "tg": False}
    try:
        m = await bot.get_chat_member(cid, uid)
    except Exception as e:
        m = None
        # так Telegram отвечает про удалённый аккаунт: человека он не знает вовсе
        if any(s in str(e) for s in _GONE):
            out["state"] = "👻 Telegram его не находит — похоже, аккаунт удалён"
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
        why, swapped = utils.short_reason(p["reason"])
        shown = utils.shown_kind(p["kind"], p["reason"])
        if swapped and tg_kind == "ban":
            # Telegram видит бан, а выдавали мут — так и пишем, с тихой строкой
            out["state"] = f"🔇 мут {_term(_until(m))}"
        lines.append(f"🔨 в базе: {_KIND_WORD.get(shown, shown)} "
                     f"{_term(p['until_ts'])} — {utils.chunk(why, 80)}")
        if swapped:
            lines.append(utils.SWAP_NOTE)
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


# откуда бот знает о человеке, если ни сообщений, ни наказаний в чате нет
_SEEN_ONLY = (
    ("watch_profiles", "👁 попадал в наблюдение (реакция или профиль), карточки не было"),
    ("verdicts", "⚖️ есть записи теневой оценки"),
    ("punishments", "🔨 были наказания, сейчас сняты"),
    ("warns", "⚠️ получал варны"),
    ("forgiven", "🕊 был прощён"),
    ("events", "📜 упоминается в журнале событий"),
)


HISTORY = 10        # строк в «Последних событиях»
# Запись лога в эти секунды от выдачи наказания в том же чате — про него же:
# лог пишут сразу за наказанием. Сама запись тогда не нужна, строка из
# списка наказаний то же самое говорит понятнее
_SAME_CASE = 10
# что рядом с наказанием стоит само по себе: вошёл, заявка, капча, жалоба
_OWN_EVENTS = {"join", "leave", "sub", "captcha", "report"}
_KIND_ICON = {"mute": "🔇", "ban": "⛔", "kick": "👢", "banchan": "📛"}
# кто сделал: «| by 123», «юзером 123» — в конец строки и именем
_ACTOR = re.compile(r"\s*\|?\s*(?:\bby|юзером)\s+(\d+)")
_TAGS = re.compile(r"<[^>]+>")


def _when(ts: int) -> str:
    return utils._local(ts).strftime("%d.%m %H:%M")


async def _clean(text: str | None, user_id: int, name) -> str:
    """Строка лога про человека — без него самого.

    Лог писался годами в разном виде: «бан: Имя (id) — причина | by 123»,
    «игра: причина — id», «бан-рулетка: mute для id». Человек на странице
    один, его имя и id в каждой строке — шум, а админ числом ничего не говорит.
    """
    text = text or ""
    actors = [int(m.group(1)) for m in _ACTOR.finditer(text)]
    text, _swapped = utils.split_swap(_ACTOR.sub("", text))
    own = text.find(f" ({user_id})")
    if own >= 0:
        colon = text.find(": ")
        start = colon + 2 if 0 <= colon < own else 0
        text = text[:start] + text[own + len(f" ({user_id})"):]
    text = re.sub(rf"(?:\s+—|\s+для)?\s*(?<!\d){user_id}(?!\d)", "", text)
    text = re.sub(r":\s*—\s*", ": ", text).strip(" —:")
    _icon, _label, text = utils.event_parts("", text)
    text = re.sub(r"^удаление:\s*", "🗑 удалено · ", text)
    for uid in actors:
        text += (" · " if text else "") + f"кем: {await name(uid)}"
    return text


async def _history(user_id: int, ids: list[int], titles: dict) -> list[dict]:
    """Последние события: наказания — из списка наказаний и в том же виде,
    с пометкой, чем кончились; остальное — из лога, почищенное."""
    now = int(time.time())
    names: dict[int, str] = {}

    async def name(uid: int) -> str:
        if uid not in names:
            names[uid] = await db.user_label(uid)
        return names[uid]

    def chat(cid: int) -> str:
        return utils.chunk(titles.get(cid, str(cid)), 24)

    out = []
    given = await db.user_punishments(user_id, ids, HISTORY)
    for p in given:
        shown = utils.shown_kind(p["kind"], p["reason"])
        how, ended = db.punishment_end(p, now)
        why, _swapped = utils.short_reason(_TAGS.sub("", p["reason"] or ""))
        body = [utils.ended_label(how, ended) if how else _term(p["until_ts"]), why]
        if p["by_id"]:
            body.append(f"кем: {await name(p['by_id'])}")
        word = _KIND_WORD.get(shown, shown)
        out.append({"ts": p["created"], "when": _when(p["created"]),
                    "chat": chat(p["chat_id"]), "icon": _KIND_ICON.get(shown, "🔨"),
                    "label": word[:1].upper() + word[1:], "ended": how is not None,
                    "body": utils.chunk(" · ".join(body), 90)})
    for e in await db.user_events(user_id, ids, HISTORY * 2):
        if e["kind"] not in _OWN_EVENTS and any(
                p["chat_id"] == e["chat_id"] and abs(p["created"] - e["ts"]) <= _SAME_CASE
                for p in given):
            continue
        icon, label, _body = utils.event_parts(e["kind"], "")
        out.append({"ts": e["ts"], "when": _when(e["ts"]), "chat": chat(e["chat_id"]),
                    "icon": icon, "label": label, "ended": False,
                    "body": utils.chunk(await _clean(e["text"], user_id, name), 90)})
    out.sort(key=lambda r: r["ts"], reverse=True)
    for r in out:
        del r["ts"]
    return out[:HISTORY]


async def collect(bot: Bot, user_id: int, chats, first: int | None = None) -> dict:
    """Всё о человеке по списку чатов. Текущий чат — первым."""
    chats = sorted(chats, key=lambda c: c["chat_id"] != first)
    ids = [c["chat_id"] for c in chats]
    rows = list(await asyncio.gather(*(_chat(bot, c, user_id) for c in chats)))
    # имя и ник берём у Telegram: в базе они такие, какими бот видел их
    # последний раз, а человек мог давно переименоваться
    tg = next((r["user"] for r in rows if r["user"] is not None), None)
    seen = await db.user_seen_why(user_id, ids)
    shown = []
    for r in rows:
        del r["user"]
        if r.pop("tg") or r["chat_id"] in seen:
            got = seen.get(r["chat_id"], set())
            if "spam_profile" in got:
                r["lines"].append("🧪 профиль записан спамом в этом чате")
            # Строк нет, а чат в списке: человек тут не писал и не наказан.
            # Без пояснения такой чат выглядел ошибкой — говорим, откуда он
            if not r["lines"]:
                r["lines"] += [text for key, text in _SEEN_ONLY if key in got]
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
    if await db.seed_has_user(user_id):
        facts.append("🧪 профиль в спам-базе")

    titles = {c["chat_id"]: c["title"] or str(c["chat_id"]) for c in chats}
    events = await _history(user_id, ids, titles)

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
        # Чаты добавляем, пока влезают: человек из двадцати чатов иначе
        # выводил текст за лимит Telegram, и карточка не открывалась вовсе.
        # Первый (текущий) — всегда, хвост сводим в одну строку
        size = len("\n".join(lines))
        for i, c in enumerate(d["chats"]):
            block = ["", f"💬 <b>{utils.esc(c['title'])}</b>"] + _tree(
                [c["state"]] + c["lines"])
            more = len(d["chats"]) - i
            room = TEXT_LIMIT - (80 if more > 1 else 0)
            if i and size + len("\n".join(block)) + 1 > room:
                lines += ["", f"💬 …и ещё {more} "
                          f"{utils.plural(more, 'чат', 'чата', 'чатов')} — "
                          "полностью в панели"]
                break
            lines += block
            size += len("\n".join(block)) + 1
    else:
        lines += ["", "💬 Ни в одном из ваших чатов не встречался."]
    text = "\n".join(lines)

    # Лог — в конце и свёрнутой цитатой, как сообщение в карточках. Режем его,
    # а не карточку: обрезка посреди HTML сломала бы разметку всего сообщения
    events = [utils.esc(f"{e['when']} · {e['chat']} · {e['icon']} {e['label']}"
                        + (f": {e['body']}" if e["body"] else "")) for e in d["events"]]
    while events:
        tail = ("\n\n<b>📜 Последние события</b>\n<blockquote expandable>"
                + "\n".join(events) + "</blockquote>")
        if len(text) + len(tail) <= TEXT_LIMIT:
            return text + tail
        events.pop()
    return text
