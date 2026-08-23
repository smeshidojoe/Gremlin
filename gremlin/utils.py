"""Мелкие помощники: время, форматирование, упоминания."""
import html
import re
import time
from datetime import datetime, timedelta, timezone

from aiogram.types import (
    ChatAdministratorRights, KeyboardButton, KeyboardButtonRequestChat, ReplyKeyboardMarkup,
)

_DUR_RE = re.compile(r"^(\d+)\s*([mмhчdд])$", re.IGNORECASE)

_UNIT_MIN = {"m": 1, "м": 1, "h": 60, "ч": 60, "d": 1440, "д": 1440}


def parse_duration(token: str) -> int | None:
    """'5m'/'1h'/'2d' (или м/ч/д) -> минуты. None если не время."""
    m = _DUR_RE.match(token.strip())
    if not m:
        return None
    return int(m.group(1)) * _UNIT_MIN[m.group(2).lower()]


def fmt_minutes(minutes: int) -> str:
    """Минуты -> '5м' / '2ч' / '3д'. 0 -> 'навсегда'."""
    if minutes <= 0:
        return "навсегда"
    if minutes < 60:
        return f"{minutes}м"
    if minutes < 1440:
        h = minutes // 60
        rem = minutes % 60
        return f"{h}ч" + (f" {rem}м" if rem else "")
    d = minutes // 1440
    rem_h = (minutes % 1440) // 60
    return f"{d}д" + (f" {rem_h}ч" if rem_h else "")


def until_ts(minutes: int) -> int | None:
    """Минуты -> unix-время окончания. 0/None -> None (навсегда)."""
    if not minutes:
        return None
    return int(time.time()) + minutes * 60


def fmt_ts(ts: int | None) -> str:
    if not ts:
        return "навсегда"
    return _local(ts).strftime("%d.%m.%Y %H:%M")


# ---------- местное время ----------
#
# Сутки и недели в статистике должны совпадать с тем, что видит человек в чате.
# Считать по UTC нельзя: «сегодня» начиналось бы в 03:00 по Москве.

def _tz() -> timezone:
    from . import config
    return timezone(timedelta(hours=config.TZ_OFFSET))


def local_now() -> datetime:
    return datetime.now(_tz())


def _local(ts: float) -> datetime:
    """Unix-время -> местное. Через TZ_OFFSET, а не через часовой пояс машины:
    вне докера он может быть любым, и даты в меню разъехались бы со статистикой."""
    return datetime.fromtimestamp(ts, _tz())


def day_num(ts: float | None = None) -> int:
    """Номер местных суток — ключ для msg_stats.day."""
    from . import config
    base = ts if ts is not None else time.time()
    return int(base + config.TZ_OFFSET * 3600) // 86400


def day_str(dt: datetime | None = None) -> str:
    """Местная дата YYYY-MM-DD — ключ для daily.day в базе статистики."""
    d = dt.astimezone(_tz()) if dt else local_now()
    return d.strftime("%Y-%m-%d")


def utc_iso_to_local(iso: str | None) -> str:
    """ISO-время из базы (UTC) -> человеку в его часовом поясе."""
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_tz()).strftime("%d.%m %H:%M")
    except ValueError:
        return iso[:16].replace("T", " ")


def rel_time(ts: int) -> str:
    """«5 мин назад» / «2 ч назад» / «вчера 22:10» / «12.07 10:00»."""
    now = int(time.time())
    d = now - ts
    if d < 60:
        return "только что"
    if d < 3600:
        return f"{d // 60} мин назад"
    if d < 21600:  # до 6 часов — относительное, дальше уже путает
        return f"{d // 3600} ч назад"
    today = day_num(now)
    then = _local(ts)
    if day_num(ts) == today:
        return then.strftime("сегодня %H:%M")
    if day_num(ts) == today - 1:
        return then.strftime("вчера %H:%M")
    return then.strftime("%d.%m %H:%M")


