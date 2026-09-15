"""Разовые правки данных при старте и соответствие схемы коду."""
import dataclasses
import time

from gremlin import db

from conftest import OWNER, FakeBot

SRC, PEER = -1001000000011, -1001000000012


async def test_every_settings_field_has_a_column(database):
    cur = await db._db.execute("PRAGMA table_info(settings)")
    columns = {r[1] for r in await cur.fetchall()}
    fields = {f.name for f in dataclasses.fields(db.Settings)}
    assert fields - columns == set(), "поле есть в коде, но нет в базе"


async def test_raise_photo_min_once(chat):
    await db.set_setting(chat, "prof_photo_min", 85)
    assert await db.raise_photo_min(97) == 1
    assert (await db.get_settings(chat)).prof_photo_min == 97
    assert await db.raise_photo_min(97) == 0


async def test_drop_vectors(chat):
    await db.sample_add(chat, 1, "auto", "spam", "казино тут", pid=None)
    row = (await db.samples_profile(chat))[0]
    await db.sample_set_vec(row["id"], b"x" * 1248)
    assert await db.drop_vectors() == 1
    assert (await db.sample_by_id(row["id"]))["vec"] is None


async def test_net_terms_repair(database):
    from gremlin import app

    for cid, title in ((SRC, "каментики"), (PEER, "овощехранилище 🛩")):
        await db.upsert_chat(cid, title, None, OWNER, "supergroup")
        await db.get_settings(cid)
    soon = int(time.time()) + 7 * 86400
    past = int(time.time()) - 3600
    tail = "это не чат леши · мут не-участнику невозможен, заменён баном на тот же срок"
    await db.add_punishment(SRC, 111, "oleg", "Олег", "ban", tail, soon, OWNER)
    cp1 = await db.add_punishment(PEER, 111, "oleg", "Олег", "ban",
                                  f"сетка · каментики: {tail}", None, OWNER)
    await db.add_punishment(SRC, 222, None, "Иван", "ban", tail, past, OWNER)
    cp2 = await db.add_punishment(PEER, 222, None, "Иван", "ban",
                                  f"сетка · каментики: {tail}", None, OWNER)
    forever = "бан из карточки"
    await db.add_punishment(SRC, 333, None, "Наталия", "ban", forever, None, OWNER)
    cp3 = await db.add_punishment(PEER, 333, None, "Наталия", "ban",
                                  f"сетка · каментики: {forever}", None, OWNER)

    found = {r["id"] for r in await db.net_terms_to_fix()}
    assert found == {cp1, cp2}

    bot = FakeBot()
    await app._fix_net_terms(bot)
    assert (await db.get_punishment(cp1))["until_ts"] == soon
    assert bot.banned == [(PEER, 111, soon)]
    assert bot.unbanned == [(PEER, 222)]
    assert (await db.get_punishment(cp2))["active"] == 0
    assert (await db.get_punishment(cp3))["until_ts"] is None

    bot2 = FakeBot()
    await app._fix_net_terms(bot2)             # второй раз не работает
    assert (bot2.banned, bot2.unbanned) == ([], [])
