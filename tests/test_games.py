"""Приколы: рулетка с общим барабаном и ответ на пасты."""
import asyncio
import json
import time

import pytest

from gremlin import config, db
from gremlin.handlers import games, group
from gremlin.services import moderation, triggers

from conftest import CHAT, FakeBot, Msg, make_user

ADMIN, VICTIM, PLAIN = 7001, 7002, 7003


@pytest.fixture
async def roulette(chat, members, monkeypatch):
    members["admins"].add(ADMIN)
    await db.set_setting(chat, "games_on", config.GAME_RUS)
    punished = []

    async def apply_punishment(bot, chat_id, user, kind, minutes, reason, by_id,
                               wipe=True):
        punished.append(getattr(user, "id", user))
        return 1

    orig_sleep = asyncio.sleep

    async def fast_sleep(delay, *a, **kw):
        await orig_sleep(0)

    monkeypatch.setattr(moderation, "apply_punishment", apply_punishment)
    monkeypatch.setattr(games, "_rus_locks", {})
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    return punished


def bullet_at(monkeypatch, position):
    """Зарядить патрон в нужное гнездо (0 — первое)."""
    monkeypatch.setattr(games.random, "randrange", lambda n: position)


async def spin(author, reply_from=None):
    m = Msg(author=author, reply_from=reply_from)
    await games.cmd_roulette(m, FakeBot())
    return m


async def test_target_selection(roulette):
    bot = FakeBot()
    assert (await games._rus_target(Msg(author=make_user(PLAIN)), bot))[0].id \
        == PLAIN
    who, by_admin = await games._rus_target(
        Msg(author=make_user(ADMIN), reply_from=make_user(VICTIM)), bot)
    assert (who.id, by_admin) == (VICTIM, True)
    who, by_admin = await games._rus_target(
        Msg(author=make_user(PLAIN), reply_from=make_user(VICTIM)), bot)
    assert (who.id, by_admin) == (PLAIN, False)
    who, by_admin = await games._rus_target(
        Msg(author=make_user(ADMIN), reply_from=make_user(9, is_bot=True)), bot)
    assert (who.id, by_admin) == (ADMIN, False)


async def test_chance_grows_and_last_chamber_always_fires(roulette, monkeypatch):
    """Патрон в последнем гнезде: пять щелчков, шестой — выстрел.
    Жать может один и тот же человек подряд."""
    bullet_at(monkeypatch, config.RUS_CHANCE - 1)
    for _ in range(config.RUS_CHANCE - 1):
        await spin(make_user(PLAIN))
    assert roulette == []
    await spin(make_user(PLAIN))
    assert roulette == [PLAIN]


async def test_first_chamber_fires_immediately(roulette, monkeypatch):
    bullet_at(monkeypatch, 0)
    await spin(make_user(PLAIN))
    assert roulette == [PLAIN]


async def test_revolver_reloads_for_whole_chat_after_shot(roulette, monkeypatch):
    bullet_at(monkeypatch, 0)
    await spin(make_user(PLAIN))
    roulette.clear()
    m = await spin(make_user(VICTIM))           # другой человек
    assert roulette == []
    assert "перезарядке" in m.replies[0]


async def test_reload_expires_and_drum_is_loaded_again(roulette, monkeypatch,
                                                       chat):
    bullet_at(monkeypatch, 0)
    await spin(make_user(PLAIN))
    await db.kv_set(games._RUS_KEY.format(chat),
                    json.dumps({"reload_until": int(time.time()) - 1}))
    roulette.clear()
    await spin(make_user(VICTIM))
    assert roulette == [VICTIM]


async def test_drum_survives_between_calls(roulette, monkeypatch, chat):
    """Барабан в базе: перезапуск не должен обнулять набитые щелчки."""
    bullet_at(monkeypatch, 3)
    await spin(make_user(PLAIN))
    await spin(make_user(VICTIM))
    drum = json.loads(await db.kv_get(games._RUS_KEY.format(chat)))
    assert drum == {"bullet": 3, "pulls": 2}


async def test_drum_is_per_chat(roulette, monkeypatch, chat):
    other = CHAT - 1
    await db.upsert_chat(other, "Другой", None, 1, "supergroup")
    await db.get_settings(other)
    await db.set_setting(other, "games_on", config.GAME_RUS)
    bullet_at(monkeypatch, 0)
    await spin(make_user(PLAIN))                 # выстрел в первом чате
    m = Msg(author=make_user(VICTIM), cid=other)
    await games.cmd_roulette(m, FakeBot())
    assert "перезарядке" not in m.replies[0]


