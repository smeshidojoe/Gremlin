"""Перенос настроек одного чата в другой.

Переносится выбранными группами — они совпадают с разделами меню, чтобы человек
не гадал, что именно поедет. Не копируется то, что привязано к конкретному чату
или человеку: получатель недельной сводки и счёт вызовов у команд (сами команды
переносятся, счёт начинается с нуля).

Медиа триггеров копируются файлами, а не ссылками на те же: иначе удаление
триггера в одном чате утащило бы картинку из другого.
"""
import logging
import os
import shutil
import uuid

from .. import config, db

logger = logging.getLogger("gremlin.transfer")

# группа -> (подпись в меню, поля settings). Списки копируются отдельно, ниже.
GROUPS: dict[str, tuple[str, tuple[str, ...]]] = {
    "inline": ("🤖 Инлайн-боты", ("inline_on", "inline_punish", "inline_mute_min",
                                  "inline_spam")),
    "links": ("🔗 Ссылки", ("links_on", "extlinks_on", "mentions_check", "forwards_on",
                            "lp_tg", "lm_tg", "lp_ext", "lm_ext",
                            "lp_men", "lm_men", "lp_fwd", "lm_fwd",
                            "gp_tg", "gm_tg", "gp_ext", "gm_ext",
                            "gp_men", "gm_men", "gp_fwd", "gm_fwd")),
    "anon": ("📛 Анонимы", ("anon_on",)),
    "words": ("🧨 Стоп-слова", ("words_on", "words_punish", "words_mute_min",
                                "words_guests")),
    "flood": ("🌊 Антифлуд", ("flood_on", "flood_msgs", "flood_window", "flood_mute_min")),
    "captcha": ("🤖 Капча", ("captcha_on", "captcha_timeout")),
    "watch": ("👁 Наблюдение", ("watch_on", "watch_bots", "watch_suspect", "watch_ban")),
    "welcome": ("👋 Приветствие", ("welcome_on", "welcome_text")),
    "media": ("🖼 Медиа-фильтры", ("media_on", "media_mask")),
    "triggers": ("🎯 Триггеры", ("trig_on",)),
    "cmds": ("🔢 Счётчики", ("cmds_on", "cmds_guest_cd", "cmds_anywhere")),
    "trust": ("🎖 Доверие", ("trust_on", "trust_soften", "trust_days",
                              "trust_msgs", "trust_mask")),
    "warns": ("⚠️ Варны", ("warns_on", "warns_limit", "warns_punish",
                             "warns_mute_min")),
    "rules": ("📜 Правила в постах", ("rules_on",)),
    "punish_cfg": ("⚙️ Настройки наказаний", ("misuse_mute",)),
    "games": ("🎪 Приколы", ("games_on", "games_adm", "rus_punish", "rus_min",
               "duel_punish", "duel_min", "battle_punish", "battle_min",
               "court_punish", "court_min")),
    "service": ("🧹 Системные", ("service_join", "service_leave", "service_other")),
    "wl": ("🕊 Вайтлист", ()),
    "cards": ("🪪 Карточки и лог", ("cards_on", "card_mask", "log_chat_id")),
}

ALL_GROUPS = tuple(GROUPS)


def _copy_media(path: str, dst_chat: int) -> str | None:
    """Скопировать файл триггера под новым именем. None — исходник пропал."""
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    src = os.path.join(config.TRIG_DIR, name)
    if not os.path.exists(src):
        logger.warning("файл триггера пропал: %s", src)
        return None
    ext = os.path.splitext(name)[1]
    new_name = f"{dst_chat}_{uuid.uuid4().hex[:12]}{ext}"
    shutil.copy2(src, os.path.join(config.TRIG_DIR, new_name))
    return os.path.join(config.TRIG_DIR, new_name)


async def copy_chat(src: int, dst: int, groups: set[str] | None = None) -> dict[str, int]:
    """Перенести выбранные группы. Возвращает, чего сколько скопировано."""
    picked = set(groups) if groups is not None else set(ALL_GROUPS)
    stats = {"полей": 0, "стоп-слов": 0, "вайтлист": 0, "чатов для ссылок": 0,
             "инлайн-ботов": 0, "триггеров": 0, "счётчиков": 0}

    s = await db.get_settings(src)
    await db.get_settings(dst)                 # создаст строку, если её ещё нет
    for group in picked:
        for f in GROUPS[group][1]:
            await db.set_setting(dst, f, getattr(s, f))
            stats["полей"] += 1

    if "words" in picked:
        have = {r["word"] for r in await db.words_list(dst)}
        for r in await db.words_list(src):
            if r["word"] not in have:
                await db.words_add(dst, r["word"], r["mode"])
                stats["стоп-слов"] += 1

    if "wl" in picked:
        for e in await db.wl_entries(src):
            if await db.wl_entry_by_key(dst, e["user_id"], e["username"]) is None:
                await db.wl_set_scopes(dst, e["user_id"], e["username"],
                                       e["title"], e["scopes"])
                stats["вайтлист"] += 1

    if "links" in picked:
        for r in await db.link_wl_list(src):
            await db.link_wl_add(dst, r["target_id"], r["username"], r["title"])
            stats["чатов для ссылок"] += 1

    if "inline" in picked:
        for r in await db.inline_wl_list(src):
            await db.inline_wl_add(dst, r["username"], r["bot_id"])
            stats["инлайн-ботов"] += 1

    if "triggers" in picked:
        for t in await db.trig_list(src):
            new_id = await db.trig_add(dst, t["phrase"], None, None, t["media_type"])
            await db.trig_set(new_id, "cooldown", t["cooldown"])
            await db.ans_clear("trig", new_id)   # trig_add кладёт пустую заготовку
            for a in await db.ans_list("trig", t["id"]):
                path = _copy_media(a["file_path"], dst) if a["file_path"] else None
                if a["file_path"] and path is None:
                    continue                     # файл потерян — вариант пропускаем
                await db.ans_add("trig", new_id, a["text"], path, a["media_type"])
            stats["триггеров"] += 1

    if "rules" in picked:
        # заготовки правил живут в answers, как варианты ответов триггеров
        await db.ans_clear("rules", dst)
        for a in await db.ans_list("rules", src):
            path = _copy_media(a["file_path"], dst) if a["file_path"] else None
            if a["file_path"] and path is None:
                continue
            await db.ans_add("rules", dst, a["text"], path, a["media_type"])
            stats["заготовок правил"] =                 stats.get("заготовок правил", 0) + 1

    if "cmds" in picked:
        for c in await db.cmd_list(src):
            if await db.cmd_find(dst, c["cmd"]) is not None:
                continue
            if not await db.cmd_add(dst, c["cmd"], c["template"], c["cooldown"]):
                continue
            new = await db.cmd_find(dst, c["cmd"])
            await db.ans_clear("cmd", new["id"])  # счёт с нуля, ответы переносим
            for a in await db.ans_list("cmd", c["id"]):
                await db.ans_add("cmd", new["id"], a["text"])
            stats["счётчиков"] += 1

    logger.info("настройки %s -> %s (%s): %s", src, dst, sorted(picked), stats)
    return stats
