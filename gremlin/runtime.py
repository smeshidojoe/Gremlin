"""Рантайм-состояние процесса."""
import time

_started = time.monotonic()


def uptime_seconds() -> int:
    return int(time.monotonic() - _started)


# Публичный адрес панели. Может появиться не сразу: если адрес выдаёт туннель,
# он приходит через несколько секунд после старта, а до тех пор его нет.
_webapp_url: str | None = None


def webapp_url() -> str | None:
    return _webapp_url


def set_webapp_url(url: str | None) -> None:
    global _webapp_url
    _webapp_url = url


# Основной объект Bot. Нужен там, где до него не дотянуться параметром:
# меню рисуется из функций без bot в руках, а спросить Telegram иногда надо —
# например, есть ли у бота доступ к каналу.
_bot = None


def bot():
    return _bot


def set_bot(obj) -> None:
    global _bot
    _bot = obj
