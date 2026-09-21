"""diag.log: судьба правил под постом и долгие обновления видны в отдельном файле."""
import asyncio
import types

from gremlin import config, db
from gremlin.handlers import group
from gremlin.services import diag

from conftest import CHAT, FakeBot, Msg


def post(mid=500, group_id=None):
    m = Msg("новый пост")
    m.message_id = mid
    m.is_automatic_forward = True
    m.sender_chat = types.SimpleNamespace(id=-100777, title="Канал", username=None)
    m.message_thread_id = None
    m.media_group_id = group_id
    m.from_user = types.SimpleNamespace(id=777000, is_bot=False, username=None,
                                        first_name="Telegram", full_name="Telegram")
    return m


async def test_rules_post_outcome_is_noted(chat, monkeypatch):
    notes = []
    monkeypatch.setattr(diag, "note", lambda text, *a: notes.append(text % a))
    monkeypatch.setattr(group, "RULES_DELAY", 0)
    monkeypatch.setattr(group, "_rules_done", {})
    await db.set_setting(chat, "rules_on", 1)

    m = post()
    await group.moderate(m, FakeBot())
    await asyncio.sleep(0.05)
    assert notes[0].startswith(f"пост пришёл: 500 в {CHAT}")
    assert notes[-1] == f"правила: пост 500 в {CHAT} — заготовок нет"

    await db.ans_add("rules", chat, "Правила чата", None, None)
    await group.moderate(post(501, "alb"), FakeBot())
    await group.moderate(post(502, "alb"), FakeBot())
    await asyncio.sleep(0.05)
    assert f"правила: пост 502 в {CHAT} — уже отвечали (часть альбома)" in notes
    assert any(n.startswith(f"правила: пост 501 в {CHAT} — отправлено") for n in notes)


async def test_slow_update_noted_with_api_share(monkeypatch):
    notes = []
    monkeypatch.setattr(diag, "note", lambda text, *a: notes.append(text % a))
    monkeypatch.setattr(config, "DIAG_SLOW_MS", 0)

    async def make_request(bot, method):
        await asyncio.sleep(0.02)

    async def handler(event, data):
        await diag.api_timer(make_request, None, types.SimpleNamespace())
        await diag.api_timer(make_request, None, types.SimpleNamespace())

    ev = types.SimpleNamespace(event_type="message", event=types.SimpleNamespace(
        chat=types.SimpleNamespace(id=CHAT), content_type="text"))
    await diag.SlowUpdates()(handler, ev, {})
    assert f"message · чат {CHAT} · text" in notes[0]
    assert "за 2 запр." in notes[0]


async def test_avatar_scored_once_per_picture(chat, monkeypatch):
    """Аватарку считаем один раз: в комментариях под постом каждое сообщение
    от гостя заново запускало модель на ту же картинку."""
    from gremlin.services import nsfw
    from gremlin.services import profile as prof_svc
    from gremlin.services import watch

    monkeypatch.setattr(nsfw, "_seen", {})
    runs, loads = [], []

    async def score(raw):
        runs.append(raw)
        return 99

    async def photo_bytes(bot, data):
        loads.append(data["photo_id"])
        return b"IMG"

    monkeypatch.setattr(nsfw, "score", score)
    monkeypatch.setattr(prof_svc, "photo_bytes", photo_bytes)
    await db.set_setting(chat, "prof_on", 1)
    await db.set_setting(chat, "prof_photo", 1)
    s = await db.get_settings(chat)
    data = {"bio": "", "channel_title": "", "channel_desc": "",
            "channel_username": "", "photo_id": "AVA1"}
    user = types.SimpleNamespace(id=42, full_name="Гость", username=None)

    for _ in range(3):
        pts, why = await watch.photo_points(FakeBot(), chat, user, s, data)
        assert pts == s.prof_photo_score and "99%" in why[0]
    assert len(runs) == 1 and len(loads) == 1

    # новая картинка — считаем заново
    await watch.photo_points(FakeBot(), chat, user, s, dict(data, photo_id="AVA2"))
    assert len(runs) == 2
