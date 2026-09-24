"""Перенос настроек одного чата в другой.

Переносится выбранными группами — они совпадают с разделами меню, чтобы человек
не гадал, что именно поедет. Не копируется то, что привязано к конкретному чату
или человеку: получатель недельной сводки и счёт вызовов у команд (сами команды
переносятся, счёт начинается с нуля).

Медиа триггеров копируются файлами, а не ссылками на те же: иначе удаление
триггера в одном чате утащило бы картинку из другого.

Тем же путём настройки выгружаются в файл и загружаются из файла: снимок чата
один, накладывается он одинаково — из другого чата или из архива.
"""
import io
import json
import logging
import os
import re
import time
import uuid
import zipfile
from collections.abc import Callable

from .. import config, db

logger = logging.getLogger("gremlin.transfer")

# группа -> (подпись в меню, поля settings). Списки копируются отдельно, ниже.
GROUPS: dict[str, tuple[str, tuple[str, ...]]] = {
    "inline": ("🤖 Инлайн-боты", ("inline_on", "inline_punish", "inline_mute_min",
                                  "inline_spam")),
    "links": ("🔗 Ссылки", ("links_on", "extlinks_on", "mentions_check", "forwards_on", "forwards_users",
                            "lp_tg", "lm_tg", "lp_ext", "lm_ext",
                            "lp_men", "lm_men", "lp_fwd", "lm_fwd",
                            "gp_tg", "gm_tg", "gp_ext", "gm_ext",
                            "gp_men", "gm_men", "gp_fwd", "gm_fwd")),
    "anon": ("📛 Анонимы", ("anon_on",)),
    "words": ("🧨 Стоп-слова", ("words_on", "words_punish", "words_mute_min",
                                "words_guests")),
    "flood": ("🌊 Антифлуд", ("flood_on", "flood_msgs", "flood_window", "flood_mute_min")),
    "captcha": ("🤖 Капча", ("captcha_on", "captcha_timeout")),
    # канал не переносим: он свой у каждого чата, как и лог-чат
    "sub": ("📣 Вход только по подписке", ("sub_on", "sub_action", "sub_pass",
                                           "sub_dm")),
    "watch": ("👁 Наблюдение", ("watch_on", "watch_bots", "watch_suspect",
                             "watch_ban", "watch_nn", "watch_react")),
    "welcome": ("👋 Приветствие", ("welcome_on", "welcome_text")),
    "media": ("🖼 Медиа-фильтры", ("media_on", "media_mask")),
    "triggers": ("🎯 Триггеры", ("trig_on",)),
    "cmds": ("🔢 Счётчики", ("cmds_on", "cmds_guest_cd", "cmds_anywhere",
                            "cmds_bare")),
    "rates": ("💱 Курс валют", ("rates_on", "rates_cd")),
    "trust": ("🎖 Доверие", ("trust_on", "trust_soften", "trust_days",
                              "trust_msgs", "trust_mask")),
    "warns": ("⚠️ Варны", ("warns_on", "warns_limit", "warns_punish",
                             "warns_mute_min")),
    "rules": ("📜 Правила в постах", ("rules_on",)),
    "punish_cfg": ("⚙️ Настройки наказаний", ("mute_reactions", "ban_wipe")),
    "modcmds": ("⌨️ Команды чата", ("cmd_mute_on", "cmd_kick_on", "cmd_ban_on",
                                   "cmd_warn_on", "cmd_lift_on", "cmd_dm_on",
                                   "cmd_mute_min", "cmd_ban_min",
                                   "misuse_mute")),
    "cas": ("🌐 Общий список спамеров", ("cas_on", "cas_join", "cas_suspect",
                                        "cas_score")),
    "prof": ("🪪 Проверка профиля", ("prof_on", "prof_mode", "prof_punish",
                                     "prof_mute_min", "prof_score",
                                     "prof_words", "prof_members", "prof_photo",
                                     "prof_photo_min", "prof_photo_score")),
    "games": ("🎪 Приколы", ("games_on", "games_adm", "rus_punish", "rus_min",
               "duel_punish", "duel_min", "battle_punish", "battle_min",
               "court_punish", "court_min", "paste_min", "paste_cd")),
    "service": ("🧹 Системные", ("service_join", "service_leave", "service_other")),
    "read": ("🔍 Распознавание", ("ocr_on", "ocr_langs", "asr_on", "asr_max_sec")),
    "sem": ("🧠 Смысловые стоп-слова", ("sem_on", "sem_threshold", "sem_punish",
                                        "sem_mute_min", "sem_guests")),
    "burst": ("📡 Рассылки", ("burst_on", "burst_users", "burst_punish",
                              "burst_mute_min")),
    # копилка улик не переносится: она про конкретный чат и его норму
    "nn": ("🧪 Нейрофильтр", ("nn_mode", "nn_threshold")),
    "wl": ("🕊 Вайтлист", ()),
    # лог-чат не переносим: он свой у каждого чата, и подставлять чужой —
    # верный способ отправить карточки не туда
    "raid": ("🛡 Защита от набегов", ("raid_on", "raid_joins", "raid_window",
                                     "raid_action", "raid_hold")),
    "report": ("🚨 Жалобы", ("report_on", "report_who", "report_cd",
                            "report_mute_min", "report_admins")),
    "cards": ("🪪 Карточки и лог", ("cards_on", "card_mask")),
}

