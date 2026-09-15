"""Прощённые: узкое освобождение по сработавшему правилу, отдельно от вайтлиста."""
import pytest

from gremlin import db
from gremlin.handlers import cards, user_menu as um
from gremlin.services import moderation

from conftest import CB, Sent

UID = 7001


@pytest.mark.parametrize("reason,scope", [
    ("профиль: стоп-слово в профиле: «18+»", "watch"),
    ("наблюдение: гомоглифы (95)", "watch"),
    ("стоп-слово: казино", "words"),
    ("смысловое совпадение: подработка", "words"),
    ("внешняя ссылка: bit.ly", "links"),
    ("ссылка на сторонний чат: t.me/x", "links"),
    ("пересылка: из канала", "links"),
    ("флуд: 8 за 10 сек", "flood"),
    ("инлайн-бот: @gif", "inline"),
    ("рассылка: 4 чата", "watch"),
    ("сетка · каментики: стоп-слово: казино", "words"),
    ("вручную админом чата", None),
    ("без причины", None),
    ("победа в бан-рулетке", None),
    ("бан из карточки", None),
    ("заявка на вступление", None),
])
def test_rule_maps_to_scope(reason, scope):
    assert moderation.forgive_scope(reason) == scope


async def test_forgiveness_is_narrow_and_separate_from_whitelist(chat):
    await db.forgive_add(chat, UID, "vasya", "Вася", "watch", "профиль", 1)
    scopes = await db.free_scopes(chat, UID, "vasya")
    assert "watch" in scopes
    assert "words" not in scopes and "all" not in scopes
    assert await db.wl_scopes_for(chat, UID, "vasya") == set()
    await db.wl_set_scopes(chat, UID, "vasya", "Вася", {"links"})
    assert await db.free_scopes(chat, UID, "vasya") == {"links", "watch"}


async def test_card_button_forgives_by_rule(chat):
    pid = await db.add_punishment(chat, UID, "vasya", "Вася", "ban",
                                  "профиль: стоп-слово в профиле: «18+»",
                                  None, None)
    card = Sent("⛔ <b>Бан</b>")
    cb = CB(f"k:fg:{pid}", message=card)
    await cards.card_forgive(cb)
    assert await db.forgiven_scopes(chat, UID) == {"watch"}
    assert "Больше не трогаем: наблюдение" in card.text
    # повтор не дублирует
    await cards.card_forgive(CB(f"k:fg:{pid}"))
    assert await db.forgiven_count(chat) == 1


async def test_manual_punishment_is_not_forgivable(chat):
    pid = await db.add_punishment(chat, 7002, None, "Петя", "ban",
                                  "вручную админом чата", None, 1)
    cb = CB(f"k:fg:{pid}")
    await cards.card_forgive(cb)
    assert "выдали руками" in cb.alerts[0]
    assert await db.forgiven_count(chat) == 0


async def test_forgiven_list_in_menu(chat):
    await db.forgive_add(chat, UID, "vasya", "Вася", "watch",
                         "сетка · каментики: профиль: стоп-слово", 1)
    text, kb = await um.view_forgiven(chat)
    assert "Вася" in text and "наблюдение" in text
    assert "сетка ·" not in text
    _t, pkb = await um.view_punishments(chat)
    assert any("Прощённые: 1" in b.text for r in pkb.inline_keyboard for b in r)
    row = (await db.forgiven_list(chat))[0]
    await db.forgiven_remove(row["id"])
    assert await db.forgiven_scopes(chat, UID) == set()
