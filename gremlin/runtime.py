"""Рантайм-состояние процесса."""
import time

_started = time.monotonic()


def uptime_seconds() -> int:
    return int(time.monotonic() - _started)
