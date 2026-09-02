"""HTTP-API панели: те же действия, что и в меню бота, только по JSON.

Правило одно: браузеру нельзя доверять. Каждый запрос приносит подпись
Telegram (её проверяет auth), а каждый запрос про чат ещё и сверяется с базой —
владеет ли этот человек этим чатом. Ровно как _guard в меню: id чата приходит
снаружи, и подставить чужой ничего не стоит.

Логику не дублируем: где меню зовёт сервис или помощник из user_menu, панель
зовёт его же. Иначе два интерфейса неизбежно разъедутся в поведении.
"""
import asyncio
import json
import logging
import os
import re
import time

from aiohttp import web

from .. import config, db, schema, utils
from ..handlers import fun as fun_h, user_menu as um
from ..services import (adm_cache, cas, digest as digest_svc, filters as flt,
                        media, moderation, net as net_svc, nn, resolve, transfer)
from . import auth

logger = logging.getLogger("gremlin.web.api")

routes = web.RouteTableDef()

# сколько строк отдаём в списках, которые в меню листались страницами:
# на странице листать не нужно, но и всю базу тянуть незачем
EVENTS_LIMIT = 60
ACTIVE_LIMIT = 300


# ---------- мелкие помощники ----------

def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def js(payload, status: int = 200) -> web.Response:
    return web.json_response(payload, status=status, dumps=_dumps)


def rows(seq) -> list[dict]:
    return [dict(r) for r in seq]


def bot_of(request) -> "object":
    return request.app["bot"]


def uid_of(request) -> int:
    return request["user"]["id"]


def is_owner(request) -> bool:
    return uid_of(request) in config.ADMIN_IDS


async def cid_of(request) -> int:
    """id чата из пути + проверка прав. Чужой чат — 403 и никаких данных."""
    try:
        cid = int(request.match_info["cid"])
    except (KeyError, ValueError):
        raise web.HTTPBadRequest(text="bad chat id")
    if not await auth.owns(uid_of(request), cid):
        raise web.HTTPForbidden(text="not your chat")
    return cid


def owner_only(request) -> None:
    if not is_owner(request):
        raise web.HTTPForbidden(text="owner only")


async def body(request) -> dict:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _strip_tags(text: str | None) -> str:
    return re.sub(r"<[^>]+>", "", text or "")


# ---------- главная ----------

async def _chat_brief(bot, row) -> dict:
    """Строка чата для списка: название плюс канал, к которому он прицеплен."""
    cid = row["chat_id"]
    try:
        _, _, linked = await adm_cache.linked_chat(bot, cid)
    except Exception:
        linked = None
    return {
        "chat_id": cid,
        "title": row["title"] or str(cid),
        "username": row["username"],
        "owner_id": row["owner_id"],
        # имя владельца: у владельца бота в списке чаты нескольких человек,
        # и без подписи непонятно, чей какой
        "owner": await db.user_label(row["owner_id"]) if row["owner_id"] else None,
        "linked": linked,
    }


@routes.get("/api/init")
async def api_init(request: web.Request) -> web.Response:
    """Всё, что нужно первому экрану: кто я, мои чаты, мои сетки."""
    bot = bot_of(request)
    uid = uid_of(request)
    chats = await db.chats_for(uid)
    me = await bot.me()
    payload = {
        "user": request["user"],
        "owner": is_owner(request),
        "bot_username": me.username,
        "add_url": f"https://t.me/{me.username}?startgroup=true&admin={um._ADD_RIGHTS}",
        "chats": [await _chat_brief(bot, c) for c in chats],
        "nets": len(await _nets_for(uid)),
    }
    if payload["owner"]:
        gl = await db.global_log()
        gl_chat = await db.get_chat(gl) if gl else None
        payload["global_log"] = {
            "chat_id": gl,
            "title": (gl_chat["title"] if gl_chat and gl_chat["title"] else
                      (str(gl) if gl else None)),
        }
    return js(payload)


# ---------- карточка чата ----------

async def _log_label(chat_id: int | None) -> str | None:
    if not chat_id:
        return None
    ch = await db.get_chat(chat_id)
    return (ch["title"] if ch and ch["title"] else str(chat_id))


@routes.get("/api/chat/{cid}")
async def api_chat(request: web.Request) -> web.Response:
    """Дашборд чата: данные, сводка тумблеров, список разделов."""
    cid = await cid_of(request)
    uid = uid_of(request)
    ch = await db.get_chat(cid)
    s = await db.get_settings(cid)
    st = await db.chat_stats(cid)
    net = await db.net_of_chat(cid)

    # разделы верхнего уровня; подстраницы (у них back) открываются изнутри
    sections = []
    for sec in schema.SECTIONS:
        if sec.back:
            continue
        if sec.key == "digest" and digest_svc.tracked_chat() != cid:
            continue
        sections.append({"key": sec.key, "title": sec.title,
                         "on": bool(getattr(s, sec.fields[0].key))
                         if sec.fields and sec.fields[0].kind == "toggle" else None})

    return js({
        "chat": {
            "chat_id": cid,
            "title": ch["title"] if ch else str(cid),
            "username": ch["username"] if ch else None,
            "owner_id": ch["owner_id"] if ch else None,
            "owner_name": (await db.user_handle(ch["owner_id"])
                           if is_owner(request) and ch and ch["owner_id"] else None),
        },
        "stats": st,
        "active": await db.active_punishments_count(cid),
        "warned": len(await db.warn_users(cid)),
        "log_chat": {"chat_id": s.log_chat_id, "title": await _log_label(s.log_chat_id)},
        "net": {"id": net["id"], "title": net["title"]} if net else None,
        "overview": [{"key": k, "label": lbl, "on": bool(getattr(s, k))}
                     for k, lbl in schema.OVERVIEW],
        "sections": sections,
        "needs_setup": await um.needs_setup(cid, uid),
        "games_on": bool(s.games_on),
    })


@routes.get("/api/chat/{cid}/stats")
async def api_stats(request: web.Request) -> web.Response:
    """Статистика чата с расшифровкой топа — тот же экран, что в меню."""
    cid = await cid_of(request)
    st = await db.chat_stats(cid)
    top = []
    for uid, cnt in st.get("top", []):
        u = await db.get_user(uid)
        name = (u["first_name"] if u else None) or ""
        uname = f"@{u['username']}" if u and u["username"] else ""
        top.append({"user_id": uid, "count": cnt,
                    "who": " ".join(x for x in (name, uname) if x) or str(uid)})
    st = dict(st)
    st["top"] = top
    return js(st)


@routes.get("/api/chat/{cid}/events")
async def api_events(request: web.Request) -> web.Response:
    """Журнал чата: что бот делал и почему."""
    cid = await cid_of(request)
    out = []
    for r in await db.recent_events(EVENTS_LIMIT, chat_id=cid):
        out.append({"kind": r["kind"], "ts": r["ts"], "when": utils.rel_time(r["ts"]),
                    "text": _strip_tags(await db.names_in(r["text"]))})
    return js({"items": out})


# ---------- разделы настроек ----------

@routes.get("/api/chat/{cid}/section/{sec}")
async def api_section(request: web.Request) -> web.Response:
    cid = await cid_of(request)
    sec = request.match_info["sec"]
    section = schema.SECTION_BY_KEY.get(sec)
    if section is None:
        raise web.HTTPNotFound(text="no such section")
    s = await db.get_settings(cid)
    data = schema.section_dict(section, s)
    data["widget_data"] = {w: await _widget(cid, w, s) for w in section.widgets}
    if sec == "digest":
        data["digest_state"] = await asyncio.to_thread(_digest_state)
    return js(data)


