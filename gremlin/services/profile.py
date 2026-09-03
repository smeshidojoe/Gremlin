"""Профиль человека целиком: описание, прикреплённый канал, аватарка.

Зачем. Есть тип спама, против которого всё остальное бессильно: аккаунт пишет
под постом канала обычную реплику по теме — «теперь картинка такая же сочная,
как ты» — и уходит. Имя обычное, текст настоящий, нейрофильтру не за что
зацепиться, и он прав. Вся реклама лежит в описании профиля и в прикреплённом
канале, куда мы никогда не смотрели.

Telegram отдаёт это обычным getChat, юзербот не нужен: описание, канал (а у
канала — его собственное описание) и file_id аватарки.

Кэш держим короткий и только в памяти. Такие аккаунты одноразовые: забанили —
второй раз не увидим, и хранить их описания неделями в базе незачем. Кэш нужен
ровно чтобы не спрашивать по разу на каждое сообщение из одной очереди.
"""
import logging
import time

from .. import config

logger = logging.getLogger("gremlin.profile")

# user_id -> (когда спросили, данные или None)
_cache: dict[int, tuple[float, dict | None]] = {}


def _prune(now: float) -> None:
    if len(_cache) > config.PROFILE_CACHE_MAX:
        for uid in [u for u, (ts, _) in _cache.items()
                    if now - ts > config.PROFILE_TTL]:
            del _cache[uid]
        # всё ещё много — значит пришли разом; чистим целиком, не жалко
        if len(_cache) > config.PROFILE_CACHE_MAX:
            _cache.clear()


async def fetch(bot, user_id: int) -> dict | None:
    """Профиль человека или None, если спросить не вышло.

    Два запроса в худшем случае: сам человек и его прикреплённый канал. Второй
    только если канал есть — а он есть далеко не у всех.
    """
    now = time.monotonic()
    hit = _cache.get(user_id)
    if hit is not None and now - hit[0] < config.PROFILE_TTL:
        return hit[1]

    data: dict | None = None
    try:
        ch = await bot.get_chat(user_id)
        data = {
            "bio": (getattr(ch, "bio", None) or "").strip(),
            "channel_title": "",
            "channel_desc": "",
            "channel_username": "",
            "photo_id": "",
        }
        photo = getattr(ch, "photo", None)
        if photo is not None:
            data["photo_id"] = photo.big_file_id or photo.small_file_id or ""
        personal = getattr(ch, "personal_chat", None)
        if personal is not None:
            data["channel_title"] = (personal.title or "").strip()
            data["channel_username"] = (personal.username or "").strip()
            try:
                full = await bot.get_chat(personal.id)
                data["channel_desc"] = (getattr(full, "description", None) or "").strip()
            except Exception:
                # канал закрыт или удалён — название всё равно оставляем
                pass
    except Exception as e:
        # чужой профиль спрашивать не всегда можно, и это не повод шуметь
        logger.debug("профиль %s не отдался: %s", user_id, e)

    _prune(now)
    _cache[user_id] = (now, data)
    return data


async def photo_bytes(bot, data: dict | None) -> bytes | None:
    """Скачать аватарку. Байты кладём в тот же кэш: смотреть их будут один раз,
    но сообщений от человека может прийти несколько подряд."""
    if not data or not data.get("photo_id"):
        return None
    if "photo_raw" in data:
        return data["photo_raw"]
    raw = None
    try:
        f = await bot.get_file(data["photo_id"])
        buf = await bot.download_file(f.file_path)
        raw = buf.read() if hasattr(buf, "read") else bytes(buf)
    except Exception as e:
        logger.debug("аватарка не скачалась: %s", e)
    data["photo_raw"] = raw
    return raw


def text_of(data: dict | None) -> str:
    """Всё текстовое из профиля одной строкой — её и проверяем правилами.

    Склеиваем описание, название канала и описание канала: для правил это
    обычный текст, и работают по нему те же стоп-слова, фразы и модель, что
    и по сообщениям. Ничего нового изобретать не нужно.
    """
    if not data:
        return ""
    parts = [data.get("bio", ""), data.get("channel_title", ""),
             data.get("channel_desc", "")]
    return " · ".join(p for p in parts if p)


def face_text(user, data: dict | None) -> str:
    """Личность целиком одной строкой: имя, ник и всё из профиля.

    Ею и сравниваем с теми, за кого уже банили, ею же и запоминаем при бане —
    иначе в копилке лежали бы одни голые имена, а спрашивали бы мы описаниями,
    и сравнение работало бы вполсилы.
    """
    who = getattr(user, "full_name", "") or ""
    uname = getattr(user, "username", None)
    head = f"{who} @{uname}" if uname else who
    tail = text_of(data)
    return f"{head} · {tail}" if tail else head


def describe(data: dict | None) -> str:
    """Человекочитаемо для карточки: что именно у него в профиле."""
    if not data:
        return ""
    lines = []
    if data.get("bio"):
        lines.append(f"📝 О себе: {data['bio']}")
    if data.get("channel_title"):
        name = data["channel_title"]
        if data.get("channel_username"):
            name += f" (@{data['channel_username']})"
        lines.append(f"📣 Канал в профиле: {name}")
    if data.get("channel_desc"):
        lines.append(f"📄 Описание канала: {data['channel_desc']}")
    return "\n".join(lines)


def forget(user_id: int) -> None:
    _cache.pop(user_id, None)


def cached() -> int:
    return len(_cache)