# kind события -> эмодзи и человеческая подпись
EVENT_KINDS = {
    "join": ("👋", "Вошёл"),
    "leave": ("🚪", "Вышел"),
    "warn": ("⚠️", "Варн"),
    "manual": ("👮", "Модерация"),
    "admin_action": ("👮", "Действие админа"),
    "watch": ("👁", "Наблюдение"),
    "captcha": ("🤖", "Капча"),
    "report": ("🚨", "Жалоба"),
    "anon": ("📛", "Аноним"),
    "card": ("🪪", "Карточка"),
    "bot": ("⚙️", "Бот"),
    "инлайн-бот": ("🤖", "Инлайн-бот"),
    "ссылка на сторонний чат": ("🔗", "Ссылка"),
    "упоминание стороннего чата": ("🔗", "Упоминание чата"),
    "пересылка": ("🔗", "Пересылка"),
    "пересылка из канала": ("🔗", "Пересылка"),   # старые записи в логе
    "стоп-слово": ("🧨", "Стоп-слово"),
    "флуд": ("🌊", "Флуд"),
}

# английские хвосты в текстах событий -> по-русски
_EVENT_WORDS = {
    "banchan:": "бан канала:",
    "unban:": "разбан:",
    "unmute:": "размут:",
    "ban:": "бан:",
    "mute:": "мут:",
    "suspect:": "подозрение:",
    "delete:": "удаление:",
    " by ": " · кем: ",
}


def event_line(kind: str, text: str, ts: int, chat_title: str | None = None) -> str:
    """Одна строка лога в читаемом виде."""
    icon, label = EVENT_KINDS.get(kind, ("•", kind))
    body = text or ""
    for en, ru in _EVENT_WORDS.items():
        body = body.replace(en, ru)
    where = f" · {esc(chat_title)}" if chat_title else ""
    return f"{icon} <b>{label}</b> · {rel_time(ts)}{where}\n    {esc(body)}"


def mention(user_id: int, name: str | None, username: str | None = None) -> str:
    """HTML-упоминание. username в приоритете (кликабельно без allow)."""
    label = html.escape(name or username or str(user_id))
    if username:
        return f"@{html.escape(username)}"
    return f'<a href="tg://user?id={user_id}">{label}</a>'


def plural(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение по числу: 1 чат, 2 чата, 5 чатов."""
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def has_premium_emoji(text: str | None) -> bool:
    """Есть ли в сохранённом тексте премиум-эмодзи.

    Telegram отдаёт их отдельной сущностью, а aiogram разворачивает в тег
    <tg-emoji>. Если тега нет — значит отправитель прислал обычный эмодзи:
    ставить премиум-эмодзи умеют только люди с подпиской.
    """
    return "<tg-emoji" in (text or "")


def esc(text: str | None) -> str:
    return html.escape(text or "")


# ответы Telegram на «отвечать уже некому»: сообщение удалили, пока бот думал
# или разгребал бэклог. Это не ошибка бота, в лог сыпать нечего.
_GONE_HINTS = (
    "message to be replied not found",
    "message to reply not found",
    "message_id_invalid",
    "message to delete not found",
    "replied message not found",
)


def msg_gone(e: Exception) -> bool:
    text = str(e).lower()
    return any(h in text for h in _GONE_HINTS)


def chunk(text: str, limit: int = 3900) -> str:
    """Обрезка под лимит Telegram."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


_STORIES = dict(can_post_stories=False, can_edit_stories=False, can_delete_stories=False)
# права, которые нужны боту для модерации/логов (запрашиваются при добавлении)
_BOT_RIGHTS = ChatAdministratorRights(
    is_anonymous=False, can_manage_chat=True, can_delete_messages=True,
    can_manage_video_chats=False, can_restrict_members=True, can_promote_members=False,
    can_change_info=False, can_invite_users=True, can_pin_messages=True, **_STORIES,
)
# юзер должен иметь как минимум те же права + promote (иначе не сможет добавить бота админом)
_USER_RIGHTS = ChatAdministratorRights(
    is_anonymous=False, can_manage_chat=True, can_delete_messages=True,
    can_manage_video_chats=False, can_restrict_members=True, can_promote_members=True,
    can_change_info=False, can_invite_users=True, can_pin_messages=True, **_STORIES,
)


def request_chat_kb(text: str = "📍 Выбрать чат") -> ReplyKeyboardMarkup:
    """Нативный выбор чата: показывает чаты, которыми владеет юзер; при выборе Telegram
    сам предложит добавить бота с нужными админ-правами (если его там нет)."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(
            text=text,
            request_chat=KeyboardButtonRequestChat(
                request_id=1, chat_is_channel=False, request_title=True,
                user_administrator_rights=_USER_RIGHTS,
                bot_administrator_rights=_BOT_RIGHTS,
            ),
        )]],
        resize_keyboard=True, one_time_keyboard=True,
    )