def _digest_state() -> dict | None:
    d = digest_svc.collect(config.STATS_DB)
    if d is None:
        return None
    return {"members": d["members"], "silent": len(d["silent"]),
            "period": d.get("period", "—"), "updated": d["updated"],
            "full": d.get("days", 7) >= 7}


async def _widget(cid: int, widget: str, s) -> dict:
    """Данные списочных частей раздела. Имена те же, что в меню бота."""
    if widget == "anon":
        allowed = [r for r in await db.wl_list(cid) if r["scope"] in ("all", "anon")]
        return {"count": len(allowed)}

    if widget == "links_pun":
        return {"member": "links_member", "guest": "links_guest"}

    if widget == "link_wl":
        return {"count": len(await db.link_wl_list(cid))}

    if widget == "inline_wl":
        return {"items": rows(await db.inline_wl_list(cid))}

    if widget == "words":
        return {"count": len(await db.words_list(cid))}

    if widget == "wl":
        out = []
        for e in await db.wl_entries(cid):
            out.append({
                "row_id": e["row_id"], "user_id": e["user_id"],
                "username": e["username"], "title": e["title"],
                "who": e["title"] or await db.user_label(e["user_id"], e["username"]),
                "scopes": sorted(e["scopes"]),
                "label": um._wl_scopes_label(e["scopes"]),
            })
        return {"items": out,
                "scopes": [{"key": k, "label": config.WL_SCOPE_LABELS[k]}
                           for k in config.WL_SCOPES]}

    if widget == "logsel":
        return {"chat_id": s.log_chat_id, "title": await _log_label(s.log_chat_id)}

    if widget == "phrases":
        return {"items": [{"id": r["id"], "text": r["text"], "hits": r["hits"]}
                          for r in await db.phrases_list(cid)],
                "limit": config.SEM_LIMIT, "model": nn.status()}

    if widget == "read_stats":
        return {"ocr": media.status(), "asr": media.asr_status(),
                "asr_url": bool(config.ASR_URL)}

    if widget == "nn_subs":
        return {"sem_on": bool(s.sem_on), "burst_on": bool(s.burst_on),
                "phrases": len(await db.phrases_list(cid))}

    if widget == "watch_subs":
        return {"cas_on": bool(s.cas_on)}

    if widget == "cas_stats":
        st = await db.cas_stats()
        st["service"] = cas.status()
        return st

    if widget == "nn_clusters":
        # сами кучки считаются по кнопке: на тысяче улик это секунда-другая,
        # и держать раздел закрытым всё это время незачем
        st = await db.samples_stats(cid)
        return {"unknown": st["unknown"], "profile": st["profile"],
                "model": nn.status()}

    if widget == "nn_stats":
        st = await db.samples_stats(cid)
        st["suggest"] = await nn.suggest_threshold(cid)
        st["threshold"] = s.nn_threshold
        st["min"] = config.NN_MIN_SAMPLES
        st["model"] = nn.status()
        st["logreg_min"] = config.NN_LOGREG_MIN
        return st

    if widget == "cardbits":
        return {"bits": [{"bit": bit, "label": lbl, "on": bool(s.card_mask & bit)}
                         for bit, lbl in config.CARD_BITS]}

    if widget == "mediabits":
        return {"bits": [{"bit": bit, "label": lbl, "on": bool(s.media_mask & bit)}
                         for bit, _key, lbl in config.MEDIA_BITS]}

    if widget == "trustbits":
        return {"bits": [{"bit": bit, "label": lbl, "on": bool(s.trust_mask & bit)}
                         for bit, lbl in config.TRUST_BITS]}

    if widget == "trustsoft":
        return {"on": sum(1 for bit, _ in config.TRUST_BITS if s.trust_mask & bit),
                "total": len(config.TRUST_BITS)}

    if widget == "welcome_text":
        return {"count": len(await db.ans_list("welcome", cid)),
                "legacy": bool(s.welcome_text)}

    if widget == "rules_text":
        return {"count": len(await db.ans_list("rules", cid))}

    if widget == "warnlist":
        return {"count": len(await db.warn_users(cid))}

    if widget == "trigs":
        return {"count": len(await db.trig_list(cid)), "limit": config.TRIG_LIMIT}

    if widget == "cmds":
        return {"count": len(await db.cmd_list(cid)), "limit": config.CMD_LIMIT}

    if widget == "digest_to":
        return {"to": s.digest_to,
                "who": await db.user_label(s.digest_to) if s.digest_to else None}

    return {}


@routes.post("/api/chat/{cid}/set")
async def api_set(request: web.Request) -> web.Response:
    """Поле из схемы: тумблер или значение селектора."""
    cid = await cid_of(request)
    data = await body(request)
    key = data.get("key")
    try:
        value = schema.validate(key, data.get("value"))
    except ValueError as e:
        raise web.HTTPBadRequest(text=str(e))
    await db.set_setting(cid, key, value)
    await _after_set(cid, key, value)
    sec = schema.FIELD_SECTION.get(key)
    s = await db.get_settings(cid)
    out = schema.section_dict(schema.SECTION_BY_KEY[sec], s)
    out["widget_data"] = {w: await _widget(cid, w, s)
                          for w in schema.SECTION_BY_KEY[sec].widgets}
    return js(out)


async def _after_set(cid: int, key: str, value) -> None:
    """Побочные эффекты, которые есть и в меню бота."""
    if key in ("words_on", "words_guests"):
        flt.invalidate_words(cid)


@routes.post("/api/chat/{cid}/phrases")
async def api_phrase_add(request: web.Request) -> web.Response:
    """Добавить фразу-образец. Каждая строка — отдельная фраза."""
    cid = await cid_of(request)
    data = await body(request)
    have = len(await db.phrases_list(cid))
    added = dupes = 0
    for raw in str(data.get("text") or "").split("\n"):
        line = raw.strip()
        if len(line) < 10 or have + added >= config.SEM_LIMIT:
            continue
        if await db.phrase_add(cid, line):
            added += 1
        else:
            dupes += 1
    nn.invalidate_phrases(cid)
    return js({"added": added, "dupes": dupes})


@routes.delete("/api/chat/{cid}/phrases/{rid}")
async def api_phrase_del(request: web.Request) -> web.Response:
    cid = await cid_of(request)
    await db.phrase_del(cid, int(request.match_info["rid"]))
    nn.invalidate_phrases(cid)
    return js({"ok": True})


@routes.get("/api/chat/{cid}/nn/doubt")
async def api_nn_doubt(request: web.Request) -> web.Response:
    """Улики, на которых фильтр колеблется, — их и стоит разметить руками."""
    cid = await cid_of(request)
    return js({"items": await nn.doubtful(cid), "model": nn.status()})


@routes.post("/api/chat/{cid}/nn/doubt")
async def api_nn_doubt_mark(request: web.Request) -> web.Response:
    cid = await cid_of(request)
    data = await body(request)
    label = data.get("label")
    if label not in ("spam", "ok"):
        raise web.HTTPBadRequest(text="bad label")
    await db.sample_relabel(int(data.get("id") or 0), label, origin="card")
    nn.invalidate(cid)
    await db.add_event(cid, "nn", f"улика {data.get('id')} размечена как {label} "
                                  f"(панель)")
    return js({"ok": True})