ALL_GROUPS = tuple(GROUPS)


def shown_groups() -> tuple[str, ...]:
    """Разделы, которые можно выбрать при переносе.

    Спрятанные из меню (медиа-фильтры, пока выключены) не предлагаем: иначе
    перенос тихо включил бы то, чего человек нигде больше не видит. В файл
    выгрузки они всё равно попадают — вдруг раздел вернут.
    """
    from .. import schema
    hidden = schema.hidden_sections()
    return tuple(g for g in ALL_GROUPS if g not in hidden)


def _db_media(path: str | None) -> bytes | None:
    """Файл заготовки из базы. None — исходник пропал.

    Путь в базе абсолютный и снят на другой машине (в контейнере он свой),
    поэтому доверяем только имени файла и ищем его сначала на месте, потом
    в старой общей папке — оттуда файлы переезжают не мгновенно.
    """
    if not path:
        return None
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    src = path if os.path.exists(path) else os.path.join(config.TRIG_DIR, name)
    if not os.path.exists(src):
        logger.warning("файл заготовки пропал: %s", path)
        return None
    with open(src, "rb") as f:
        return f.read()


_EXT_RE = re.compile(r"\.[a-z0-9]{1,5}$")


def _ext(name: str) -> str:
    m = _EXT_RE.search(name.lower())
    return m.group(0) if m else ""


def _place(data: bytes, ref: str, dst_chat: int, purpose: str) -> str:
    """Положить медиа в папку чата-получателя под новым именем.

    Копия, а не ссылка на тот же файл: иначе удаление заготовки в одном чате
    утащило бы картинку из другого.
    """
    from . import triggers
    dst = os.path.join(triggers.media_dir(dst_chat, purpose),
                       f"{uuid.uuid4().hex[:12]}{_ext(ref)}")
    with open(dst, "wb") as f:
        f.write(data)
    return dst


# ---------- снимок и применение ----------
#
# Перенос между чатами, выгрузка в файл и загрузка из файла — это один и тот же
# путь: снять снимок настроек и списков, потом наложить его на чат. Раньше
# перенос копировал сразу из базы в базу, и выгрузку пришлось бы писать второй
# копией той же логики, которая неизбежно разошлась бы с первой.

# владелец заготовок в answers -> (группа, папка медиа)
_ANSWER_GROUPS = (("welcome", "welcome"), ("rules", "rules"), ("paste", "games"))


def _answer(a) -> dict:
    return {"text": a["text"], "file": a["file_path"], "media_type": a["media_type"]}


