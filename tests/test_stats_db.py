"""База статистики: её отсутствие не должно ломать меню.

Подключение обычным способом создаёт пустой файл. После этого сводка падала не
на «базы нет», а на «в базе нет таблиц», и раздел переставал открываться.
"""
import os
import sqlite3

from gremlin import config
from gremlin.services import digest, stats_collect


def test_reading_absent_db_creates_nothing(tmp_path, monkeypatch):
    path = tmp_path / "нет.db"
    monkeypatch.setattr(config, "STATS_DB", str(path))
    monkeypatch.setattr(stats_collect, "_tracked_cache", (0.0, None))
    assert stats_collect.tracked_chat_id() is None
    assert not os.path.exists(path), "файл базы не должен появляться из чтения"


def test_empty_db_is_treated_as_missing(tmp_path, monkeypatch):
    path = tmp_path / "пустая.db"
    sqlite3.connect(path).close()          # файл есть, таблиц нет
    monkeypatch.setattr(config, "STATS_DB", str(path))
    monkeypatch.setattr(stats_collect, "_tracked_cache", (0.0, None))
    assert stats_collect.tracked_chat_id() is None
    assert digest.collect(str(path)) is None


def test_missing_db_for_digest(tmp_path):
    assert digest.collect(str(tmp_path / "нету.db")) is None
