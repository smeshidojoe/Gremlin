"""Пауза дуэли и жесты: ошибки тут молчаливые — не тот ключ паузы держит не
того человека, а неэкранированное имя ломает разметку ответа."""
import pytest
from aiogram.types import MessageEntity

from gremlin import config, db
from gremlin.handlers import games
from gremlin.services import deleting, moderation, triggers

from conftest import FakeBot, Msg, make_user

A, B, C, D, ADMIN = 8101, 8102, 8103, 8104, 8105


@pytest.fixture
def penalties(chat, members, monkeypatch):
    """Кого и на сколько мутили; что стирали."""
    members["admins"].add(ADMIN)
    out = {"mutes": [], "deleted": []}

    async def game_punish(bot, chat_id, user, kind, minutes, reason, by_id):
        out["mutes"].append((user.id, minutes))
        return 1, None, None

    async def many(bot, chat_id, ids):
        out["deleted"].append(list(ids))
        return len(ids)

    monkeypatch.setattr(moderation, "game_punish", game_punish)
    monkeypatch.setattr(deleting, "many", many)
    monkeypatch.setattr(games.runtime, "spawn", lambda coro: coro.close())
    monkeypatch.setattr(games, "_duel_last", {})
    monkeypatch.setattr(games, "_gest_last", {})
    return out


def msg(author, text="", reply_to=None, entities=None):
    m = Msg(text, author=make_user(author) if isinstance(author, int) else author,
            reply_from=(make_user(reply_to) if isinstance(reply_to, int)
                        else reply_to))
    m.entities = entities
    m.caption_entities = None
    return m


async def test_duel_once_an_hour_per_caller(chat, penalties):
    await db.set_setting(chat, "games_on", config.GAME_DUEL)
    first = msg(A, "!дуэль", reply_to=B)
    await games.cmd_duel(first, FakeBot())
    assert first.replies and not penalties["mutes"]

    again = msg(A, "!дуэль", reply_to=C)          # тот же вызывающий — рано
    await games.cmd_duel(again, FakeBot())
    assert again.replies == []
    assert penalties["mutes"] == [(A, config.DUEL_CD_MUTE)]
    assert penalties["deleted"] == [[again.message_id]]

    other = msg(D, "!дуэль", reply_to=B)          # B вызывать может кто угодно
    await games.cmd_duel(other, FakeBot())
    assert other.replies and len(penalties["mutes"]) == 1

    for _ in range(2):                            # админ — без паузы
        m = msg(ADMIN, "!дуэль", reply_to=C)
        await games.cmd_duel(m, FakeBot())
        assert m.replies
    assert len(penalties["mutes"]) == 1


async def test_duel_in_comments_ignores_channel_post(chat, penalties):
    """В комментариях всё — ответ на пост канала, его приносит Telegram
    (777000). Соперником он стать не должен; @ник в команде — должен."""
    await db.set_setting(chat, "games_on", config.GAME_DUEL)
    post = msg(A, "!дуэль", reply_to=make_user(777000, name="Telegram"))
    post.reply_to_message.sender_chat = object()
    await games.cmd_duel(post, FakeBot())
    assert "Telegram" not in post.replies[0] and "@ником" in post.replies[0]

    await db.msg_inc(chat, C, "bobby", "Боб")
    text = "!дуэль @bobby"
    m = msg(B, text, reply_to=make_user(777000, name="Telegram"),
            entities=[MessageEntity(type="mention", offset=7, length=6)])
    await games.cmd_duel(m, FakeBot())
    assert "Telegram" not in m.replies[0]
    assert {"caller": B, "foe": C} in games._duels.values()


@pytest.fixture
def answers(monkeypatch):
    sent = []

    async def send_answer(anchor, ans, reply=True, subs=None):
        text = ans["text"]
        for k, v in (subs or {}).items():
            text = text.replace(k, v)
        sent.append(text)
    monkeypatch.setattr(triggers, "send_answer", send_answer)
    return sent


async def test_gesture_tags_escaped_and_timers_apart(chat, penalties, answers):
    await db.set_setting(chat, "games_on", config.GAME_LOVE | config.GAME_MOG)
    await db.ans_add("love", chat, "{кто} → {кому} ({сколько})")
    await db.ans_add("mog", chat, "{кто} моггнул {кому}")
    evil = make_user(A, name="<b>Ann", username="ann")

    await games.cmd_love(msg(evil, "!любить", reply_to=B), FakeBot())
    assert answers[-1] == (f'<a href="tg://user?id={A}">&lt;b&gt;Ann</a> → '
                           f'<a href="tg://user?id={B}">Юзер{B}</a> (1)')

    await games.cmd_mog(msg(evil, "!моггнуть", reply_to=B), FakeBot())
    assert len(answers) == 2 and not penalties["mutes"]   # у мога своя пауза

    await games.cmd_love(msg(evil, "!любить", reply_to=B), FakeBot())
    assert len(answers) == 2
    assert penalties["mutes"] == [(A, 5)]

    t = await db.tally_get(chat, B)
    assert (t["love_got"], t["mog_got"]) == (1, 1)
    assert (await db.tally_get(chat, A))["love_gave"] == 1


