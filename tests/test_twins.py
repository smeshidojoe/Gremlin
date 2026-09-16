"""Копии карточки в двух логах закрываются вместе.

Карточка уходит и в лог чата, и в глобальный лог владельца. Нажали кнопку в
одном — во втором она должна пропасть, иначе одно и то же решают дважды.
"""
import pytest

from gremlin.handlers import cards as cards_h
from gremlin.handlers import events
from gremlin.services import moderation

from conftest import CB, CHAT, OWNER, FakeBot, Sent

TWIN_LOG, TWIN_MSG = -1004000000001, 77
VICTIM = 9900


class EditBot(FakeBot):
    """Бот, который помнит правки чужих сообщений."""

    def __init__(self):
        super().__init__()
        self.edits = []

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
        self.edits.append((chat_id, message_id, kw.get("reply_markup")))


@pytest.fixture
async def twins(chat):
    """Карточка отправлена в два лога и связана сама с собой."""
    msg = Sent("🙋 <b>Заявка на вступление</b>")
    await moderation.link_twins([(CHAT, msg.message_id), (TWIN_LOG, TWIN_MSG)])
    return msg


async def press(msg, data, bot=None):
    cb = CB(data, uid=OWNER, message=msg)
    cb.bot = bot or EditBot()
    return cb


async def test_join_request_card_closes_in_both_logs(twins, members):
    cb = await press(twins, f"sub:ok:{CHAT}:{VICTIM}")
    await events.sub_take(cb, cb.bot)
    assert "Впущен" in twins.text
    assert twins.reply_markup is None
    # в копии: тот же итог и без кнопок
    assert cb.bot.edits == [(TWIN_LOG, TWIN_MSG, None)]


async def test_decline_also_closes_the_twin(twins, members):
    cb = await press(twins, f"sub:no:{CHAT}:{VICTIM}")
    await events.sub_drop(cb, cb.bot)
    assert "Отказано" in twins.text
    assert cb.bot.edits == [(TWIN_LOG, TWIN_MSG, None)]


async def test_report_card_closes_in_both_logs(twins, members, chat):
    cb = await press(twins, f"k:rno:{chat}")
    await cards_h.card_report_drop(cb)
    assert "Жалоба отклонена" in twins.text
    assert cb.bot.edits == [(TWIN_LOG, TWIN_MSG, None)]


async def test_lone_card_needs_no_twin(chat, members):
    """Копий нет — правим только своё сообщение и никуда не ходим."""
    msg = Sent("🙋 <b>Заявка на вступление</b>", message_id=505)
    cb = await press(msg, f"sub:no:{chat}:{VICTIM}")
    await events.sub_drop(cb, cb.bot)
    assert cb.bot.edits == []