async def snapshot(src: int, groups: set[str] | None = None) -> dict:
    """Всё, что переносится из чата, одним словарём. Медиа — путями из базы."""
    picked = set(groups) if groups is not None else set(ALL_GROUPS)
    s = await db.get_settings(src)
    snap: dict = {
        "groups": sorted(picked),
        "settings": {f: getattr(s, f) for g in sorted(picked) for f in GROUPS[g][1]},
    }
    if "words" in picked:
        snap["words"] = [{"word": r["word"], "mode": r["mode"]}
                         for r in await db.words_list(src)]
    if "sem" in picked:
        snap["phrases"] = [r["text"] for r in await db.phrases_list(src)]
    if "wl" in picked:
        snap["whitelist"] = [{"user_id": e["user_id"], "username": e["username"],
                              "title": e["title"], "scopes": sorted(e["scopes"])}
                             for e in await db.wl_entries(src)]
    if "links" in picked:
        snap["link_wl"] = [{"target_id": r["target_id"], "username": r["username"],
                            "title": r["title"]} for r in await db.link_wl_list(src)]
    if "inline" in picked:
        snap["inline_wl"] = [{"username": r["username"], "bot_id": r["bot_id"]}
                             for r in await db.inline_wl_list(src)]
    if "triggers" in picked:
        snap["triggers"] = [
            {"phrase": t["phrase"], "cooldown": t["cooldown"],
             "media_type": t["media_type"],
             "answers": [_answer(a) for a in await db.ans_list("trig", t["id"])]}
            for t in await db.trig_list(src)]
    for owner, group in _ANSWER_GROUPS:
        if group in picked:
            snap[f"{owner}_answers"] = [_answer(a) for a in await db.ans_list(owner, src)]
    if "cmds" in picked:
        # счёт вызовов не переносим: он про этот чат, в новом начинается с нуля
        snap["counters"] = [
            {"cmd": c["cmd"], "template": c["template"], "cooldown": c["cooldown"],
             "answers": [a["text"] for a in await db.ans_list("cmd", c["id"])]}
            for c in await db.cmd_list(src)]
    return snap


async def apply(dst: int, snap: dict, groups: set[str],
                fetch: Callable[[str], bytes | None]) -> dict[str, int]:
    """Наложить снимок на чат. fetch(ссылка) отдаёт байты медиа или None.

    Настройки перезаписываются, списки дополняются: стоп-слова, вайтлист,
    триггеры и счётчики, которые уже есть в чате, не трогаем. Заготовки
    приветствия, правил и ответов на пасты заменяются целиком — это один
    набор на чат, а не список.
    """
    picked = set(groups) & set(snap.get("groups", ())) & set(ALL_GROUPS)
    stats = {"полей": 0, "стоп-слов": 0, "вайтлист": 0, "чатов для ссылок": 0,
             "инлайн-ботов": 0, "триггеров": 0, "счётчиков": 0}
    await db.get_settings(dst)                 # создаст строку, если её ещё нет

    settings = snap.get("settings", {})
    for group in sorted(picked):
        for f in GROUPS[group][1]:
            if f in settings:
                await db.set_setting(dst, f, settings[f])
                stats["полей"] += 1

    async def put_answer(owner: str, owner_id: int, a: dict, purpose: str) -> bool:
        path = None
        if a.get("file"):
            data = fetch(a["file"])
            if data is None:
                return False                   # файл потерян — вариант пропускаем
            path = _place(data, a["file"], dst, purpose)
        await db.ans_add(owner, owner_id, a.get("text"), path, a.get("media_type"))
        return True

    if "words" in picked:
        have = {r["word"] for r in await db.words_list(dst)}
        for w in snap.get("words", ()):
            if w["word"] not in have:
                await db.words_add(dst, w["word"], w["mode"])
                have.add(w["word"])
                stats["стоп-слов"] += 1

    if "sem" in picked:
        have = {r["text"].lower() for r in await db.phrases_list(dst)}
        for text in snap.get("phrases", ()):
            if text.lower() not in have:
                await db.phrase_add(dst, text)
                have.add(text.lower())
                stats["фраз"] = stats.get("фраз", 0) + 1

    if "wl" in picked:
        for e in snap.get("whitelist", ()):
            if await db.wl_entry_by_key(dst, e["user_id"], e["username"]) is None:
                await db.wl_set_scopes(dst, e["user_id"], e["username"],
                                       e["title"], set(e["scopes"]))
                stats["вайтлист"] += 1

    if "links" in picked:
        for r in snap.get("link_wl", ()):
            await db.link_wl_add(dst, r["target_id"], r["username"], r["title"])
            stats["чатов для ссылок"] += 1

    if "inline" in picked:
        for r in snap.get("inline_wl", ()):
            await db.inline_wl_add(dst, r["username"], r["bot_id"])
            stats["инлайн-ботов"] += 1

    if "triggers" in picked:
        for t in snap.get("triggers", ()):
            new_id = await db.trig_add(dst, t["phrase"], None, None, t["media_type"])
            await db.trig_set(new_id, "cooldown", t["cooldown"])
            await db.ans_clear("trig", new_id)   # trig_add кладёт пустую заготовку
            for a in t["answers"]:
                await put_answer("trig", new_id, a, "trig")
            stats["триггеров"] += 1

    labels = {"welcome": "заготовок приветствия", "rules": "заготовок правил",
              "paste": "заготовок на пасты"}
    for owner, group in _ANSWER_GROUPS:
        if group not in picked:
            continue
        await db.ans_clear(owner, dst)
        for a in snap.get(f"{owner}_answers", ()):
            if await put_answer(owner, dst, a, owner):
                stats[labels[owner]] = stats.get(labels[owner], 0) + 1

    if "cmds" in picked:
        for c in snap.get("counters", ()):
            if await db.cmd_find(dst, c["cmd"]) is not None:
                continue
            if not await db.cmd_add(dst, c["cmd"], c["template"], c["cooldown"]):
                continue
            new = await db.cmd_find(dst, c["cmd"])
            await db.ans_clear("cmd", new["id"])
            for text in c["answers"]:
                await db.ans_add("cmd", new["id"], text)
            stats["счётчиков"] += 1

    return stats