async def test_gesture_spam_without_mute(chat, penalties, answers):
    await db.set_setting(chat, "games_on", config.GAME_LOVE)
    await db.set_setting(chat, "gest_mute", 0)
    await games.cmd_love(msg(A, "!любовь", reply_to=B), FakeBot())
    await games.cmd_love(msg(A, "!любовь", reply_to=B), FakeBot())
    assert penalties["mutes"] == [] and len(penalties["deleted"]) == 1


async def test_gesture_by_username(chat, penalties, answers):
    await db.set_setting(chat, "games_on", config.GAME_LOVE)
    await db.msg_inc(chat, C, "bobby", "Боб")
    text = "!любовь @Bobby"
    ent = [MessageEntity(type="mention", offset=text.index("@"), length=6)]
    await games.cmd_love(msg(A, text, entities=ent), FakeBot())
    assert f"tg://user?id={C}" in answers[-1]
    assert (await db.tally_get(chat, C))["love_got"] == 1

    text = "!любовь @nobody"
    ghost = msg(D, text, entities=[MessageEntity(type="mention", offset=8, length=7)])
    await games.cmd_love(ghost, FakeBot())
    assert ghost.replies == [games._UNKNOWN]


async def test_bot_counts_and_topic_root_is_no_reply(chat, penalties, answers):
    await db.set_setting(chat, "games_on", config.GAME_LOVE | config.GAME_STATUS)
    bot = FakeBot()
    gremlin = make_user(bot.id, name="Gremlin", is_bot=True)
    await games.cmd_love(msg(A, "!love", reply_to=gremlin), bot)
    assert (await db.tally_get(chat, bot.id))["love_got"] == 1

    card = msg(B, "!status", reply_to=gremlin)
    await games.cmd_status(card, bot)
    assert "love_got" not in card.replies[0] and ": <b>1</b>" in card.replies[0]

    # in a forum every message "replies" to the topic root: that is no target
    topic = msg(C, "!status", reply_to=D)
    topic.reply_to_message.forum_topic_created = object()
    await games.cmd_status(topic, bot)
    assert f"tg://user?id={C}" in topic.replies[0]


async def test_inline_gesture(chat, penalties, answers):
    await db.set_setting(chat, "games_on", config.GAME_LOVE)
    m = msg(A, "я бы хотел дать тебе !любовь", reply_to=B)
    assert await games.fire_game(FakeBot(), m)
    assert (await db.tally_get(chat, B))["love_got"] == 1

    # no target mid-text: stay silent, let triggers have the message
    lone = msg(C, "вот это !любовь, да")
    assert not await games.fire_game(FakeBot(), lone)
    assert lone.replies == [] and len(answers) == 1
    # a word merely glued to "!" is not a command
    assert not await games.fire_game(FakeBot(), msg(D, "привет!любовь", reply_to=B))


async def test_status_of_others_is_admin_only(chat, penalties, members):
    await db.set_setting(chat, "games_on", config.GAME_STATUS)
    plain = msg(A, "!статус", reply_to=B)
    await games.cmd_status(plain, FakeBot())
    assert f"tg://user?id={A}" in plain.replies[0]

    admin = msg(ADMIN, "!статус", reply_to=B)
    await games.cmd_status(admin, FakeBot())
    assert f"tg://user?id={B}" in admin.replies[0]


async def test_disabled_gesture_is_removed_and_punished(chat, penalties, answers):
    await db.set_setting(chat, "games_on", 0)
    m = msg(A, "!любовь", reply_to=B)
    assert await games.fire_game(FakeBot(), m)
    assert penalties["deleted"] == [[m.message_id]]      # only the command
    assert penalties["mutes"] == [(A, 5)] and answers == []

    # mid-text is left alone even with a target
    assert not await games.fire_game(FakeBot(), msg(C, "дать тебе !любовь", reply_to=B))
    await games.fire_game(FakeBot(), msg(ADMIN, "!mog", reply_to=B))
    assert len(penalties["deleted"]) == 1

    await db.set_setting(chat, "gest_off_punish", 0)
    await games.fire_game(FakeBot(), msg(D, "!любовь", reply_to=B))
    assert len(penalties["deleted"]) == 1 and len(penalties["mutes"]) == 1
