"""Ссылки на свои чаты не должны выглядеть для нейрофильтра рекламой.

В копилке спама почти все ссылки ведут на чужие каналы, и ссылка на пост
своего же канала получала в теневом журнале 82%.
"""
import types

from gremlin.handlers import group
from gremlin.services import adm_cache, filters, nn

from conftest import CHAT, FakeBot, Msg

LINKED = -1001234567890
OWN = ({"legushenkatg", "vegchat"}, {CHAT, LINKED})


def test_strip_keeps_foreign_links_only():
    text = ("глянь https://t.me/legushenkatg/32572?comment=7244552 и "
            "t.me/c/1234567890/15, а ещё https://t.me/gniloedina/1675")
    assert filters.strip_own_links(text, *OWN) == \
        "глянь и , а ещё https://t.me/gniloedina/1675"


def test_strip_leaves_plain_text_and_other_sites():
    text = "ну............. https://minesweeper.online/ru/help"
    assert filters.strip_own_links(text, *OWN) == text
    assert filters.strip_own_links("https://t.me/legushenkatg/1", *OWN) == ""


async def test_shadow_judges_text_without_own_links(monkeypatch):
    seen = []

    async def fake_check(chat_id, text):
        seen.append(text)
        return None
    monkeypatch.setattr(nn, "check", fake_check)
    s = types.SimpleNamespace(nn_mode=2, nn_threshold=75)

    msg = Msg("смотрите пост https://t.me/legushenkatg/32572 и https://t.me/spam_shop")
    assert await nn.shadow(CHAT, msg, s, own=OWN) is False
    assert seen == ["смотрите пост и https://t.me/spam_shop"]

    # без списка своих — как раньше, текст целиком
    await nn.shadow(CHAT, msg, s)
    assert seen[-1] == msg.text


async def test_own_targets_cover_chat_and_linked_channel(chat, monkeypatch):
    async def linked(bot, chat_id):
        return LINKED, "legushenkatg", "Канал"
    monkeypatch.setattr(adm_cache, "linked_chat", linked)
    names, ids = await group._own_targets(
        FakeBot(), types.SimpleNamespace(id=CHAT, username="vegchat"))
    assert {"legushenkatg", "vegchat", "GremlinTestBot"} <= names
    assert None not in names
    assert {CHAT, LINKED} <= ids
