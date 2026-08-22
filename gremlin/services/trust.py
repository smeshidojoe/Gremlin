"""Доверие: строгость правил зависит от того, кто пишет.

Идея простая — гостя и человека, который два года сидит в чате, нельзя мерить
одной линейкой. Уровень считается по своей же базе: сколько человек знаком чату,
сколько написал за месяц, были ли у него наказания. Запросов в Telegram не
добавляется, кроме одного «состоит ли в чате» — и тот берётся из кэша.

Уровень ничего не запрещает и не разрешает сам по себе. Он только двигает
пороги наблюдения и смягчает наказание тем, кто давно свой.
"""
import logging
import time

from .. import config, db

logger = logging.getLogger("gremlin.trust")

GUEST, NEW, KNOWN, VETERAN = 0, 1, 2, 3

# (chat_id, user_id) -> (уровень, когда посчитали)
_cache: dict[tuple[int, int], tuple[int, float]] = {}
TTL = 600          # 10 минут: за это время человек не станет ветераном


def _prune(now: float) -> None:
    if len(_cache) > 10000:
        for key in [k for k, (_, ts) in _cache.items() if now - ts > TTL]:
            del _cache[key]


async def level(bot, chat_id: int, user_id: int, s=None) -> int:
    """Уровень доверия человека в этом чате."""
    now = time.time()
    hit = _cache.get((chat_id, user_id))
    if hit is not None and now - hit[1] < TTL:
        return hit[0]

    from . import adm_cache
    s = s or await db.get_settings(chat_id)
    lvl = await _compute(bot, chat_id, user_id, s, adm_cache)
    _prune(now)
    _cache[(chat_id, user_id)] = (lvl, now)
    return lvl


async def _compute(bot, chat_id: int, user_id: int, s, adm_cache) -> int:
    # админы и вайтлист — сразу максимум: их правила и так не трогают
    if user_id in config.ADMIN_IDS:
        return VETERAN
    if user_id in await adm_cache.chat_admin_ids(bot, chat_id):
        return VETERAN
    if await db.wl_scopes_for(chat_id, user_id, None):
        return VETERAN
    if not await adm_cache.is_member(bot, chat_id, user_id):
        return GUEST            # комментатор под постом канала, в чате не состоит

    facts = await db.trust_facts(chat_id, user_id)
    if facts["pun"]:
        return NEW              # наказание за месяц сбрасывает в новички
    if facts["days"] >= config.TRUST_VET_DAYS and facts["msgs"] >= config.TRUST_VET_MSGS:
        return VETERAN
    if facts["days"] >= s.trust_days and facts["msgs"] >= s.trust_msgs:
        return KNOWN
    return NEW


def invalidate(chat_id: int, user_id: int) -> None:
    """Сбросить кэш — после наказания уровень меняется сразу."""
    _cache.pop((chat_id, user_id), None)


def watch_thresholds(s, lvl: int) -> tuple[int, int]:
    """Пороги наблюдения с поправкой на доверие."""
    if not s.trust_on:
        return s.watch_suspect, s.watch_ban
    factor = config.TRUST_WATCH_FACTOR.get(lvl, 1.0)
    ban = int(s.watch_ban * factor) if s.watch_ban else 0
    return int(s.watch_suspect * factor), ban


# наказание по убыванию строгости — смягчаем сдвигом вправо
_LADDER = ["ban", "mute", "delete"]


def soften(kind: str, lvl: int, s, bit: int = 0) -> str:
    """Смягчить наказание своим: бан -> мут -> удаление.

    bit — какая проверка сработала: смягчаем только отмеченные в подменю.
    Гостя и новичка не трогаем — для них настройки и так пишутся строгими.
    """
    if not s.trust_on or not s.trust_soften or kind not in _LADDER:
        return kind
    if bit and not (s.trust_mask & bit):
        return kind
    steps = config.TRUST_SOFTEN.get(lvl, 0)
    if not steps:
        return kind
    return _LADDER[min(_LADDER.index(kind) + steps, len(_LADDER) - 1)]


def label(lvl: int) -> str:
    return config.TRUST_LABELS.get(lvl, str(lvl))
