"""Последние ошибки в памяти — для админ-меню."""
import time
from collections import deque

from .. import config

_errors: deque[str] = deque(maxlen=config.ERROR_LOG_SIZE)


def add(text: str) -> None:
    ts = time.strftime("%d.%m %H:%M:%S")
    _errors.append(f"[{ts}] {text}")


def recent(n: int = 15) -> list[str]:
    return list(_errors)[-n:]