@routes.get("/api/chat/{cid}/nn/clusters")
async def api_nn_clusters(request: web.Request) -> web.Response:
    """Разбивка копилки на кучки похожих улик — для раздела «виды спама»."""
    cid = await cid_of(request)
    scope = request.query.get("scope", "unknown")
    if scope not in ("unknown", "profile"):
        raise web.HTTPBadRequest(text="bad scope")
    return js({"scope": scope, "items": await nn.clusters(cid, scope),
               "model": nn.status(), "min": config.NN_MIN_SAMPLES})


@routes.post("/api/chat/{cid}/nn/clusters")
async def api_nn_cluster_label(request: web.Request) -> web.Response:
    """Разметить кучку целиком: сотня улик одним нажатием вместо ста карточек."""
    cid = await cid_of(request)
    data = await body(request)
    label = data.get("label")
    if label not in ("spam", "ok"):
        raise web.HTTPBadRequest(text="bad label")
    moved = await nn.label_cluster(cid, int(data.get("index") or 0), label)
    if moved:
        await db.add_event(cid, "nn", f"кучка размечена как {label}: {moved} улик "
                                      f"(панель)")
    return js({"moved": moved})


@routes.post("/api/chat/{cid}/bit")
async def api_bit(request: web.Request) -> web.Response:
    """Переключить один бит маски: карточки, медиа, доверие, игры."""
    cid = await cid_of(request)
    data = await body(request)
    key, bit = data.get("key"), int(data.get("bit") or 0)
    if key not in ("card_mask", "media_mask", "trust_mask", "games_on", "games_adm"):
        raise web.HTTPBadRequest(text="bad mask")
    s = await db.get_settings(cid)
    await db.set_setting(cid, key, getattr(s, key) ^ bit)
    return js({"value": getattr(await db.get_settings(cid), key)})


# текстовые поля, которые панель вправе менять напрямую
_TEXT_FIELDS: dict[str, int] = {}


@routes.post("/api/chat/{cid}/text")
async def api_text(request: web.Request) -> web.Response:
    cid = await cid_of(request)
    data = await body(request)
    key = data.get("key")
    if key not in _TEXT_FIELDS:
        raise web.HTTPBadRequest(text="bad field")
    value = (data.get("value") or "").strip()[:_TEXT_FIELDS[key]]
    await db.set_setting(cid, key, value or None)
    return js({"value": value})


# ---------- стоп-слова ----------

@routes.get("/api/chat/{cid}/words")
async def api_words(request: web.Request) -> web.Response:
    cid = await cid_of(request)
    return js({"items": [{"id": r["id"], "word": r["word"], "mode": r["mode"],
                          "label": um._word_label(r["word"], r["mode"])}
                         for r in await db.words_list(cid)]})


@routes.post("/api/chat/{cid}/words")
async def api_words_add(request: web.Request) -> web.Response:
    """Список через запятую или с новой строки; звёздочка = любые окончания."""
    cid = await cid_of(request)
    text = (await body(request)).get("text") or ""
    added = dupes = 0
    for raw in text.replace("\n", ",").split(","):
        w = raw.strip().lower()
        if not w:
            continue
        mode = "stem" if w.endswith("*") else "strict"
        w = w.rstrip("*")
        if not w:
            continue
        if await db.words_add(cid, w, mode):
            added += 1
        else:
            dupes += 1
    flt.invalidate_words(cid)
    return js({"added": added, "dupes": dupes})


@routes.delete("/api/chat/{cid}/words/{rid}")
async def api_words_del(request: web.Request) -> web.Response:
    cid = await cid_of(request)
    await db.words_remove(int(request.match_info["rid"]))
    flt.invalidate_words(cid)
    return js({"ok": True})


@routes.post("/api/chat/{cid}/words/clear")
async def api_words_clear(request: web.Request) -> web.Response:
    cid = await cid_of(request)
    n = await db.words_clear(cid)
    flt.invalidate_words(cid)
    return js({"removed": n})


# ---------- вайтлист людей ----------

@routes.post("/api/chat/{cid}/wl")
async def api_wl_add(request: web.Request) -> web.Response:
    """Добавить в вайтлист по id или @username — сразу с полным игнором."""
    cid = await cid_of(request)
    token = ((await body(request)).get("target") or "").strip()
    user_id, username, title = None, None, None
    if token.lstrip("-").isdigit():
        user_id = int(token)
        for probe in db.id_variants(user_id):        # канал — подтянем название
            try:
                ch = await bot_of(request).get_chat(probe)
                if getattr(ch, "title", None):
                    user_id, title, username = probe, ch.title, ch.username
                    break
            except Exception:
                continue
    elif token.startswith("@") and len(token) > 3:
        username = token
        user_id, title = await resolve.by_username(bot_of(request), token)
    else:
        raise web.HTTPBadRequest(text="Нужен id или @username.")

    uname = (username or None) and username.lower().lstrip("@")
    exists = await db.wl_entry_by_key(cid, user_id, uname)
    note = "Он уже в вайтлисте."
    if exists is None:
        await db.wl_set_scopes(cid, user_id, uname, title, {"all"})
        exists = await db.wl_entry_by_key(cid, user_id, uname)
        note = "Добавлен с полным игнором."
        if user_id:      # был забанен как анонимный отправитель — снимаем бан
            p = await db.active_punishment_of(cid, user_id, "banchan")
            if p is not None:
                ok, msg, _ = await moderation.lift_punishment(
                    bot_of(request), p["id"], invite=False)
                note += " Бан канала снят." if ok else f" Бан канала снять не вышло: {msg}"
                if ok:
                    await db.add_event(cid, "anon",
                                       f"разбан канала по вайтлисту: {title or user_id}")
    return js({"row_id": exists["row_id"], "note": note})


@routes.post("/api/chat/{cid}/wl/{rid}/scope")
async def api_wl_scope(request: web.Request) -> web.Response:
    """Галочка уровня игнора. Правила «полного игнора» те же, что в меню."""
    cid = await cid_of(request)
    rid = int(request.match_info["rid"])
    scope = (await body(request)).get("scope")
    if scope not in config.WL_SCOPES:
        raise web.HTTPBadRequest(text="bad scope")
    e = await db.wl_entry(cid, rid)
    if e is None:
        raise web.HTTPNotFound(text="no entry")
    on = um._wl_effective(e["scopes"])
    on = (set() if "all" in e["scopes"] else set(config.WL_SCOPES)) if scope == "all" \
        else (on - {scope} if scope in on else on | {scope})
    await db.wl_set_scopes(cid, e["user_id"], e["username"], e["title"], um._wl_pack(on))
    upd = await db.wl_entry(cid, rid)
    return js({"scopes": sorted(upd["scopes"]) if upd else [],
               "label": um._wl_scopes_label(upd["scopes"]) if upd else ""})


@routes.delete("/api/chat/{cid}/wl/{rid}")
async def api_wl_del(request: web.Request) -> web.Response:
    cid = await cid_of(request)
    e = await db.wl_entry(cid, int(request.match_info["rid"]))
    if e is not None:
        await db.wl_set_scopes(cid, e["user_id"], e["username"], e["title"], set())
    return js({"ok": True})


# ---------- разрешённые чаты и каналы (ссылки) ----------

@routes.get("/api/chat/{cid}/linkwl")
async def api_linkwl(request: web.Request) -> web.Response:
    cid = await cid_of(request)
    return js({"items": rows(await db.link_wl_list(cid))})


