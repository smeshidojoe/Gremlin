"""Вход в чат только для подписчиков канала.

Работает через заявки на вступление: человек жмёт «Вступить», Telegram шлёт
боту заявку, бот смотрит, подписан ли он на канал, и решает. Чтобы заявки
приходили, в самой группе должна быть включена проверка новых участников —
это настройка Telegram, включает её владелец группы.

Спросить «подписан ли» может только админ канала, поэтому бот должен быть
админом и там.

Одна приятная особенность Telegram: вместе с заявкой приходит личный чат с
человеком, и боту разрешено туда написать, даже если тот ему никогда не писал.
Так что отказ можно объяснить, а не оставлять молча.
"""
import logging
import time

from .. import config, db

logger = logging.getLogger("gremlin.subscribe")


class SubError(Exception):
    """Спросить про подписку не вышло. Текст — уже человеческий."""

# статусы, при которых считаем человека подписанным
_IN = ("creator", "administrator", "member")

# придержанные заявки: (chat_id, user_id) -> когда подали.
# Нужны кнопке «я подписался»: одобрить можно только висящую заявку, а
# спрашивать Telegram, есть ли она, API не позволяет.
_pending: dict[tuple[int, int], float] = {}


def remember(chat_id: int, user_id: int) -> None:
    now = time.time()
    if len(_pending) > 10000:
        for key in [k for k, ts in _pending.items()
                    if now - ts > config.SUB_PENDING_TTL]:
            del _pending[key]
    _pending[(chat_id, user_id)] = now


def waiting(chat_id: int, user_id: int) -> bool:
    ts = _pending.get((chat_id, user_id))
    return ts is not None and time.time() - ts <= config.SUB_PENDING_TTL


def forget(chat_id: int, user_id: int) -> None:
    """Человек вошёл — забываем о нём всё, включая счёт заявок."""
    _pending.pop((chat_id, user_id), None)
    _tries.pop((chat_id, user_id), None)


def forget_wait(chat_id: int, user_id: int) -> None:
    """Заявка отработана, но человек в чат не попал.

    Счёт заявок при этом не сбрасываем: он и нужен, чтобы отличить «подал
    один раз» от «долбится по кругу». Со сбросом счётчик всегда показывал
    единицу, и карточка уходила на каждую заявку — ровно то, от чего
    защищались.
    """
    _pending.pop((chat_id, user_id), None)


# (чат, человек) -> (когда началось окно, сколько заявок за него)
_tries: dict[tuple[int, int], tuple[float, int]] = {}


def note_request(chat_id: int, user_id: int) -> int:
    """Засчитать заявку и вернуть, какая она по счёту за окно.

    Считаем в памяти: после перезапуска счёт начинается заново, и это
    правильнее, чем тащить долбёжку недельной давности в базу.
    """
    now = time.time()
    if len(_tries) > 10000:
        for key in [k for k, (ts, _n) in _tries.items()
                    if now - ts > config.SUB_REPEAT_WINDOW]:
            del _tries[key]
    started, count = _tries.get((chat_id, user_id), (now, 0))
    if now - started > config.SUB_REPEAT_WINDOW:
        started, count = now, 0
    count += 1
    _tries[(chat_id, user_id)] = (started, count)
    return count


def card_due(count: int) -> bool:
    """Показывать ли карточку на эту по счёту заявку."""
    return count == 1 or count == config.SUB_REPEAT_ALERT


async def target_channel(bot, chat_id: int, s) -> int | None:
    """На какой канал смотрим: заданный руками или привязанный к чату."""
    if s.sub_chat_id:
        return s.sub_chat_id
    from . import adm_cache
    linked, _uname, _title = await adm_cache.linked_chat(bot, chat_id)
    return linked or None


async def channel_label(bot, chat_id: int, s) -> str:
    """Как назвать канал в меню."""
    target = await target_channel(bot, chat_id, s)
    if not target:
        return "не задан и не привязан"
    try:
        ch = await bot.get_chat(target)
        name = ch.title or str(target)
        return f"{name} (@{ch.username})" if ch.username else name
    except Exception:
        return str(target)


async def channel_link(bot, target: int) -> str | None:
    """Ссылка, по которой можно подписаться."""
    try:
        ch = await bot.get_chat(target)
        if ch.username:
            return f"https://t.me/{ch.username}"
        link = getattr(ch, "invite_link", None)
        if link:
            return link
        return await bot.export_chat_invite_link(target)
    except Exception as e:
        logger.debug("ссылку на канал %s не достали: %s", target, e)
        return None


