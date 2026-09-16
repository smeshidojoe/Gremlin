"""Короткая настройка свежего чата: лог-чат и перенос настроек.

Без лог-чата бот работает молча: карточек нет, кнопок «снять наказание» и
«впустить» тоже. Раньше развилку показывали, только если было откуда
переносить настройки, и владелец первого чата про лог-чат не узнавал вовсе.
"""
import pytest

from gremlin import db
from gremlin.handlers import user_menu as um

from conftest import CHAT, OWNER

OTHER = CHAT - 50
LOG = CHAT - 51


def buttons(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


async def test_fresh_chat_needs_setup_even_when_its_the_only_one(chat):
    assert await um.needs_setup(chat, OWNER) is True
    await db.kv_set(um.setup_key(chat), "1")
    assert await um.needs_setup(chat, OWNER) is False


async def test_setup_offers_log_chat_first(chat):
    text, kb = await um.view_setup(chat, OWNER)
    assert "Лог-чат" in text and "не выбран" in text
    assert f"u:logsel:{chat}" in buttons(kb)
    assert f"u:cpn:{chat}" in buttons(kb)


async def test_transfer_offered_only_when_there_is_a_source(chat):
    _text, kb = await um.view_setup(chat, OWNER)
    assert f"u:cp:{chat}" not in buttons(kb)        # других чатов нет

    await db.upsert_chat(OTHER, "Второй", None, OWNER, "supergroup")
    await db.get_settings(OTHER)
    text, kb = await um.view_setup(chat, OWNER)
    assert f"u:cp:{chat}" in buttons(kb)
    assert "Перенос настроек" in text


async def test_chosen_log_chat_is_shown(chat):
    await db.upsert_chat(LOG, "Тетрадь", None, OWNER, "supergroup")
    await db.set_setting(chat, "log_chat_id", LOG)
    text, kb = await um.view_setup(chat, OWNER)
    assert "Тетрадь" in text
    assert any("✅" in b.text for row in kb.inline_keyboard for b in row)


async def test_chat_card_warns_about_missing_log_chat(chat):
    text, _kb = await um.view_chat(chat, OWNER)
    assert "Лог-чат не выбран" in text

    await db.upsert_chat(LOG, "Тетрадь", None, OWNER, "supergroup")
    await db.set_setting(chat, "log_chat_id", LOG)
    text, _kb = await um.view_chat(chat, OWNER)
    assert "Лог-чат не выбран" not in text
