"""Перенос настроек, выгрузка в файл и загрузка из файла.

Все три идут одним путём: снимок чата и наложение снимка. Здесь проверяем,
что перенос между чатами после переделки ведёт себя как раньше, что файл
переживает круг «выгрузил — загрузил» вместе с медиа, и что подсунутый архив
не пишет куда не надо и не протаскивает мусор в настройки.
"""
import io
import json
import os
import zipfile

import pytest

from gremlin import db
from gremlin.handlers import user_menu as um
from gremlin.services import transfer

from conftest import CB, CHAT, OWNER

DST = CHAT - 7
PICTURE = b"\x89PNG\r\n\x1a\n" + b"x" * 200


async def _seed(tmp_path):
    await db.set_setting(CHAT, "words_on", 1)
    await db.set_setting(CHAT, "cmd_ban_on", 0)
    await db.set_setting(CHAT, "welcome_text", "привет")
    await db.words_add(CHAT, "казино", "stem")
    await db.words_add(CHAT, "ставки", "strict")
    await db.wl_set_scopes(CHAT, 555, "friend", None, {"links", "words"})
    pic = tmp_path / "pic.png"
    pic.write_bytes(PICTURE)
    tid = await db.trig_add(CHAT, "котик", None, None, "photo")
    await db.ans_clear("trig", tid)
    await db.ans_add("trig", tid, "вот котик", str(pic), "photo")
    await db.ans_add("welcome", CHAT, "добро пожаловать")
    return tid


async def _dst():
    await db.upsert_chat(DST, "Второй", None, OWNER, "supergroup")
    await db.get_settings(DST)


async def _check_copied(dst):
    s = await db.get_settings(dst)
    assert s.words_on == 1 and s.cmd_ban_on == 0 and s.welcome_text == "привет"
    words = {r["word"]: r["mode"] for r in await db.words_list(dst)}
    assert words == {"казино": "stem", "ставки": "strict"}
    wl = await db.wl_entries(dst)
    assert [(e["user_id"], e["scopes"]) for e in wl] == [(555, {"links", "words"})]
    trig = (await db.trig_list(dst))[0]
    ans = (await db.ans_list("trig", trig["id"]))[0]
    assert ans["text"] == "вот котик" and ans["media_type"] == "photo"
    # медиа — своя копия, а не ссылка на файл исходного чата
    assert os.path.exists(ans["file_path"]) and str(dst) in ans["file_path"]
    with open(ans["file_path"], "rb") as f:
        assert f.read() == PICTURE
    assert [a["text"] for a in await db.ans_list("welcome", dst)] == ["добро пожаловать"]


async def test_copy_between_chats_still_works(chat, tmp_path):
    await _seed(tmp_path)
    await _dst()
    stats = await transfer.copy_chat(CHAT, DST)
    assert stats["стоп-слов"] == 2 and stats["триггеров"] == 1
    await _check_copied(DST)


async def test_copy_is_additive_for_lists(chat, tmp_path):
    await _seed(tmp_path)
    await _dst()
    await db.words_add(DST, "своё", "strict")
    await transfer.copy_chat(CHAT, DST, {"words"})
    await transfer.copy_chat(CHAT, DST, {"words"})     # повтор не плодит дубли
    assert sorted(r["word"] for r in await db.words_list(DST)) == ["казино", "своё", "ставки"]


async def test_file_round_trip(chat, tmp_path):
    await _seed(tmp_path)
    raw, stats = await transfer.export_chat(CHAT)
    assert stats["медиа"] == 1
    snap, media = transfer.parse_archive(raw)
    assert snap["chat_title"] == "Чат"
    await _dst()
    await transfer.apply(DST, snap, set(snap["groups"]), media.get)
    await _check_copied(DST)


async def test_lost_media_file_skips_only_that_answer(chat, tmp_path):
    tid = await _seed(tmp_path)
    os.remove(tmp_path / "pic.png")
    raw, stats = await transfer.export_chat(CHAT)
    assert stats["медиа"] == 0
    snap, _media = transfer.parse_archive(raw)
    trig = snap["triggers"][0]
    assert trig["phrase"] == "котик" and trig["answers"] == []
    assert tid


# ---------- подсунутые архивы ----------