@routes.post("/api/chat/{cid}/linkwl")
async def api_linkwl_add(request: web.Request) -> web.Response:
    cid = await cid_of(request)
    token = ((await body(request)).get("target") or "").strip()
    target_id, uname, title = None, None, None
    if token.lstrip("-").isdigit():
        target_id = int(token)
        for probe in db.id_variants(target_id):
            try:
                ch = await bot_of(request).get_chat(probe)
                target_id, title, uname = probe, getattr(ch, "title", None), ch.username
                break
            except Exception:
                continue
    elif token.startswith("@") and len(token) > 3:
        uname = token.lstrip("@")
        target_id, title = await resolve.by_username(bot_of(request), token)
    else:
        raise web.HTTPBadRequest(text="Нужен @username или id.")
    added = await db.link_wl_add(cid, target_id, uname, title)
    who = title or (f"@{uname}" if uname else str(target_id))
    if not added:
        return js({"note": f"{who} уже в списке разрешённых."})
    return js({"note": f"{who} разрешён." if target_id
               else f"{who} разрешён (id не определился — сверяю по нику)."})


@routes.delete("/api/chat/{cid}/linkwl/{rid}")
async def api_linkwl_del(request: web.Request) -> web.Response:
    await cid_of(request)
    await db.link_wl_remove(int(request.match_info["rid"]))
    return js({"ok": True})


# ---------- разрешённые инлайн-боты ----------

@routes.post("/api/chat/{cid}/inlinewl")
async def api_inlinewl_add(request: web.Request) -> web.Response:
    cid = await cid_of(request)
    token = ((await body(request)).get("target") or "").strip()
    if not token.startswith("@") or len(token) < 4:
        raise web.HTTPBadRequest(text="Нужен @username бота.")
    uname = token.lstrip("@")
    bot_id, _ = await resolve.by_username(bot_of(request), token)
    if await db.inline_wl_allowed(cid, uname, bot_id):
        raise web.HTTPBadRequest(text="Этот бот уже в списке.")
    added = await db.inline_wl_add(cid, uname, bot_id)
    return js({"note": f"@{uname} разрешён." if added
               else f"@{uname} уже в списке."})


@routes.delete("/api/chat/{cid}/inlinewl/{rid}")
async def api_inlinewl_del(request: web.Request) -> web.Response:
    await cid_of(request)
    await db.inline_wl_remove(int(request.match_info["rid"]))
    return js({"ok": True})


# ---------- триггеры ----------

@routes.get("/api/chat/{cid}/trigs")
async def api_trigs(request: web.Request) -> web.Response:
    cid = await cid_of(request)
    items = rows(await db.trig_list(cid))
    stats = await db.ans_stats("trig", [r["id"] for r in items])
    for r in items:
        total, media = stats.get(r["id"], (0, 0))
        r["answers"] = total
        r["media"] = media
    return js({"items": items, "limit": config.TRIG_LIMIT})


@routes.post("/api/chat/{cid}/trigs")
async def api_trig_add(request: web.Request) -> web.Response:
    """Новый триггер: фраза сейчас, ответы — отдельными вариантами."""
    cid = await cid_of(request)
    data = await body(request)
    phrase = (data.get("phrase") or "").strip().lower()
    text = (data.get("text") or "").strip()
    if len(phrase) < 3:
        raise web.HTTPBadRequest(text="Фраза от 3 символов.")
    if len(await db.trig_list(cid)) >= config.TRIG_LIMIT:
        raise web.HTTPBadRequest(text=f"Лимит {config.TRIG_LIMIT} триггеров.")
    rid = await db.trig_add(cid, phrase, None)
    if text:
        await db.ans_add("trig", rid, text)
    note = "Триггер добавлен."
    if not (await db.get_settings(cid)).trig_on:
        # добавили триггер — значит хотят, чтобы он работал
        await db.set_setting(cid, "trig_on", 1)
        note = "Триггер добавлен, раздел включён."
    return js({"id": rid, "note": note})


@routes.get("/api/chat/{cid}/trigs/{rid}")
async def api_trig(request: web.Request) -> web.Response:
    await cid_of(request)
    rid = int(request.match_info["rid"])
    r = await db.trig_get(rid)
    if r is None:
        raise web.HTTPNotFound(text="no trigger")
    return js({"trigger": dict(r), "answers": rows(await db.ans_list("trig", rid)),
               "cooldowns": list(config.CMD_COOLDOWN_PRESETS)})


@routes.post("/api/chat/{cid}/trigs/{rid}")
async def api_trig_edit(request: web.Request) -> web.Response:
    await cid_of(request)
    rid = int(request.match_info["rid"])
    data = await body(request)
    if "phrase" in data:
        phrase = (data["phrase"] or "").strip().lower()
        if len(phrase) < 3:
            raise web.HTTPBadRequest(text="Фраза от 3 символов.")
        await db.trig_set(rid, "phrase", phrase)
    if "cooldown" in data:
        cd = int(data["cooldown"])
        if cd not in config.CMD_COOLDOWN_PRESETS:
            raise web.HTTPBadRequest(text="bad cooldown")
        await db.trig_set(rid, "cooldown", cd)
    return js({"ok": True})


@routes.delete("/api/chat/{cid}/trigs/{rid}")
async def api_trig_del(request: web.Request) -> web.Response:
    await cid_of(request)
    rid = int(request.match_info["rid"])
    await db.ans_clear("trig", rid)      # вместе с вариантами и их медиа
    await db.trig_remove(rid)
    return js({"ok": True})


# ---------- счётчики ----------

@routes.get("/api/chat/{cid}/cmds")
async def api_cmds(request: web.Request) -> web.Response:
    cid = await cid_of(request)
    return js({"items": rows(await db.cmd_list(cid)), "limit": config.CMD_LIMIT})


@routes.post("/api/chat/{cid}/cmds")
async def api_cmd_add(request: web.Request) -> web.Response:
    cid = await cid_of(request)
    data = await body(request)
    cmd = (data.get("cmd") or "").strip().lower()
    text = (data.get("text") or "").strip()
    if not cmd.startswith("!"):
        cmd = "!" + cmd
    if len(cmd) < 2 or " " in cmd:
        raise web.HTTPBadRequest(text="Команда должна быть одним словом.")
    if cmd.lstrip("!") in um._RESERVED_CMDS:
        raise web.HTTPBadRequest(text="Это системная команда бота.")
    if await db.cmd_find(cid, cmd):
        raise web.HTTPBadRequest(text="Такой счётчик уже есть.")
    if not text:
        raise web.HTTPBadRequest(text="Нужна заготовка ответа.")
    if len(await db.cmd_list(cid)) >= config.CMD_LIMIT:
        raise web.HTTPBadRequest(text=f"Лимит {config.CMD_LIMIT} счётчиков.")
    await db.cmd_add(cid, cmd, text, 30)
    row = await db.cmd_find(cid, cmd)
    await db.ans_add("cmd", row["id"], text)
    note = "Счётчик создан."
    if not (await db.get_settings(cid)).cmds_on:
        await db.set_setting(cid, "cmds_on", 1)
        note = "Счётчик создан, раздел включён."
    return js({"id": row["id"], "note": note})


@routes.get("/api/chat/{cid}/cmds/{rid}")
async def api_cmd(request: web.Request) -> web.Response:
    await cid_of(request)
    rid = int(request.match_info["rid"])
    r = await db.cmd_get(rid)
    if r is None:
        raise web.HTTPNotFound(text="no counter")
    return js({"cmd": dict(r), "answers": rows(await db.ans_list("cmd", rid)),
               "cooldowns": list(config.CMD_COOLDOWN_PRESETS)})