async def copy_chat(src: int, dst: int, groups: set[str] | None = None) -> dict[str, int]:
    """Перенести выбранные группы. Возвращает, чего сколько скопировано."""
    picked = set(groups) if groups is not None else set(ALL_GROUPS)
    stats = await apply(dst, await snapshot(src, picked), picked, _db_media)
    logger.info("настройки %s -> %s (%s): %s", src, dst, sorted(picked), stats)
    return stats


# ---------- файл настроек ----------
#
# Zip, а не голый JSON: у триггеров и приветствий бывают картинки и голосовые,
# и без них выгрузка превращалась бы в «триггеры есть, ответов нет».
#
# Файл приходит снаружи, поэтому при загрузке ничему в нём не верим: имена
# внутри архива путями не становятся (иначе ../../ пишет куда угодно), размеры
# ограничены до распаковки, каждое поле настроек сверяется с типом и списком
# допустимых значений, а незнакомые ключи молча отбрасываются.

KIND = "gremlin-chat-settings"
FORMAT = 1
MANIFEST = "gremlin.json"
MAX_ARCHIVE = 20 * 1024 * 1024     # больше Telegram боту и не отдаст
MAX_UNPACKED = 64 * 1024 * 1024
MAX_ENTRIES = 1000
MAX_ITEMS = 5000                   # элементов в одном списке
MAX_TEXT = 4096
_MEDIA_REF = re.compile(r"^media/\d{4}(\.[a-z0-9]{1,5})?$")
_MEDIA_TYPES = {"photo", "video", "animation", "sticker", "voice", "video_note",
                "document", "audio"}


class BadArchive(ValueError):
    """Файл не подходит. Текст — для человека."""


async def export_chat(src: int) -> tuple[bytes, dict[str, int]]:
    """Собрать файл настроек чата: все группы, медиа внутри."""
    snap = await snapshot(src)
    media: dict[str, bytes] = {}

    def pack(answers: list[dict]) -> list[dict]:
        kept = []
        for a in answers:
            if a["file"]:
                data = _db_media(a["file"])
                if data is None:
                    continue                   # файл потерян — вариант пропускаем
                name = f"media/{len(media) + 1:04d}{_ext(a['file'])}"
                media[name] = data
                a = dict(a, file=name)
            kept.append(a)
        return kept

    for t in snap.get("triggers", ()):
        t["answers"] = pack(t["answers"])
    for owner, _group in _ANSWER_GROUPS:
        key = f"{owner}_answers"
        if key in snap:
            snap[key] = pack(snap[key])

    ch = await db.get_chat(src)
    manifest = {"kind": KIND, "format": FORMAT, "exported": int(time.time()),
                "chat_title": ch["title"] if ch else None, **snap}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=1))
        for name, data in media.items():
            zf.writestr(name, data)
    stats = describe(snap)
    stats["медиа"] = len(media)
    logger.info("выгрузка настроек %s: %s", src, stats)
    return buf.getvalue(), stats