def _zip(manifest=None, extra=None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if manifest is not None:
            zf.writestr(transfer.MANIFEST, json.dumps(manifest))
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return buf.getvalue()


def _manifest(**kw):
    base = {"kind": transfer.KIND, "format": transfer.FORMAT, "groups": ["words"],
            "settings": {}, "words": []}
    base.update(kw)
    return base


@pytest.mark.parametrize("raw, why", [
    (b"not a zip at all", "не файл выгрузки"),
    (_zip(extra={"readme.txt": "hi"}), "не файл выгрузки"),
    (_zip({"kind": "other", "format": 1}), "не файл выгрузки"),
    (_zip(_manifest(format=99)), "более новой версии"),
])
def test_rejects_foreign_files(raw, why):
    with pytest.raises(transfer.BadArchive) as err:
        transfer.parse_archive(raw)
    assert why in str(err.value)


def test_zip_bomb_is_refused_before_unpacking(monkeypatch):
    monkeypatch.setattr(transfer, "MAX_UNPACKED", 1000)
    raw = _zip(_manifest(), {"media/0001.png": b"\0" * 5000})
    with pytest.raises(transfer.BadArchive):
        transfer.parse_archive(raw)


def test_media_names_never_become_paths():
    """Имя вида ../../ не должно ни читаться, ни попасть в снимок."""
    evil = {"text": None, "file": "media/../../../gremlin.sqlite3", "media_type": "photo"}
    good = {"text": None, "file": "media/0001.png", "media_type": "photo"}
    raw = _zip(_manifest(groups=["welcome"], welcome_answers=[evil, good]),
               {"media/0001.png": PICTURE, "media/../../../gremlin.sqlite3": b"x"})
    snap, media = transfer.parse_archive(raw)
    assert [a["file"] for a in snap["welcome_answers"]] == ["media/0001.png"]
    assert list(media) == ["media/0001.png"]


async def test_bad_settings_are_dropped_good_ones_kept(chat):
    raw = _zip(_manifest(
        groups=["words", "modcmds", "flood"],
        settings={
            "words_on": 1,               # годится
            "cmd_kick_on": 0,            # годится
            "flood_on": 7,               # тумблер не бывает 7
            "flood_msgs": "много",       # число строкой
            "misuse_mute": 123456,       # нет такого пресета
            "log_chat_id": -100500,      # поле не из переносимых
            "owner_id; DROP TABLE": 1,   # незнакомый ключ
        },
        words=[{"word": "казино", "mode": "stem"}, {"word": ""}, "мусор",
               {"word": "ok", "mode": "evil"}],
    ))
    snap, media = transfer.parse_archive(raw)
    assert snap["settings"] == {"words_on": 1, "cmd_kick_on": 0}
    assert snap["words"] == [{"word": "казино", "mode": "stem"},
                             {"word": "ok", "mode": "strict"}]
    await transfer.apply(CHAT, snap, set(snap["groups"]), media.get)
    s = await db.get_settings(CHAT)
    assert s.cmd_kick_on == 0 and s.log_chat_id is None


# ---------- меню: загрузка из файла ----------

async def test_menu_applies_stashed_file(chat, tmp_path):
    await _seed(tmp_path)
    raw, _stats = await transfer.export_chat(CHAT)
    await _dst()
    snap, media = transfer.parse_archive(raw)
    transfer.stash(OWNER, DST, snap, media)

    class State:
        data = {"copy_groups": ["words", "triggers"]}

        async def get_data(self):
            return dict(self.data)

        async def clear(self):
            self.data = {}

    cb = CB(f"u:cpd:{DST}:{um.FROM_FILE}")
    await um.cb_copy_do(cb, State())
    assert "из файла" in cb.message.text
    assert {r["word"] for r in await db.words_list(DST)} == {"казино", "ставки"}
    assert len(await db.trig_list(DST)) == 1
    assert (await db.get_settings(DST)).welcome_text is None   # раздел не отмечали
    assert transfer.stashed(OWNER, DST) is None                # файл больше не висит


async def test_forgotten_file_is_reported(chat):
    await _dst()
    transfer.unstash(OWNER, DST)
    cb = CB(f"u:cpg:{DST}:{um.FROM_FILE}:words")

    class State:
        async def get_data(self):
            return {}

        async def update_data(self, **kw):
            pass

    await um.cb_copy_toggle(cb, State())
    assert "забыт" in cb.alerts[-1]


def test_stash_expires(monkeypatch):
    transfer.stash(1, 2, {"groups": []}, {})
    assert transfer.stashed(1, 2) is not None
    monkeypatch.setattr(transfer, "STASH_TTL", -1)
    assert transfer.stashed(1, 2) is None