# Почему проверка не сработала: chat_id -> (когда, текст для человека).
# Держим, чтобы сказать владельцу и показать в разделе: без этого сломанная
# настройка выглядела как «функция не работает» без единого объяснения.
_problem: dict[int, tuple[float, str]] = {}
# когда в последний раз писали владельцу — чтобы не повторяться каждую заявку
_warned: dict[int, float] = {}

# что означает отказ Telegram, человеческим языком
_REASONS = (
    ("member list is inaccessible",
     "бот не администратор в канале — спросить, подписан ли человек, "
     "он не может"),
    ("chat_admin_required",
     "боту не хватает прав в канале"),
    ("chat not found",
     "канал не найден: его удалили или бота из него исключили"),
    ("bot is not a member",
     "бота нет в канале"),
    ("user_not_participant",
     "канал не отдаёт список участников"),
)


def _human(err: str) -> str:
    low = str(err).lower()
    for needle, text in _REASONS:
        if needle in low:
            return text
    return f"Telegram отказал: {err}"


def note_problem(chat_id: int, err: str) -> str:
    """Запомнить, почему проверка не работает. Возвращает объяснение."""
    text = _human(err)
    _problem[chat_id] = (time.time(), text)
    return text


def problem(chat_id: int) -> str | None:
    """Свежая поломка настройки или None. Старше суток не показываем:
    её могли уже починить, а мы бы пугали зря."""
    hit = _problem.get(chat_id)
    if hit is None or time.time() - hit[0] > config.SUB_WARN_TTL:
        return None
    return hit[1]


def clear_problem(chat_id: int) -> None:
    _problem.pop(chat_id, None)
    _warned.pop(chat_id, None)


def warn_due(chat_id: int) -> bool:
    """Пора ли снова сказать владельцу. Раз в сутки на чат."""
    now = time.time()
    if now - _warned.get(chat_id, 0) < config.SUB_WARN_TTL:
        return False
    _warned[chat_id] = now
    return True


async def subscribed(bot, channel_id: int, user_id: int) -> bool | None:
    """Подписан ли человек. None — спросить не вышло.

    Разница между «нет» и «не знаю» тут решает судьбу заявки: на «не знаю»
    отклонять нельзя, иначе выпавший канал или снятые у бота права закроют
    вход всем подряд.
    """
    try:
        member = await bot.get_chat_member(channel_id, user_id)
    except Exception as e:
        logger.warning("подписку на %s для %s не проверить: %s",
                       channel_id, user_id, e)
        raise SubError(str(e)) from e
    status = getattr(member, "status", None)
    status = getattr(status, "value", status)
    if status == "restricted":
        # ограниченный в канале всё ещё может им быть — смотрим на флаг
        return bool(getattr(member, "is_member", False))
    return status in _IN


# Ответ «есть ли доступ к каналу»: chat_id -> (когда, текст, всё ли хорошо).
# Спрашиваем при открытии раздела, поэтому кэшируем на минуту — иначе каждое
# нажатие «назад-вперёд» стоило бы запроса в Telegram.
_access: dict[int, tuple[float, str, bool]] = {}
ACCESS_TTL = 60


async def access_state(bot, chat_id: int, s) -> tuple[str, bool]:
    """(что показать человеку, всё ли в порядке).

    Проверяем не подписку кого-то, а собственный доступ бота к каналу: без
    прав админа он не сможет спросить вообще ни про кого, и функция будет
    молчать. Раньше это выяснялось только после первой заявки.
    """
    hit = _access.get(chat_id)
    now = time.time()
    if hit is not None and now - hit[0] < ACCESS_TTL:
        return hit[1], hit[2]

    target = await target_channel(bot, chat_id, s)
    if not target:
        out = ("канал не задан и не привязан к чату", False)
    else:
        name = await channel_label(bot, chat_id, s)
        try:
            me = (await bot.me()).id
            await bot.get_chat_member(target, me)
            out = (f"бот админ в «{name}», проверка работает", True)
        except Exception as e:
            out = (f"«{name}»: {_human(str(e))}", False)
            note_problem(chat_id, str(e))
    _access[chat_id] = (now, out[0], out[1])
    return out


def forget_access(chat_id: int) -> None:
    """Канал поменяли — прошлый ответ больше ничего не значит."""
    _access.pop(chat_id, None)
    clear_problem(chat_id)


async def note_event(chat_id: int, kind: str, user, extra: str = "") -> None:
    await db.add_event(chat_id, "sub",
                       f"{kind}: {user.full_name} ({user.id}) {extra}".strip())
