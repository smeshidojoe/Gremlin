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
    return face_join(getattr(user, "full_name", "") or "",
                     getattr(user, "username", None), data)


def face_join(name: str, username: str | None, data: dict | None) -> str:
    """Та же строка личности, но из готовых частей — для набора сборщика.

    Вид у неё должен быть ровно тот, каким бот проверяет живой профиль: иначе
    пример в наборе и проверяемый профиль отличаются оформлением, и сходство
    выходит ниже настоящего.
    """
    username = (username or "").strip().lstrip("@")
    name = (name or "").strip()
    head = f"{name} @{username}".strip() if username else name
    tail = text_of(data)
    if head and tail:
        return f"{head} · {tail}"
    return head or tail


# Поля формы профиля в сборщике — в том порядке, в каком они идут в строке,
# и как они называются в копилке (samples.data) и в выгрузке.
FACE_FIELDS = ("Имя", "Ник", "О себе", "Канал", "Описание канала")
FACE_KEYS = dict(zip(FACE_FIELDS, ("name", "username", "bio", "channel", "channel_desc")))


def form_to_data(fields: dict, extra: list[str] | tuple = ()) -> dict:
    """Поля формы {"Имя": …} -> поля копилки {"name": …}; свободные строки — в extra."""
    out = {FACE_KEYS[k]: v for k, v in fields.items() if k in FACE_KEYS and v}
    if out.get("username"):
        out["username"] = out["username"].strip().lstrip("@")
    if extra:
        out["extra"] = list(extra)
    return out


def face_of_data(d: dict) -> str:
    """Строка личности из полей копилки: name, username, bio, channel, channel_desc.

    Одна сборка на всех — форму сборщика, загрузку выгрузки и пересборку
    старых записей: строка должна выходить той же, что у живой проверки.
    """
    head = face_join(d.get("name", ""), d.get("username"),
                     {"bio": d.get("bio", ""), "channel_title": d.get("channel", ""),
                      "channel_desc": d.get("channel_desc", "")})
    return " · ".join(p for p in (head, *(d.get("extra") or ())) if p)


def fields_of(user, data: dict | None) -> dict:
    """Поля живого профиля по отдельности — то, из чего собрана face_text.

    Строка нужна модели, поля — всему остальному: пересобрать строку под
    другую модель, выгрузить набор, понять, что именно было в профиле.
    """
    data = data or {}
    out = {"name": getattr(user, "full_name", "") or "",
           "username": getattr(user, "username", None) or "",
           "bio": data.get("bio") or "", "channel": data.get("channel_title") or "",
           "channel_desc": data.get("channel_desc") or ""}
    if data:
        out["photo"] = bool(data.get("photo_id"))
    return out


def face_of_fields(fields: dict, extra: list[str] | tuple = ()) -> str:
    """Строка личности из полей формы: {"Имя": …, "Ник": …, …} + свободные строки."""
    return face_of_data(form_to_data(fields, extra))


def unlabel(text: str) -> str | None:
    """Старая запись формы «Имя: Анна · Ник: @anna · …» -> «Анна @anna · …».

    None — это не старая запись формы, переделывать нечего.
    """
    fields, free = {}, []
    for part in (text or "").split(" · "):
        key, sep, val = part.partition(": ")
        if sep and key in FACE_FIELDS:
            fields[key] = val.strip()
        else:
            free.append(part)
    return face_of_fields(fields, free) if fields else None


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
