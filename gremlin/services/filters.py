"""Детекторы: тг-ссылки, стоп-слова (с кэшем regex), антифлуд."""
import re
import time
from collections import deque

from .. import db

# t.me/xxx, telegram.me/xxx, telegram.dog/xxx, tg://join, tg://resolve, t.me/+invite
_TG_LINK_RE = re.compile(
    r"(?:https?://)?(?:t(?:elegram)?\.(?:me|dog)/|tg://(?:join|resolve))"
    r"(\+?[\w\d_?=&/-]+)?",
    re.IGNORECASE,
)


# ссылка в распознанном тексте: entity там нет, Telegram его не размечал
_BARE_URL_RE = re.compile(
    r"(?:https?://|www\.)[\w\-.]+\.[a-z]{2,}(?:/[^\s]*)?", re.IGNORECASE)


def find_tg_links(message, extra: str = "") -> list[str]:
    """Все тг-ссылки в тексте/подписи/entity url. Свой чат и бот исключаются в вызывающем коде.

    extra — текст, распознанный в картинке или голосовом: для правил он такой
    же полноправный, как подпись, просто Telegram его не размечал.
    """
    found: list[str] = []
    texts = [message.text or message.caption or "", extra]
    for ent, ent_text in _entities(message):
        if ent.type == "text_link" and ent.url:
            texts.append(ent.url)
    for t in texts:
        for m in _TG_LINK_RE.finditer(t):
            found.append(m.group(0))
    return found


def _entities(message):
    text = message.text or message.caption or ""
    ents = (message.entities or []) + (message.caption_entities or [])
    for ent in ents:
        # extract_from корректно режет по UTF-16 оффсетам (эмодзи и т.п.)
        yield ent, ent.extract_from(text)


def find_ext_links(message, extra: str = "") -> list[str]:
    """Внешние (не телеграмные) ссылки. Берём из entity — Telegram размечает их сам,
    поэтому нет ложных срабатываний на «3.5» или «файл.txt».

    В распознанном тексте entity взять неоткуда, поэтому там ищем регуляркой
    и только явные адреса: со схемой или с www.
    """
    found: list[str] = []
    for m in _BARE_URL_RE.finditer(extra or ""):
        if not _TG_LINK_RE.search(m.group(0)):
            found.append(m.group(0))
    for ent, ent_text in _entities(message):
        if ent.type == "url":
            url = ent_text
        elif ent.type == "text_link" and ent.url:
            url = ent.url
        else:
            continue
        if not _TG_LINK_RE.search(url):  # телеграмные ловит отдельное правило
            found.append(url)
    return found


def mentions_in(message) -> list[str]:
    """@упоминания из entities (без @)."""
    result = []
    for ent, ent_text in _entities(message):
        if ent.type == "mention":
            result.append(ent_text.lstrip("@"))
    return result


def link_allowed(link: str, usernames: set[str], chat_ids: set[int]) -> bool:
    """Ссылка ведёт на «свои»: этот чат, привязанный канал или самого бота.

    Умеет оба формата: публичный t.me/username/123 и внутренний
    t.me/c/<id_без_-100>/123 — по нему как раз ходят ссылки на сообщения
    закрытого чата.
    """
    low = link.lower()
    for u in usernames:
        if u and (f"/{u.lower()}" in low or f"domain={u.lower()}" in low):
            return True
    for cid in chat_ids:
        internal = str(cid).lstrip("-")
        if internal.startswith("100"):
            internal = internal[3:]
        if internal and (f"/c/{internal}" in low or f"/{internal}/" in low):
            return True
    return False


# ---------- стоп-слова ----------

# chat_id -> compiled regex | None; сбрасывается при изменении списка
_word_cache: dict[int, re.Pattern | None] = {}


def invalidate_words(chat_id: int) -> None:
    _word_cache.pop(chat_id, None)


async def match_stopword(chat_id: int, text: str) -> str | None:
    """Вернуть найденное стоп-слово или None."""
    if chat_id not in _word_cache:
        rows = await db.words_list(chat_id)
        if not rows:
            _word_cache[chat_id] = None
        else:
            parts = []
            for r in rows:
                w = re.escape(r["word"])
                if r["mode"] == "stem":
                    parts.append(rf"{w}\w*")  # слово + любые окончания
                else:
                    parts.append(w)
            _word_cache[chat_id] = re.compile(
                r"(?<!\w)(" + "|".join(parts) + r")(?!\w)", re.IGNORECASE | re.UNICODE
            )
    rx = _word_cache[chat_id]
    if rx is None:
        return None
    m = rx.search(text)
    return m.group(1) if m else None


# ---------- антифлуд ----------

# (chat_id, user_id) -> deque[ts]
_flood: dict[tuple[int, int], deque] = {}


def flood_hit(chat_id: int, user_id: int, max_msgs: int, window: int) -> bool:
    """True если юзер превысил лимит сообщений в окне."""
    key = (chat_id, user_id)
    now = time.monotonic()
    dq = _flood.get(key)
    if dq is None:
        if len(_flood) > 50000:
            _flood.clear()
        dq = _flood[key] = deque(maxlen=64)
    dq.append(now)
    recent = [t for t in dq if now - t <= window]
    if len(recent) >= max_msgs:
        dq.clear()  # чтобы не наказывать повторно за тот же залп
        return True
    return False
