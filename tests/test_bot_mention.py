"""Упоминание чужого бота гостем: Telegram отдаёт бота типом private, как
человека, и реклама «пройди тест у @…bot» от комментатора шла насквозь."""
from aiogram.types import MessageEntity

from gremlin import db
from gremlin.handlers import group
from gremlin.services import moderation

from conftest import FakeBot, Msg, make_user

TEXT = "я проходила тест у @psohmarybot, попробуй и ты"


def bot_msg(uid):
    m = Msg(TEXT, author=make_user(uid))
    m.entities = [MessageEntity(type="mention", offset=TEXT.index("@"), length=12)]
    m.caption_entities = None
    return m


async def test_guest_bot_mention_is_punished_member_is_not(chat, members,
                                                           monkeypatch):
    hits = []

    async def violation(bot, message, bit, feature, kind, minutes, detail):
        hits.append((message.from_user.id, feature, kind, detail))
    monkeypatch.setattr(moderation, "violation", violation)
    for k, v in {"links_on": 1, "mentions_check": 1, "gp_men": "ban",
                 "lp_men": "delete", "watch_on": 0, "nn_mode": 0}.items():
        await db.set_setting(chat, k, v)

    members["outside"].add(4848)
    await group.moderate(bot_msg(4848), FakeBot())
    assert hits == [(4848, "упоминание стороннего чата", "ban", "@psohmarybot (бот)")]

    await group.moderate(bot_msg(4949), FakeBot())   # участник зовёт бота — можно
    assert len(hits) == 1