@routes.post("/api/chat/{cid}/cmds/{rid}")
async def api_cmd_edit(request: web.Request) -> web.Response:
    await cid_of(request)
    rid = int(request.match_info["rid"])
    data = await body(request)
    if "cooldown" in data:
        cd = int(data["cooldown"])
        if cd not in config.CMD_COOLDOWN_PRESETS:
            raise web.HTTPBadRequest(text="bad cooldown")
        await db.cmd_set(rid, "cooldown", cd)
    if data.get("reset"):
        await db.cmd_set(rid, "count", 0)
    return js({"ok": True})


@routes.delete("/api/chat/{cid}/cmds/{rid}")
async def api_cmd_del(request: web.Request) -> web.Response:
    await cid_of(request)
    rid = int(request.match_info["rid"])
    await db.ans_clear("cmd", rid)
    await db.cmd_remove(rid)
    return js({"ok": True})


# ---------- варианты ответов (триггеры, счётчики, приветствие, правила) ----------

_ANS_OWNERS = {"trig", "cmd", "welcome", "rules"}


async def _ans_scope(request) -> tuple[int, str, int]:
    """Чат, владелец варианта и его id — с проверкой, что владелец из этого чата."""
    cid = await cid_of(request)
    owner = request.query.get("owner") or (await body(request)).get("owner")
    oid = request.query.get("oid") or (await body(request)).get("oid")
    if owner not in _ANS_OWNERS:
        raise web.HTTPBadRequest(text="bad owner")
    oid = int(oid)
    # у приветствия и правил владелец — сам чат; у триггера и счётчика
    # проверяем, что запись принадлежит именно этому чату
    if owner in ("welcome", "rules"):
        if oid != cid:
            raise web.HTTPForbidden(text="not your object")
    else:
        row = await (db.trig_get(oid) if owner == "trig" else db.cmd_get(oid))
        if row is None or row["chat_id"] != cid:
            raise web.HTTPForbidden(text="not your object")
    return cid, owner, oid


@routes.get("/api/chat/{cid}/answers")
async def api_answers(request: web.Request) -> web.Response:
    _, owner, oid = await _ans_scope(request)
    items = []
    for a in await db.ans_list(owner, oid):
        items.append({"id": a["id"], "text": a["text"],
                      "plain": _strip_tags(a["text"]),
                      "media_type": a["media_type"],
                      "has_media": bool(a["file_path"])})
    return js({"items": items, "limit": um.ANS_LIMIT, "owner": owner, "oid": oid})


@routes.post("/api/chat/{cid}/answers")
async def api_answer_add(request: web.Request) -> web.Response:
    _, owner, oid = await _ans_scope(request)
    text = ((await body(request)).get("text") or "").strip()
    if not text:
        raise web.HTTPBadRequest(text="Нужен текст варианта.")
    if len(await db.ans_list(owner, oid)) >= um.ANS_LIMIT:
        raise web.HTTPBadRequest(text=f"Лимит {um.ANS_LIMIT} вариантов.")
    rid = await db.ans_add(owner, oid, text)
    return js({"id": rid})


@routes.post("/api/chat/{cid}/answers/upload")
async def api_answer_upload(request: web.Request) -> web.Response:
    """Медиа-вариант: файл сохраняем рядом с медиа триггеров, как это делает бот."""
    cid = await cid_of(request)
    reader = await request.multipart()
    owner = oid = None
    caption = ""
    saved = kind = None
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "owner":
            owner = (await part.text()).strip()
        elif part.name == "oid":
            oid = int((await part.text()).strip())
        elif part.name == "caption":
            caption = (await part.text()).strip()
        elif part.name == "file":
            if owner not in _ANS_OWNERS or oid is None:
                raise web.HTTPBadRequest(text="сначала owner и oid")
            kind = _media_kind(part.filename or "")
            saved = await _save_upload(part, cid, kind)
    if saved is None:
        raise web.HTTPBadRequest(text="Файл не пришёл.")
    if owner in ("welcome", "rules"):
        if oid != cid:
            raise web.HTTPForbidden(text="not your object")
    else:
        row = await (db.trig_get(oid) if owner == "trig" else db.cmd_get(oid))
        if row is None or row["chat_id"] != cid:
            raise web.HTTPForbidden(text="not your object")
    rid = await db.ans_add(owner, oid, caption or None, saved, kind)
    return js({"id": rid, "media_type": kind})


_MEDIA_EXT = {
    "photo": (".jpg", ".jpeg", ".png", ".webp"),
    "animation": (".gif", ".mp4"),
    "video": (".mov", ".mkv", ".webm"),
    "voice": (".ogg", ".oga"),
    "audio": (".mp3", ".m4a", ".flac"),
}


def _media_kind(filename: str) -> str:
    ext = os.path.splitext(filename.lower())[1]
    for kind, exts in _MEDIA_EXT.items():
        if ext in exts:
            return kind
    return "document"


async def _save_upload(part, cid: int, kind: str) -> str:
    """Слить файл на диск с потолком по размеру. Возвращает путь."""
    os.makedirs(config.TRIG_DIR, exist_ok=True)
    ext = os.path.splitext(part.filename or "")[1][:8] or ".bin"
    path = os.path.join(config.TRIG_DIR, f"{cid}_{int(time.time() * 1000)}{ext}")
    size = 0
    with open(path, "wb") as f:
        while True:
            chunk = await part.read_chunk()
            if not chunk:
                break
            size += len(chunk)
            if size > config.WEB_UPLOAD_MAX:
                f.close()
                os.remove(path)
                raise web.HTTPRequestEntityTooLarge(
                    max_size=config.WEB_UPLOAD_MAX, actual_size=size)
            f.write(chunk)
    return path


@routes.delete("/api/chat/{cid}/answers/{rid}")
async def api_answer_del(request: web.Request) -> web.Response:
    cid = await cid_of(request)
    a = await db.ans_get(int(request.match_info["rid"]))
    if a is None:
        return js({"ok": True})
    # чужой вариант удалить нельзя: сверяем владельца с этим чатом
    if a["owner"] in ("welcome", "rules"):
        if a["owner_id"] != cid:
            raise web.HTTPForbidden(text="not your object")
    else:
        row = await (db.trig_get(a["owner_id"]) if a["owner"] == "trig"
                     else db.cmd_get(a["owner_id"]))
        if row is None or row["chat_id"] != cid:
            raise web.HTTPForbidden(text="not your object")
    await db.ans_remove(a["id"])
    return js({"ok": True})


@routes.post("/api/chat/{cid}/welcome/migrate")
async def api_welcome_migrate(request: web.Request) -> web.Response:
    """Старое приветствие одним текстом переносим в список заготовок."""
    cid = await cid_of(request)
    s = await db.get_settings(cid)
    if s.welcome_text:
        await db.ans_add("welcome", cid, s.welcome_text)
        await db.set_setting(cid, "welcome_text", None)
    return js({"ok": True})


# ---------- варны ----------

@routes.get("/api/chat/{cid}/warned")
async def api_warned(request: web.Request) -> web.Response:
    cid = await cid_of(request)
    s = await db.get_settings(cid)
    items = []
    for r in await db.warn_users(cid):
        items.append({"user_id": r["user_id"], "count": r["cnt"],
                      "who": f"@{r['username']}" if r["username"]
                      else (r["name"] or str(r["user_id"])),
                      "when": utils.rel_time(r["last_ts"])})
    return js({"items": items, "limit": s.warns_limit})