def export_name(chat_id: int) -> str:
    return f"gremlin-{chat_id}-{time.strftime('%Y-%m-%d')}.zip"


def describe(snap: dict) -> dict[str, int]:
    """Что лежит в снимке — для экрана «что загрузить»."""
    out = {}
    for key, label in (("words", "стоп-слов"), ("phrases", "фраз"),
                       ("whitelist", "вайтлист"), ("link_wl", "чатов для ссылок"),
                       ("inline_wl", "инлайн-ботов"), ("triggers", "триггеров"),
                       ("counters", "счётчиков")):
        if snap.get(key):
            out[label] = len(snap[key])
    return out


def _str(v, limit: int = MAX_TEXT, empty: bool = False) -> str | None:
    if not isinstance(v, str) or (not empty and not v.strip()):
        raise BadArchive("в файле испорчен текст")
    return v[:limit]


def _opt_str(v, limit: int = 256) -> str | None:
    return None if v is None else _str(v, limit)


def _int(v, allow_none: bool = False) -> int | None:
    if v is None and allow_none:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise BadArchive("в файле испорчено число")
    return v


def _items(snap: dict, key: str, clean) -> list:
    """Список из файла: битые элементы пропускаем, а не роняем всю загрузку."""
    raw = snap.get(key) or []
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:MAX_ITEMS]:
        try:
            out.append(clean(item))
        except (BadArchive, KeyError, TypeError, AttributeError):
            continue
    return out


def _clean_settings(raw) -> dict:
    from .. import schema
    out = {}
    if not isinstance(raw, dict):
        return out
    allowed = {f for _label, keys in GROUPS.values() for f in keys}
    for key, value in raw.items():
        if key not in allowed:
            continue                           # незнакомое поле — мимо
        default = db._SETTINGS_DEFAULTS.get(key)
        if isinstance(value, bool):
            value = int(value)
        if isinstance(default, int):
            if not isinstance(value, int):
                continue
        elif isinstance(default, str):
            if not isinstance(value, str):
                continue
            value = value[:MAX_TEXT]
        elif value is not None and not isinstance(value, (int, str)):
            continue
        if isinstance(value, str) and default is None:
            value = value[:MAX_TEXT]
        if key in schema.TOGGLE_FIELDS and value not in (0, 1):
            continue
        # значение из старой версии бота, которого больше нет в пресетах —
        # не подставляем: в меню такое не выбрать и не отобразить
        if key in schema.CYCLE_FIELDS and value not in schema.CYCLE_FIELDS[key]:
            continue
        out[key] = value
    return out


