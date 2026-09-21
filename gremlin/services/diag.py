"""Диагностика в отдельный файл diag.log.

Только в файл: ни в вывод докера, ни в bot.log эти строки не идут. Пишем
редкое — долгие обновления и судьбу правил под постами, — чтобы по «бот
промолчал» было видно, где именно он промолчал, и при этом не засорять
общий лог строкой на каждое сообщение.
"""
import contextvars
import logging
import time
from logging.handlers import RotatingFileHandler

from aiogram import BaseMiddleware

from .. import config

log = logging.getLogger("gremlin.diag")
log.propagate = False

# Время запросов к Telegram внутри одного обновления: [секунды, запросов,
# самый долгий метод, его секунды]. Фоновые задачи из обновления наследуют
# тот же список — их запросы после записи строки уже ни на что не влияют.
_api: contextvars.ContextVar[list | None] = contextvars.ContextVar("diag_api", default=None)


def setup() -> None:
    if log.handlers:
        return
    handler = RotatingFileHandler(config.DIAG_PATH, maxBytes=config.DIAG_MAX,
                                  backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


_steps: contextvars.ContextVar[dict | None] = contextvars.ContextVar("diag_steps",
                                                                      default=None)


def note(text: str, *args) -> None:
    log.info(text, *args)


class step:
    """Замер куска работы: `with diag.step("аватарка"):`.

    Нужен, чтобы в строке про долгое обновление было видно не только «своё
    3.5 с», но и чьё именно: нейрофильтра, классификатора аватарок или базы.
    Вне обновления ничего не делает.
    """

    __slots__ = ("name", "start")

    def __init__(self, name: str):
        self.name = name
        self.start = 0.0

    def __enter__(self):
        self.start = time.monotonic()
        return self

    def __exit__(self, *exc):
        bag = _steps.get()
        if bag is None:
            return False
        spent, count = bag.get(self.name, (0.0, 0))
        bag[self.name] = (spent + time.monotonic() - self.start, count + 1)
        return False


def _describe(update) -> str:
    kind = getattr(update, "event_type", "?")
    ev = getattr(update, "event", None)
    chat = getattr(ev, "chat", None) if ev is not None else None
    out = kind
    if chat is not None:
        out += f" · чат {chat.id}"
    what = getattr(ev, "content_type", None)
    if what:
        out += f" · {what}"
    if getattr(ev, "is_automatic_forward", False):
        out += " · пост канала"
    return out


class SlowUpdates(BaseMiddleware):
    """Внешний слой на все обновления: долгие пишет с разбивкой по времени."""

    async def __call__(self, handler, event, data):
        acc = [0.0, 0, "", 0.0]
        bag: dict[str, tuple[float, int]] = {}
        token, steps_token = _api.set(acc), _steps.set(bag)
        start = time.monotonic()
        try:
            return await handler(event, data)
        finally:
            _api.reset(token)
            _steps.reset(steps_token)
            took = time.monotonic() - start
            if took * 1000 >= config.DIAG_SLOW_MS:
                api, calls, slowest, slowest_s = acc
                steps = " · ".join(
                    f"{name} {spent:.1f} с ×{count}"
                    for name, (spent, count) in sorted(bag.items(),
                                                       key=lambda kv: -kv[1][0]))
                note("долго %.1f с · %s · Telegram %.1f с за %d запр. (дольше всех %s %.1f с)"
                     " · своё %.1f с%s",
                     took, _describe(event), api, calls, slowest or "—", slowest_s,
                     max(0.0, took - api), f" · {steps}" if steps else "")


async def api_timer(make_request, bot, method):
    """Мидлварь сессии: сколько обновление ждало ответов Telegram."""
    acc = _api.get()
    start = time.monotonic()
    try:
        return await make_request(bot, method)
    finally:
        if acc is not None:
            spent = time.monotonic() - start
            acc[0] += spent
            acc[1] += 1
            if spent > acc[3]:
                acc[2], acc[3] = type(method).__name__, spent