@routes.post("/api/chat/{cid}/warned/{uid}/reset")
async def api_warn_reset(request: web.Request) -> web.Response:
    cid = await cid_of(request)
    await db.warn_reset(cid, int(request.match_info["uid"]))
    return js({"ok": True})


# ---------- наказания ----------

@routes.get("/api/chat/{cid}/active")
async def api_active(request: web.Request) -> web.Response:
    cid = await cid_of(request)
    items = []
    for r in await db.active_punishments(cid, limit=ACTIVE_LIMIT):
        items.append({
            "id": r["id"], "user_id": r["user_id"],
            "who": r["name"] or await db.user_label(r["user_id"], r["username"]),
            "kind": r["kind"], "kind_label": um._KIND_WORD.get(r["kind"], r["kind"]),
            "until": "навсегда" if not r["until_ts"] else utils.fmt_ts(r["until_ts"]),
            "reason": _strip_tags(r["reason"]) or "—",
        })
    return js({"items": items})


@routes.post("/api/chat/{cid}/active/{pid}/lift")
async def api_lift(request: web.Request) -> web.Response:
    cid = await cid_of(request)
    pid = int(request.match_info["pid"])
    p = await db.get_punishment(pid)
    if p is None or p["chat_id"] != cid:
        raise web.HTTPForbidden(text="not your punishment")
    ok, msg, _ = await moderation.lift_punishment(bot_of(request), pid, invite=False)
    if ok:
        asyncio.create_task(net_svc.lift(bot_of(request), p["chat_id"], p["user_id"]))
    return js({"ok": ok, "note": _strip_tags(msg)})


_MASS = {"ban": um._ban_one, "unban": um._unban_one, "kick": um._kick_one}


@routes.post("/api/chat/{cid}/mass")
async def api_mass(request: web.Request) -> web.Response:
    """Массовые бан/разбан/кик списком id и @username — как в меню."""
    cid = await cid_of(request)
    data = await body(request)
    worker = _MASS.get(data.get("kind"))
    if worker is None:
        raise web.HTTPBadRequest(text="bad kind")
    tokens = [t for t in re.split(r"[\s,;]+", data.get("text") or "") if t]
    if not tokens:
        raise web.HTTPBadRequest(text="Не нашёл ни одного id.")
    cut = len(tokens) > um.MASS_LIMIT
    tokens = tokens[:um.MASS_LIMIT]
    out = {"done": [], "skip": [], "fail": []}
    for i, token in enumerate(tokens, 1):
        result, line = await worker(bot_of(request), cid, token, uid_of(request))
        out[result].append(_strip_tags(line))
        if i < len(tokens):
            await asyncio.sleep(um.MASS_DELAY)   # лимиты Telegram важнее скорости
    out["cut"] = cut
    return js(out)


# ---------- лог-чат, перенос настроек, выход ----------

@routes.post("/api/chat/{cid}/log")
async def api_log(request: web.Request) -> web.Response:
    """Назначить лог-чат. Пусто — убрать."""
    cid = await cid_of(request)
    raw = (await body(request)).get("chat_id")
    if raw in (None, "", "-"):
        await db.set_setting(cid, "log_chat_id", None)
        await db.add_event(cid, "bot", "лог-чат убран")
        return js({"chat_id": None, "title": None})
    try:
        target = int(str(raw).strip())
    except ValueError:
        raise web.HTTPBadRequest(text="Нужен числовой id чата.")
    # чужой рабочий чат логом быть не может: туда полетели бы чужие сообщения
    if not await db.owns_chat(uid_of(request), target) and await db.get_chat(target):
        raise web.HTTPForbidden(text="Этот чат принадлежит другому владельцу.")
    try:
        await bot_of(request).get_chat(target)
    except Exception as e:
        raise web.HTTPBadRequest(text=f"Бот не видит этот чат: {e}")
    await db.set_setting(cid, "log_chat_id", target)
    await db.add_event(cid, "bot", f"лог-чат установлен: {target}")
    return js({"chat_id": target, "title": await _log_label(target)})


@routes.get("/api/chat/{cid}/copy")
async def api_copy_sources(request: web.Request) -> web.Response:
    cid = await cid_of(request)
    others = [c for c in await db.chats_for(uid_of(request)) if c["chat_id"] != cid]
    return js({
        "chats": [{"chat_id": c["chat_id"], "title": c["title"] or str(c["chat_id"])}
                  for c in others],
        "groups": [{"key": k, "label": transfer.GROUPS[k][0]} for k in transfer.ALL_GROUPS],
    })


@routes.post("/api/chat/{cid}/copy")
async def api_copy(request: web.Request) -> web.Response:
    cid = await cid_of(request)
    data = await body(request)
    src = int(data.get("src") or 0)
    groups = [g for g in (data.get("groups") or []) if g in transfer.GROUPS]
    if not await auth.owns(uid_of(request), src):
        raise web.HTTPForbidden(text="Чужой чат-источник.")
    if not groups:
        raise web.HTTPBadRequest(text="Не выбрано ни одного раздела.")
    stats = await transfer.copy_chat(src, cid, set(groups))
    await db.kv_set(um.setup_key(cid), "1")
    flt.invalidate_words(cid)
    return js({"copied": stats})


@routes.post("/api/chat/{cid}/setup-skip")
async def api_setup_skip(request: web.Request) -> web.Response:
    cid = await cid_of(request)
    await db.kv_set(um.setup_key(cid), "1")
    return js({"ok": True})


@routes.post("/api/chat/{cid}/leave")
async def api_leave(request: web.Request) -> web.Response:
    cid = await cid_of(request)
    ok, note = await moderation.leave_chat(bot_of(request), cid)
    if not ok:
        raise web.HTTPBadRequest(text=f"Не вышло: {note}")
    return js({"ok": True, "note": note.strip()})


# ---------- недельная сводка ----------

@routes.post("/api/chat/{cid}/digest")
async def api_digest(request: web.Request) -> web.Response:
    cid = await cid_of(request)
    data = await body(request)
    if data.get("off"):
        await db.set_setting(cid, "digest_to", 0)
        return js({"to": 0})
    if data.get("now"):
        s = await db.get_settings(cid)
        if not s.digest_to:
            raise web.HTTPBadRequest(text="Сначала укажите получателя.")
        if digest_svc.tracked_chat() != cid:
            raise web.HTTPBadRequest(text="Сводка ведётся только для профильного чата.")
        ok = await digest_svc.send_digest(bot_of(request), cid, s.digest_to)
        if not ok:
            raise web.HTTPBadRequest(text="Не вышло: нет базы статистики или юзер недоступен.")
        return js({"note": "Сводка отправлена."})
    try:
        to = int(str(data.get("to") or "").strip())
    except ValueError:
        raise web.HTTPBadRequest(text="Нужен числовой id получателя.")
    await db.set_setting(cid, "digest_to", to)
    return js({"to": to, "who": await db.user_label(to)})


# ---------- игры ----------