async def test_miss_shows_remaining_chance(roulette, monkeypatch):
    bullet_at(monkeypatch, 5)
    m = Msg(author=make_user(PLAIN))
    sent = []

    async def reply(text, **kw):
        from conftest import Sent
        s = Sent(text)
        sent.append(s)
        return s

    m.reply = reply
    await games.cmd_roulette(m, FakeBot())
    assert "шанс 1 из 5" in sent[0].text


async def test_admin_spins_for_replied_user(roulette, monkeypatch):
    bullet_at(monkeypatch, 0)
    m = await spin(make_user(ADMIN), reply_from=make_user(VICTIM, "Жертва"))
    assert "Барабан крутят за" in m.replies[0]
    assert roulette == [VICTIM]


async def test_bullet_spent_even_on_admin(roulette, monkeypatch):
    """Админа пуля не берёт, но патрон потрачен: иначе админ разряжал бы
    барабан без последствий."""
    bullet_at(monkeypatch, 0)
    await spin(make_user(ADMIN))
    assert roulette == []
    m = await spin(make_user(PLAIN))
    assert "перезарядке" in m.replies[0]


async def test_disabled_game_silent(roulette, chat):
    await db.set_setting(chat, "games_on", 0)
    m = await spin(make_user(ADMIN), reply_from=make_user(VICTIM))
    assert m.replies == []


@pytest.fixture
async def paste(chat, monkeypatch):
    sent = []

    async def send_answer(message, ans, **kw):
        sent.append(ans["text"])

    monkeypatch.setattr(triggers, "send_answer", send_answer)
    monkeypatch.setattr(group, "_paste_fired", {})
    await db.ans_add("paste", chat, "не читал, но осуждаю")
    await db.set_setting(chat, "games_on", config.GAME_PASTE)
    await db.set_setting(chat, "paste_min", 300)
    await db.set_setting(chat, "paste_cd", 15)
    return sent


async def test_paste_answers_long_message_with_cooldown(paste, chat):
    s = await db.get_settings(chat)
    await group.fire_paste(FakeBot(), Msg("коротко"), s)
    assert paste == []
    long_text = "буква " * 100
    await group.fire_paste(FakeBot(), Msg(long_text), s)
    assert paste == ["не читал, но осуждаю"]
    await group.fire_paste(FakeBot(), Msg(long_text), s)
    assert len(paste) == 1
    group._paste_fired[chat] = time.monotonic() - 16 * 60
    await group.fire_paste(FakeBot(), Msg(long_text), s)
    assert len(paste) == 2


async def test_paste_silent_without_answers(paste, chat):
    await db.ans_clear("paste", chat)
    s = await db.get_settings(chat)
    await group.fire_paste(FakeBot(), Msg("буква " * 100), s)
    assert paste == []


@pytest.fixture
async def vanish(chat, members, monkeypatch):
    """!vanish включён, стирает 3; удалённое перехватываем."""
    from gremlin.services import deleting
    members["admins"].add(ADMIN)
    await db.set_setting(chat, "games_on", config.GAME_VANISH)
    await db.set_setting(chat, "vanish_n", 3)
    gone = []

    async def many(bot, chat_id, ids):
        gone.extend(ids)
        return len(ids)

    monkeypatch.setattr(deleting, "many", many)
    monkeypatch.setattr(moderation, "_seen_msgs", {})
    for uid, base in ((PLAIN, 100), (VICTIM, 200)):
        for i in range(5):
            moderation.remember_message(chat, uid, base + i)
    return gone


def vanish_msg(author, reply_from=None, mid=999):
    msg = Msg("!vanish", author=make_user(author), reply_from=reply_from)
    msg.message_id = mid
    moderation.remember_message(CHAT, author, mid)     # команду бот тоже запомнил
    return msg


async def test_vanish_erases_own_last_n_and_the_command(vanish, chat):
    await games.fire_game(FakeBot(), vanish_msg(PLAIN))
    assert sorted(vanish) == [102, 103, 104, 999]      # чужие 200+ не тронуты


async def test_vanish_by_reply_admin_only(vanish, chat):
    # обычный участник ответом стирает только своё
    await games.fire_game(FakeBot(), vanish_msg(PLAIN, reply_from=make_user(VICTIM)))
    assert sorted(vanish) == [102, 103, 104, 999]
    vanish.clear()
    # админ ответом — сообщения автора того, на что ответил
    await games.fire_game(FakeBot(), vanish_msg(ADMIN, reply_from=make_user(VICTIM), mid=998))
    assert sorted(vanish) == [202, 203, 204, 998]
