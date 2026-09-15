"""Теневой прогон единой оценки: пишется, но ничего не решает и в Telegram не ходит."""
import json
import os
import time

from gremlin import config, db
from gremlin.services import adm_cache, moderation
from gremlin.services import verdict as vd
from gremlin.services import watch

from conftest import FakeBot, Msg, make_chat, make_user


def log_text(cid):
    p = vd.log_path(cid)
    return open(p, encoding="utf-8").read() if os.path.exists(p) else ""


async def test_shadow_writes_file_and_journal(chat):
    s = await db.get_settings(chat)
    assert s.uni_mode == 1
    ctx = await vd.context(chat, make_user(8200), Msg("привет",
                           reply_from=make_user(999)), lvl=2, text="привет")
    assert ctx["reply_to_other"] is True
    sig = vd.content_signals(stopword="в лс") + vd.profile_signals(word="18+")
    got = await vd.shadow(make_chat(), make_user(8200), s, signals=sig, ctx=ctx,
                          text="пиши в лс", was="стоп-слово/ban")
    assert got is not None
    txt = log_text(chat)
    assert "было: стоп-слово/ban" in txt
    assert "! [содержание] стоп-слово" in txt
    assert sum((await db.verdict_stats(chat)).values()) == 1


async def test_shadow_off_writes_nothing(chat):
    await db.set_setting(chat, "uni_mode", 0)
    s = await db.get_settings(chat)
    out = await vd.shadow(make_chat(), make_user(1), s,
                          signals=vd.content_signals(stopword="x"),
                          ctx={"outward": True}, text="x")
    assert out is None
    assert log_text(chat) == ""


async def test_reply_to_self_is_not_conversation(chat):
    me = make_user(8200)
    ctx = await vd.context(chat, me, Msg("ещё", author=me, reply_from=me),
                           lvl=2, text="ещё")
    assert ctx["reply_to_other"] is False


async def test_watch_records_verdict_without_changing_decision(chat, members,
                                                              cards):
    for key, val in (("watch_on", 1), ("watch_suspect", 40), ("watch_ban", 200),
                     ("prof_on", 0), ("trust_on", 0)):
        await db.set_setting(chat, key, val)
    members["outside"].add(8201)
    s = await db.get_settings(chat)
    bot = FakeBot()
    await watch.check_user(bot, make_chat(), make_user(8201, "Аня 18+ пиши в лс"),
                           s, Msg("зайди https://telegra.ph/spam"), None)
    assert cards, "наблюдение должно было отработать как раньше"
    assert bot.banned == []
    assert "было: наблюдение/" in log_text(chat)


async def test_rule_violation_evaluated_before_sample(chat, members, cards,
                                                      monkeypatch):
    """Оценка по снятому идёт до записи улики — иначе нашла бы сама себя."""
    order = []

    async def spy_shadow(*a, **kw):
        order.append("оценка")

    real_add = db.sample_add

    async def spy_add(*a, **kw):
        order.append("улика")
        return await real_add(*a, **kw)

    async def no_punish(bot, chat_id, user, kind, mute_min, reason, by_id,
                        wipe=True):
        return 1

    monkeypatch.setattr(moderation, "_uni_shadow", spy_shadow)
    monkeypatch.setattr(db, "sample_add", spy_add)
    monkeypatch.setattr(moderation, "apply_punishment", no_punish)
    await moderation.violation(FakeBot(), Msg("казино тут, пиши @dealer"),
                               config.BIT_WORDS, "стоп-слово", "delete", 0,
                               "казино")
    assert order[:2] == ["оценка", "улика"]


async def test_journal_keeps_breakdown(chat):
    s = await db.get_settings(chat)
    await vd.shadow(make_chat(), make_user(1), s,
                    signals=vd.content_signals(stopword="казино"),
                    ctx={"outward": True, "trust": 0, "guest": True},
                    text="казино", was="стоп-слово/delete")
    cur = await db._db.execute(
        "SELECT families, signals, was FROM verdicts ORDER BY id DESC LIMIT 1")
    row = await cur.fetchone()
    assert isinstance(json.loads(row["families"]), dict)
    assert isinstance(json.loads(row["signals"]), list)
    assert row["was"] == "стоп-слово/delete"
    assert await db.verdicts_prune(60) == 0


def test_member_cached_never_asks_telegram(monkeypatch):
    monkeypatch.setattr(adm_cache, "_members", {})
    assert adm_cache.member_cached(1, 2) is None
    adm_cache._members[(1, 2)] = (time.monotonic() + 100, False)
    assert adm_cache.member_cached(1, 2) is False
    adm_cache._members[(1, 3)] = (time.monotonic() - 1, True)   # протух
    assert adm_cache.member_cached(1, 3) is None
