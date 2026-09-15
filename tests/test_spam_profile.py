"""«Спам-профиль»: ручная запись профиля в базу для сравнения."""
import types

import pytest

from gremlin import db
from gremlin.handlers import cards, user_menu as um
from gremlin.services import moderation, nn, profile

from conftest import CB, CHAT, OWNER, FakeBot, Sent

U = 9300


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(profile, "_cache", {})
    monkeypatch.setattr(nn, "_faces", {})


class SpamBot(FakeBot):
    def __init__(self, member=True):
        super().__init__()
        self.member = member

    async def get_chat_member(self, cid, uid):
        if not self.member:
            raise RuntimeError("user not found")
        return types.SimpleNamespace(status="kicked", user=types.SimpleNamespace(
            id=uid, full_name="Анна 18+", username="anna_dm"))

    async def get_chat(self, cid):
        if cid == U:
            return types.SimpleNamespace(id=U, bio="пиши в лс", photo=None,
                                         personal_chat=None)
        return await super().get_chat(cid)


async def faces(chat_id):
    return [(r["label"], r["text"]) for r in await db.samples_of_origin(chat_id, "profile")]


def buttons(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_button_added_under_existing():
    kb = moderation.with_spam_button(moderation.card_kb(5, "ban"), CHAT, U)
    assert buttons(kb) == ["k:lift:5", f"k:sp:{CHAT}:{U}"]
    assert buttons(moderation.with_spam_button(None, CHAT, U)) == [f"k:sp:{CHAT}:{U}"]


def test_manual_mute_card_has_real_ban_button():
    """Раньше карточка ручного мута слала «Забанить» с k:ban:None:None."""
    kb = moderation.card_kb(7, "mute", CHAT, U)
    assert f"k:ban:{CHAT}:{U}" in buttons(kb)


async def test_remember_writes_full_face_once(chat):
    bot = SpamBot()
    ok, note = await nn.remember_spam_profile(bot, chat, U)
    assert ok and "записан" in note
    assert await faces(chat) == [("spam", "Анна 18+ @anna_dm · пиши в лс")]
    ok, note = await nn.remember_spam_profile(bot, chat, U)
    assert not ok and "уже" in note
    assert len(await faces(chat)) == 1


async def test_remember_relabels_forgiven_face(chat):
    await nn.remember_face(chat, U, "старое имя", "ok")
    ok, _ = await nn.remember_spam_profile(SpamBot(), chat, U)
    assert ok
    assert {label for label, _ in await faces(chat)} == {"spam"}


async def test_remember_falls_back_to_db_and_refuses_unknown(chat):
    ok, note = await nn.remember_spam_profile(SpamBot(member=False), chat, U + 1)
    assert not ok and "Не знаю" in note


async def test_card_button(chat):
    msg = Sent("⛔ <b>Бан</b> (вручную)")
    msg.chat = types.SimpleNamespace(id=-100555)
    msg.reply_markup = moderation.with_spam_button(
        moderation.card_kb(5, "ban"), chat, U)
    cb = CB(f"k:sp:{chat}:{U}", message=msg)
    cb.bot = SpamBot()
    await cards.card_spam_profile(cb, cb.bot)
    assert "записан в базу спама" in msg.text
    assert buttons(msg.reply_markup) == ["k:lift:5"]
    assert cb.alerts == ["Записан"]
    assert len(await faces(chat)) == 1


async def test_status_card_button(chat):
    msg = Sent("🔎 Проверка статуса")
    msg.reply_markup = types.SimpleNamespace(inline_keyboard=[
        [types.SimpleNamespace(callback_data=f"u:spp:{chat}:{U}")],
        [types.SimpleNamespace(callback_data=f"u:p:{chat}:0")],
    ])
    edited = []

    async def edit_reply_markup(reply_markup=None):
        edited.append(reply_markup)

    msg.edit_reply_markup = edit_reply_markup
    cb = CB(f"u:spp:{chat}:{U}", uid=OWNER, message=msg)
    await um.cb_status_spam(cb, SpamBot())
    assert "записан" in cb.alerts[0]
    assert len(await faces(chat)) == 1