def parse_archive(raw: bytes) -> tuple[dict, dict[str, bytes]]:
    """Прочитать файл настроек. Возвращает снимок и медиа. BadArchive — не подходит."""
    if len(raw) > MAX_ARCHIVE:
        raise BadArchive("файл больше 20 МБ")
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise BadArchive("это не файл выгрузки: нужен .zip, который отдал бот")
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise BadArchive("в архиве слишком много файлов")
        if sum(i.file_size for i in infos) > MAX_UNPACKED:
            raise BadArchive("архив распаковывается больше чем в 64 МБ")
        names = {i.filename for i in infos}
        if MANIFEST not in names:
            raise BadArchive("это не файл выгрузки Гремлина")
        try:
            manifest = json.loads(zf.read(MANIFEST).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise BadArchive("описание настроек в архиве испорчено")
        if not isinstance(manifest, dict) or manifest.get("kind") != KIND:
            raise BadArchive("это не файл выгрузки Гремлина")
        if not isinstance(manifest.get("format"), int) or manifest["format"] > FORMAT:
            raise BadArchive("файл из более новой версии бота — обновите бота")

        media: dict[str, bytes] = {}

        def media_ref(v) -> str | None:
            # имя из архива путём не становится никогда: только ключом словаря
            if v is None:
                return None
            if not isinstance(v, str) or not _MEDIA_REF.match(v) or v not in names:
                raise BadArchive("ссылка на медиа испорчена")
            if v not in media:
                media[v] = zf.read(v)
            return v

        def answer(a) -> dict:
            mtype = a.get("media_type")
            if mtype is not None and mtype not in _MEDIA_TYPES:
                raise BadArchive("незнакомый тип медиа")
            file = media_ref(a.get("file"))
            text = _opt_str(a.get("text"), MAX_TEXT)
            if file is None and not text:
                raise BadArchive("пустая заготовка")
            return {"text": text, "file": file, "media_type": mtype}

        groups = manifest.get("groups")
        snap: dict = {
            "groups": sorted(g for g in groups if g in GROUPS)
            if isinstance(groups, list) else [],
            "settings": _clean_settings(manifest.get("settings")),
        }
        snap["words"] = _items(manifest, "words", lambda w: {
            "word": _str(w["word"], 200),
            "mode": w["mode"] if w.get("mode") in ("strict", "stem") else "strict"})
        snap["phrases"] = _items(manifest, "phrases", lambda p: _str(p, 500))

        def wl(e) -> dict:
            scopes = [x for x in e["scopes"] if x in config.WL_SCOPES]
            if not scopes:
                raise BadArchive("пустой вайтлист")
            uid = _int(e.get("user_id"), allow_none=True)
            username = _opt_str(e.get("username"), 64)
            if uid is None and not username:
                raise BadArchive("вайтлист без человека")
            return {"user_id": uid, "username": username,
                    "title": _opt_str(e.get("title")), "scopes": scopes}
        snap["whitelist"] = _items(manifest, "whitelist", wl)

        snap["link_wl"] = _items(manifest, "link_wl", lambda r: {
            "target_id": _int(r.get("target_id"), allow_none=True),
            "username": _opt_str(r.get("username"), 64),
            "title": _opt_str(r.get("title"))})
        snap["inline_wl"] = _items(manifest, "inline_wl", lambda r: {
            "username": _str(r["username"], 64),
            "bot_id": _int(r.get("bot_id"), allow_none=True)})

        def trigger(t) -> dict:
            mtype = t.get("media_type")
            if mtype is not None and mtype not in _MEDIA_TYPES:
                raise BadArchive("незнакомый тип медиа")
            answers = []
            for a in (t.get("answers") or [])[:100]:
                try:
                    answers.append(answer(a))
                except (BadArchive, AttributeError):
                    continue
            return {"phrase": _str(t["phrase"], 200),
                    "cooldown": max(0, _int(t.get("cooldown", 30))),
                    "media_type": mtype, "answers": answers}
        snap["triggers"] = _items(manifest, "triggers", trigger)

        for owner, _group in _ANSWER_GROUPS:
            key = f"{owner}_answers"
            snap[key] = _items(manifest, key, answer)

        snap["counters"] = _items(manifest, "counters", lambda c: {
            "cmd": _str(c["cmd"], 64), "template": _str(c["template"], MAX_TEXT),
            "cooldown": max(0, _int(c.get("cooldown", 30))),
            "answers": [x[:MAX_TEXT] for x in (c.get("answers") or [])[:100]
                        if isinstance(x, str) and x.strip()]})

    snap["chat_title"] = _opt_str(manifest.get("chat_title"))
    return snap, media


# ---------- загруженный файл до подтверждения ----------
#
# Между «прислал файл» и «подтвердил, что грузить» проходит выбор галочек.
# Держим разобранный файл в памяти: он небольшой, а после перезапуска бота
# человек просто пришлёт его ещё раз.

STASH_TTL = 15 * 60
_stash: dict[tuple[int, int], tuple[float, dict, dict[str, bytes]]] = {}


def stash(user_id: int, chat_id: int, snap: dict, media: dict[str, bytes]) -> None:
    now = time.monotonic()
    for key in [k for k, v in _stash.items() if now - v[0] > STASH_TTL]:
        _stash.pop(key, None)
    _stash[(user_id, chat_id)] = (now, snap, media)


def stashed(user_id: int, chat_id: int) -> tuple[dict, dict[str, bytes]] | None:
    got = _stash.get((user_id, chat_id))
    if got is None or time.monotonic() - got[0] > STASH_TTL:
        _stash.pop((user_id, chat_id), None)
        return None
    return got[1], got[2]


def unstash(user_id: int, chat_id: int) -> None:
    _stash.pop((user_id, chat_id), None)
