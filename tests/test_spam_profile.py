"""«Спам-профиль»: ручная запись профиля в спам-базу."""
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


async def seed_faces():
    return [(r["label"], r["text"]) for r in await db.seed_page(None, None, 0, 50, "prof")]


async def chat_faces(chat_id):
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


async def test_remember_writes_to_spam_base_once(chat):
    """Кнопка пишет в спам-базу, а не в базу чата: там её и ищут."""
    bot = SpamBot()
    ok, note = await nn.remember_spam_profile(bot, chat, U)
    assert ok and "записан" in note
    assert await seed_faces() == [("spam", "Анна 18+ @anna_dm · пиши в лс")]
    assert await chat_faces(chat) == []
    ok, note = await nn.remember_spam_profile(bot, chat, U)
    assert not ok and "уже" in note
    assert len(await seed_faces()) == 1


async def test_autoban_record_does_not_block_button(chat):
    """Запись автобана в копилке — не «уже в базе»: в спам-базе её нет."""
    await db.sample_add(chat, U, "profile", "spam", "Анна 18+ @anna_dm · пиши в лс")
    ok, _ = await nn.remember_spam_profile(SpamBot(), chat, U)
    assert ok
    assert len(await seed_faces()) == 1


async def test_remember_relabels_forgiven_face(chat):
    await db.sample_add(chat, U, "profile", "ok", "старое имя", labeled_by=1)
    ok, _ = await nn.remember_spam_profile(SpamBot(), chat, U)
    assert ok
    assert {r["label"] for r in await db.samples_pool("prof")} == {"spam"}


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
    assert "записан в спам-базу" in msg.text
    # «Спам-профиль» ушёл, на его месте — отмена
    assert buttons(msg.reply_markup) == ["k:lift:5", f"k:spu:{chat}:{U}"]
    assert cb.alerts == ["Записан"]
    assert len(await seed_faces()) == 1

    # передумали — запись уходит из спам-базы, кнопка «Спам-профиль» возвращается
    undo = CB(f"k:spu:{chat}:{U}", message=msg)
    await cards.card_spam_profile_undo(undo, SpamBot())
    assert "убран из спам-базы" in msg.text
    assert buttons(msg.reply_markup) == ["k:lift:5", f"k:sp:{chat}:{U}"]
    assert undo.alerts == ["Убран"]
    assert await seed_faces() == []


async def test_undo_keeps_other_people(chat):
    await db.seed_add("спамер номер один", "spam", "prof", user_id=U)
    await db.seed_add("другой спамер тут", "spam", "prof", user_id=U + 1)
    assert await db.seed_forget_user(U) == 1
    assert await seed_faces() == [("spam", "другой спамер тут")]


async def test_old_chat_profiles_move_to_spam_base(chat):
    """Ручные спам-профили из баз чатов переезжают в спам-базу при старте;
    автобаны и случаи остаются в копилке."""
    await db.sample_add(chat, U, "profile", "spam", "ручной спамер", labeled_by=1)
    await db.sample_add(chat, U + 1, "profile", "spam", "автобан бота")
    await db.seed_add("уже в базе давно", "spam", "prof", user_id=U + 2)
    await db.sample_add(chat, U + 2, "profile", "spam", "дубль того же", labeled_by=1)
    await db.seed_commit()
    await db._migrate()
    assert sorted(await seed_faces()) == [("spam", "ручной спамер"),
                                          ("spam", "уже в базе давно")]
    assert await chat_faces(chat) == [("spam", "автобан бота")]


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
    assert len(await seed_faces()) == 1