@routes.get("/api/chat/{cid}/games")
async def api_games(request: web.Request) -> web.Response:
    cid = await cid_of(request)
    s = await db.get_settings(cid)
    items = []
    for bit, label, how, about in config.GAME_BITS:
        by_hand = bit != config.GAME_TITLES
        item = {"bit": bit, "label": label, "how": how, "about": about,
                "on": bool(s.games_on & bit), "admins": bool(s.games_adm & bit),
                "by_hand": by_hand}
        if by_hand:
            kind_field, min_field = config.GAME_FIELDS[bit]
            kind, minutes = getattr(s, kind_field), getattr(s, min_field)
            item["kind"] = kind
            item["minutes"] = minutes
            item["prize"] = "бан" if kind == "ban" else f"мут на {utils.fmt_minutes(minutes)}"
        items.append(item)
    return js({"items": items,
               "mutes": [{"value": m, "label": utils.fmt_minutes(m)}
                         for m in config.MUTE_PRESETS]})


@routes.post("/api/chat/{cid}/games/prize")
async def api_game_prize(request: web.Request) -> web.Response:
    cid = await cid_of(request)
    data = await body(request)
    bit = int(data.get("bit") or 0)
    if bit not in config.GAME_FIELDS:
        raise web.HTTPBadRequest(text="bad game")
    kind_field, min_field = config.GAME_FIELDS[bit]
    if "kind" in data:
        if data["kind"] not in config.GAME_PUNISH_VALUES:
            raise web.HTTPBadRequest(text="bad punish")
        await db.set_setting(cid, kind_field, data["kind"])
    if "minutes" in data:
        minutes = int(data["minutes"])
        if minutes not in config.MUTE_PRESETS:
            raise web.HTTPBadRequest(text="bad minutes")
        await db.set_setting(cid, min_field, minutes)
    return js({"ok": True})


# ---------- сетки чатов ----------

async def _nets_for(uid: int) -> list:
    return await (db.nets_all() if uid in config.ADMIN_IDS else db.nets_of(uid))


async def _net_or_403(request, nid: int):
    net = await db.net_get(nid)
    if net is None:
        raise web.HTTPNotFound(text="no net")
    if net["owner_id"] != uid_of(request) and not is_owner(request):
        raise web.HTTPForbidden(text="not your net")
    return net


@routes.get("/api/nets")
async def api_nets(request: web.Request) -> web.Response:
    uid = uid_of(request)
    nets = await _nets_for(uid)
    out = []
    for n in nets:
        chats = await db.net_chats(n["id"])
        out.append({"id": n["id"], "title": n["title"], "chats": len(chats),
                    "owner_id": n["owner_id"],
                    # имя владельца нужно и для своих сеток: по нему панель
                    # группирует список у владельца бота
                    "owner": await db.user_label(n["owner_id"])})
    mine = sum(1 for n in nets if n["owner_id"] == uid)
    return js({"items": out, "limit": config.NET_LIMIT, "can_create": mine < config.NET_LIMIT})


@routes.post("/api/nets")
async def api_net_create(request: web.Request) -> web.Response:
    title = ((await body(request)).get("title") or "").strip()[:40]
    if len(title) < 2:
        raise web.HTTPBadRequest(text="Название от 2 символов.")
    nid = await db.net_create(uid_of(request), title)
    if nid is None:
        raise web.HTTPBadRequest(text=f"Лимит {config.NET_LIMIT} сеток.")
    return js({"id": nid})


@routes.get("/api/net/{nid}")
async def api_net(request: web.Request) -> web.Response:
    nid = int(request.match_info["nid"])
    net = await _net_or_403(request, nid)
    chats = await db.net_chats(nid)
    free = [c for c in await db.chats_for(uid_of(request))
            if c["owner_id"] == net["owner_id"] and c["net_id"] != nid]
    free_out = []
    for c in free:
        busy = await db.net_get(c["net_id"]) if c["net_id"] else None
        free_out.append({"chat_id": c["chat_id"], "title": c["title"] or str(c["chat_id"]),
                         "busy": busy["title"] if busy else None})
    return js({
        "id": nid, "title": net["title"], "owner_id": net["owner_id"],
        "chats": [{"chat_id": c["chat_id"], "title": c["title"] or str(c["chat_id"])}
                  for c in chats],
        "free": free_out,
        "bits": [{"bit": bit, "label": lbl, "on": bool(net["sync_mask"] & bit)}
                 for bit, lbl in config.NET_BITS],
        "lift_mode": net["lift_mode"],
        "lift_label": um._LIFT_LABEL[net["lift_mode"]],
    })


@routes.post("/api/net/{nid}")
async def api_net_edit(request: web.Request) -> web.Response:
    nid = int(request.match_info["nid"])
    net = await _net_or_403(request, nid)
    data = await body(request)
    if "title" in data:
        title = (data["title"] or "").strip()[:40]
        if len(title) < 2:
            raise web.HTTPBadRequest(text="Название от 2 символов.")
        await db.net_set(nid, "title", title)
    if "bit" in data:
        await db.net_set(nid, "sync_mask", net["sync_mask"] ^ int(data["bit"]))
    if "lift_mode" in data:
        mode = data["lift_mode"]
        if mode not in um._LIFT_LABEL:
            raise web.HTTPBadRequest(text="bad lift mode")
        await db.net_set(nid, "lift_mode", mode)
    return js({"ok": True})


@routes.delete("/api/net/{nid}")
async def api_net_delete(request: web.Request) -> web.Response:
    nid = int(request.match_info["nid"])
    await _net_or_403(request, nid)
    await db.net_delete(nid)
    return js({"ok": True})


@routes.post("/api/net/{nid}/chats")
async def api_net_add_chat(request: web.Request) -> web.Response:
    nid = int(request.match_info["nid"])
    net = await _net_or_403(request, nid)
    cid = int((await body(request)).get("chat_id") or 0)
    ch = await db.get_chat(cid)
    if ch is None or ch["owner_id"] != net["owner_id"]:
        raise web.HTTPForbidden(text="Чат не принадлежит владельцу сетки.")
    await db.net_assign(cid, nid)
    return js({"ok": True})


@routes.delete("/api/net/{nid}/chats/{cid}")
async def api_net_del_chat(request: web.Request) -> web.Response:
    nid = int(request.match_info["nid"])
    await _net_or_403(request, nid)
    cid = int(request.match_info["cid"])
    ch = await db.get_chat(cid)
    if ch is None or ch["net_id"] != nid:
        raise web.HTTPNotFound(text="Чата нет в этой сетке.")
    await db.net_assign(cid, None)
    return js({"ok": True})


@routes.post("/api/net/{nid}/import")
async def api_net_import(request: web.Request) -> web.Response:
    """Разослать активные баны сетки по всем её чатам — разовая операция."""
    nid = int(request.match_info["nid"])
    await _net_or_403(request, nid)
    done, failed = await um.net_import_run(bot_of(request), nid, uid_of(request))
    return js({"done": done, "failed": failed})


@routes.post("/api/chat/{cid}/net")
async def api_chat_net(request: web.Request) -> web.Response:
    """Положить чат в сетку или вынуть из неё — с карточки чата."""
    cid = await cid_of(request)
    raw = (await body(request)).get("net_id")
    if raw in (None, "", 0, "0"):
        await db.net_assign(cid, None)
        return js({"net": None})
    net = await _net_or_403(request, int(raw))
    ch = await db.get_chat(cid)
    if ch is None or ch["owner_id"] != net["owner_id"]:
        raise web.HTTPForbidden(text="Чат не принадлежит владельцу сетки.")
    await db.net_assign(cid, net["id"])
    return js({"net": {"id": net["id"], "title": net["title"]}})


