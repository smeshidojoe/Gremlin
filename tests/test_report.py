"""Жалобы участников: команда в чате и кнопки карточки."""
import time
import types

import pytest

from gremlin import config, db
from gremlin.handlers import cards as cards_h
from gremlin.handlers import group
from gremlin.services import moderation

from conftest import CB, CHAT, OWNER, FakeBot, Msg, Sent, make_user

ADMIN, VICTIM, WHINER, GUEST = 8801, 8802, 8803, 8804


@pytest.fixture
async def reports(chat, members, cards, monkeypatch):
    """Жалобы включены, карточки перехвачены, уборка в чате не запускается."""
    members["admins"].add(ADMIN)
    await db.set_setting(chat, "report_on", 1)
    monkeypatch.setattr(group, "_report_fired", {})
    monkeypatch.setattr(group, "_reports", {})

    def spawn(coro):
        coro.close()

    monkeypatch.setattr(group.runtime, "spawn", spawn)
    return cards


def complaint(author, target=None, text="!report", msg_id=555):
    """Сообщение-жалоба ответом на чужое."""
    m = Msg(text, author=author)
    if target is not None:
        reply = Msg("купи крипту дёшево", author=target)
        reply.message_id = msg_id
        m.reply_to_message = reply
    return m


async def test_card_has_message_and_buttons(reports, chat):
    m = complaint(make_user(WHINER), make_user(VICTIM, "Спамер"), "!report реклама")
    await group.cmd_report(m, FakeBot())
    assert len(reports) == 1
    card = reports[0]
    assert card["bit"] == config.BIT_REPORT
    assert "Спамер" in card["text"] and "реклама" in card["text"]
    assert "купи крипту дёшево" in card["text"]
    # ссылка нужна всегда: фото и голосовое в карточке не посмотреть
    assert "🔗" in card["text"] and "t.me/c/" in card["text"]
    buttons = [b.callback_data for row in card["markup"].inline_keyboard for b in row]
    assert buttons == [f"k:rdel:{chat}:555", f"k:rmute:{chat}:{VICTIM}:555",
                       f"k:rban:{chat}:{VICTIM}:555", f"k:sp:{chat}:{VICTIM}",
                       f"k:rno:{chat}"]


async def test_reason_is_optional(reports):
    await group.cmd_report(complaint(make_user(WHINER), make_user(VICTIM)), FakeBot())
    assert "Причина" not in reports[0]["text"]


async def test_second_report_counts_instead_of_new_card(reports, chat):
    bot = FakeBot()
    await group.cmd_report(complaint(make_user(WHINER), make_user(VICTIM)), bot)
    await group.cmd_report(complaint(make_user(GUEST), make_user(VICTIM)), bot)
    assert len(reports) == 1
    assert group._reports[(chat, 555)]["count"] == 2
    assert "🔁 Пожаловались: <b>2</b>" in group._reports[(chat, 555)]["text"]


async def test_cooldown_between_reports(reports):
    bot = FakeBot()
    who = make_user(WHINER)
    await group.cmd_report(complaint(who, make_user(VICTIM)), bot)
    await group.cmd_report(complaint(who, make_user(GUEST), msg_id=556), bot)
    assert len(reports) == 1                     # вторая жалоба ушла в паузу
    group._report_fired[(CHAT, WHINER)] = time.monotonic() - 10_000
    await group.cmd_report(complaint(who, make_user(GUEST), msg_id=556), bot)
    assert len(reports) == 2


async def test_only_members_may_report(reports, chat, members):
    members["outside"].add(GUEST)
    await db.set_setting(chat, "report_who", "members")
    await group.cmd_report(complaint(make_user(GUEST), make_user(VICTIM)), FakeBot())
    assert reports == []


async def test_reports_on_admins_are_dropped_by_default(reports, chat):
    await group.cmd_report(complaint(make_user(WHINER), make_user(ADMIN)), FakeBot())
    assert reports == []
    await db.set_setting(chat, "report_admins", 1)
    group._report_fired.clear()
    await group.cmd_report(complaint(make_user(WHINER), make_user(ADMIN)), FakeBot())
    assert len(reports) == 1


async def test_needs_reply_and_not_self(reports):
    bot = FakeBot()
    await group.cmd_report(complaint(make_user(WHINER)), bot)
    await group.cmd_report(complaint(make_user(WHINER), make_user(WHINER)), bot)
    assert reports == []


async def test_disabled_feature_is_silent(reports, chat):
    await db.set_setting(chat, "report_on", 0)
    m = complaint(make_user(WHINER), make_user(VICTIM))
    await group.cmd_report(m, FakeBot())
    assert reports == [] and m.replies == []


# ---------- кнопки карточки ----------

@pytest.fixture
def punished(monkeypatch):
    done = []

    async def apply_punishment(bot, chat_id, user, kind, minutes, reason, by_id,
                               wipe=True):
        done.append((user.id, kind, minutes, reason))
        return 77

    monkeypatch.setattr(moderation, "apply_punishment", apply_punishment)
    return done


def press(data, uid=OWNER):
    msg = Sent("🚨 <b>Жалоба</b>")
    msg.chat = types.SimpleNamespace(id=-100999)
    cb = CB(data, uid=uid, message=msg)
    cb.bot = FakeBot()
    return cb, msg


async def test_ban_button_punishes_and_keeps_evidence(reports, chat, punished):
    await group.cmd_report(complaint(make_user(WHINER), make_user(VICTIM)), FakeBot())
    cb, msg = press(f"k:rban:{chat}:{VICTIM}:555")
    await cards_h.card_report_ban(cb, cb.bot)
    assert punished == [(VICTIM, "ban", 0, "по жалобе участников")]
    assert "Забанен по жалобе" in msg.text
    rows = await db.samples_of_origin(chat, "card")
    assert [r["text"] for r in rows] == ["купи крипту дёшево"]


async def test_mute_button_uses_chat_setting(reports, chat, punished):
    await db.set_setting(chat, "report_mute_min", 180)
    await group.cmd_report(complaint(make_user(WHINER), make_user(VICTIM)), FakeBot())
    cb, _ = press(f"k:rmute:{chat}:{VICTIM}:555")
    await cards_h.card_report_mute(cb, cb.bot)
    assert punished == [(VICTIM, "mute", 180, "по жалобе участников")]


async def test_delete_and_dismiss(reports, chat):
    cb, msg = press(f"k:rdel:{chat}:555")
    await cards_h.card_report_delete(cb, cb.bot)
    assert "Сообщение удалено" in msg.text

    cb2, msg2 = press(f"k:rno:{chat}")
    await cards_h.card_report_drop(cb2)
    assert "Жалоба отклонена" in msg2.text


async def test_stranger_cannot_press(reports, chat, punished):
    cb, _ = press(f"k:rban:{chat}:{VICTIM}:555", uid=GUEST)
    await cards_h.card_report_ban(cb, cb.bot)
    assert punished == []