# ---------- владелец бота: доступ, глобальный лог, служебное ----------

@routes.get("/api/access")
async def api_access(request: web.Request) -> web.Response:
    owner_only(request)
    items = []
    for r in await db.access_list():
        stored = r["name"] if "name" in r.keys() else None
        items.append({"id": r["id"], "user_id": r["user_id"], "username": r["username"],
                      "who": await db.user_label(r["user_id"], r["username"],
                                                 fallback=stored)})
    return js({"items": items})


@routes.post("/api/access")
async def api_access_add(request: web.Request) -> web.Response:
    owner_only(request)
    token = ((await body(request)).get("target") or "").strip()
    if token.lstrip("-").isdigit():
        uid = int(token)
        row = await db.get_user(uid)
        await db.access_add(uid, None, row["first_name"] if row else None)
    elif token.startswith("@") and len(token) > 3:
        uid, name = await resolve.by_username(bot_of(request), token)
        await db.access_add(uid, token, name)
    else:
        raise web.HTTPBadRequest(text="Нужен id или @username.")
    return js({"ok": True})


@routes.delete("/api/access/{rid}")
async def api_access_del(request: web.Request) -> web.Response:
    owner_only(request)
    await db.access_remove(int(request.match_info["rid"]))
    return js({"ok": True})


@routes.post("/api/global-log")
async def api_global_log(request: web.Request) -> web.Response:
    owner_only(request)
    raw = (await body(request)).get("chat_id")
    if raw in (None, "", "-"):
        await db.set_global_log(None)
        await db.add_event(None, "bot", "глобальный лог убран")
        return js({"chat_id": None, "title": None})
    try:
        target = int(str(raw).strip())
    except ValueError:
        raise web.HTTPBadRequest(text="Нужен числовой id чата.")
    try:
        await bot_of(request).get_chat(target)
    except Exception as e:
        raise web.HTTPBadRequest(text=f"Бот не видит этот чат: {e}")
    await db.set_global_log(target)
    await db.add_event(None, "bot", f"глобальный лог: {target}")
    return js({"chat_id": target, "title": await _log_label(target)})


@routes.get("/api/admin/log")
async def api_admin_log(request: web.Request) -> web.Response:
    owner_only(request)
    titles = {c["chat_id"]: c["title"] for c in await db.all_chats()}
    out = []
    for r in await db.recent_events(EVENTS_LIMIT):
        out.append({"kind": r["kind"], "when": utils.rel_time(r["ts"]),
                    "chat": titles.get(r["chat_id"]),
                    "text": _strip_tags(await db.names_in(r["text"]))})
    return js({"items": out})


@routes.get("/api/admin/errors")
async def api_admin_errors(request: web.Request) -> web.Response:
    owner_only(request)
    from ..services import errorlog
    return js({"items": errorlog.recent(30)})


@routes.get("/api/admin/health")
async def api_admin_health(request: web.Request) -> web.Response:
    owner_only(request)
    from ..services import health
    return js({"text": _strip_tags(await health.report())})


# ---------- бан-рулетка владельца ----------

@routes.get("/api/fun/roulette")
async def api_roulette(request: web.Request) -> web.Response:
    owner_only(request)
    uid = uid_of(request)
    cfg = await fun_h._cfg(uid)
    ch = await db.get_chat(cfg["chat_id"]) if cfg["chat_id"] else None
    return js({
        "cfg": cfg,
        "chat_title": (ch["title"] if ch else None),
        "chats": [{"chat_id": c["chat_id"], "title": c["title"] or str(c["chat_id"])}
                  for c in await db.chats_for(uid)],
        "mutes": [{"value": m, "label": utils.fmt_minutes(m)} for m in config.MUTE_PRESETS],
        "timers": list(fun_h.TIMER_PRESETS),
    })


@routes.post("/api/fun/roulette")
async def api_roulette_set(request: web.Request) -> web.Response:
    owner_only(request)
    uid = uid_of(request)
    data = await body(request)
    cfg = await fun_h._cfg(uid)
    if "chat_id" in data:
        cid = int(data["chat_id"])
        if not await auth.owns(uid, cid):
            raise web.HTTPForbidden(text="not your chat")
        cfg["chat_id"] = cid
    if data.get("kind") in fun_h.KIND_LABEL:
        cfg["kind"] = data["kind"]
    if "minutes" in data and int(data["minutes"]) in config.MUTE_PRESETS:
        cfg["minutes"] = int(data["minutes"])
    if data.get("mode") in fun_h.MODE_LABEL:
        cfg["mode"] = data["mode"]
    if "timer" in data and int(data["timer"]) in fun_h.TIMER_PRESETS:
        cfg["timer"] = int(data["timer"])
    await fun_h._save(uid, cfg)
    return js({"cfg": cfg})


@routes.post("/api/fun/roulette/spin")
async def api_roulette_spin(request: web.Request) -> web.Response:
    owner_only(request)
    uid = uid_of(request)
    cfg = await fun_h._cfg(uid)
    if not cfg["chat_id"]:
        raise web.HTTPBadRequest(text="Сначала выберите чат.")
    bot = bot_of(request)
    try:
        note = (await fun_h._run_opt(bot, cfg, uid) if cfg["mode"] == "opt"
                else await fun_h._run_all(bot, cfg, uid))
    except Exception as e:
        logger.warning("рулетка из панели упала", exc_info=True)
        raise web.HTTPBadRequest(text=f"Не вышло: {e}")
    return js({"note": note})


# ---------- стартовый набор нейрофильтра ----------
#
# Набор общий на весь бот, поэтому только владельцу: удалённый пример пропадает
# сразу у всех чатов.

SEED_PAGE = 20


@routes.get("/api/seed")
async def api_seed(request: web.Request) -> web.Response:
    owner_only(request)
    label = request.query.get("label") or None
    if label not in ("spam", "ok", None):
        label = None
    q = (request.query.get("q") or "").strip() or None
    page = max(0, int(request.query.get("page") or 0))
    total = await db.seed_count(label, q)
    rows = await db.seed_page(label, q, page * SEED_PAGE, SEED_PAGE)
    return js({
        "stats": await db.seed_stats(),
        "vecs": await db.seed_vec_count(),
        "in_work": config.NN_SEED_LIMIT,
        "until": config.NN_SEED_UNTIL,
        "total": total,
        "page": page,
        "pages": max(1, -(-total // SEED_PAGE)),
        "items": [{"id": r["id"], "label": r["label"], "text": r["text"]}
                  for r in rows],
    })


@routes.post("/api/seed/delete")
async def api_seed_delete(request: web.Request) -> web.Response:
    owner_only(request)
    p = await body(request)
    if p.get("all"):
        gone = await db.seed_clear()
        what = "набор очищен"
    elif p.get("ids"):
        gone = await db.seed_delete([int(x) for x in p["ids"]])
        what = "удалены примеры"
    else:
        label = p.get("label") if p.get("label") in ("spam", "ok") else None
        q = (p.get("q") or "").strip() or None
        if not label and not q:
            raise web.HTTPBadRequest(text="Нечего удалять: задайте поиск или метку.")
        gone = await db.seed_delete_where(label, q)
        what = f"удалено по фильтру ({q or label})"
    if gone:
        nn.invalidate()          # набор подмешан всем молодым чатам
        await db.add_event(None, "nn", f"стартовый набор: {what}, {gone} шт "
                                       f"by {uid_of(request)}")
    return js({"ok": True, "gone": gone})
