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

from .. import config, db, runtime, schema, utils
from ..handlers import fun as fun_h, user_menu as um
from ..services import (adm_cache, cas, digest as digest_svc, filters as flt,
                        media, moderation, net as net_svc, nn, resolve,
                        status as status_svc, transfer, triggers)
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


def _cd_labels() -> dict:
    """Подписи кулдаунов: «40 минут» вместо «2400 сек»."""
    return {c: utils.fmt_seconds(c) for c in config.CMD_COOLDOWN_PRESETS if c}


def rows(seq) -> list[dict]:
    return [dict(r) for r in seq]


def bot_of(request) -> "object":
    return request.app["bot"]


def uid_of(request) -> int:
    return request["user"]["id"]


def is_owner(request) -> bool:
    return uid_of(request) in config.ADMIN_IDS


async def cid_of(request, need: str = "settings") -> int:
    """id чата из пути + проверка прав. Не хватает уровня — 403 и никаких данных.

    Уровни: punish (наказания, статус, массовые действия) < settings (разделы
    модерации) < owner (лог-чат, сетки, перенос, удаление бота, список админов).
    """
    try:
        cid = int(request.match_info["cid"])
    except (KeyError, ValueError):
        raise web.HTTPBadRequest(text="bad chat id")
    if not await auth.owns(uid_of(request), cid, need):
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


def _short_reason(reason: str | None) -> str:
    """Причина для списка: без «сетка · чат:» и приписки про подмену мута."""
    why, _swapped = utils.short_reason(_strip_tags(reason))
    return why


# ---------- главная ----------

async def _chat_brief(bot, row) -> dict:
    """Строка чата для списка: название плюс канал, к которому он прицеплен.

    Канал берём из базы. Раньше его спрашивали у Telegram на каждый чат, и
    открытие панели росло вместе с их числом — до двух запросов на строку.
    Обновляется он при регистрации чата и сверкой на старте.
    """
    cid = row["chat_id"]
    linked = row["linked_title"] if "linked_title" in row.keys() else None
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
    cid = await cid_of(request, "punish")
    uid = uid_of(request)
    # админу с уровнем «наказания» разделы настроек не показываем совсем
    level = await db.chat_access(uid, cid)
    full = level in ("owner", "settings")
    ch = await db.get_chat(cid)
    s = await db.get_settings(cid)
    st = await db.chat_stats(cid)
    net = await db.net_of_chat(cid)

    # разделы верхнего уровня; подстраницы (у них back) открываются изнутри
    sections = []
    hidden = schema.hidden_sections()
    for sec in schema.SECTIONS:
        if sec.back or sec.key in hidden:
            continue
        if sec.key == "digest" and digest_svc.tracked_chat() != cid:
            continue
        sections.append({"key": sec.key, "title": sec.title,
                         "group": schema.group_of(sec.key),
                         "on": bool(getattr(s, sec.fields[0].key))
                         if sec.fields and sec.fields[0].kind == "toggle" else None})

    if not full:
        sections = []

    return js({
        "level": level,
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
        "groups": ([{"key": k, "title": t, "hint": h}
                    for k, t, h in schema.SECTION_GROUPS] if full else []),
        "needs_setup": await um.needs_setup(cid, uid),
        "bot": (None if ch and ch["kind"] == "channel"
                else await adm_cache.bot_status(bot_of(request), cid)),
        "games_on": bool(s.games_on),
    })


@routes.get("/api/chat/{cid}/stats")
async def api_stats(request: web.Request) -> web.Response:
    """Статистика чата с расшифровкой топа — тот же экран, что в меню."""
    cid = await cid_of(request, "punish")
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
    # дату собираем здесь: у браузера свой часовой пояс, и «считаем с» съезжало
    st["since_date"] = (utils._local(st["since"]).strftime("%d.%m.%Y")
                        if st["since"] else None)
    return js(st)


# ---------- графики ----------
#
# Считаем на сервере, рисуем в браузере: библиотек графиков не берём, тянуть
# полмегабайта ради шести картинок в мини-аппе незачем.

CHART_RANGES = (7, 30, 90)
_DOW = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
_RULE_LABELS = dict(config.WL_SCOPE_LABELS, manual="вручную", other="прочее")


@routes.get("/api/chat/{cid}/charts")
async def api_charts(request: web.Request) -> web.Response:
    cid = await cid_of(request, "punish")
    try:
        days = int(request.query.get("days", 30))
    except ValueError:
        days = 30
    if days not in CHART_RANGES:
        days = 30
    d = await db.chart_series(cid, days)

    rules: dict[str, int] = {}
    for r in d["reasons"]:
        # ручное наказание к правилу отношения не имеет, а причина бота,
        # которую не берёт forgive_scope, — это капча, варны, набег, жалоба
        key = (moderation.forgive_scope(r["reason"]) or "other") if r["by_bot"] else "manual"
        rules[key] = rules.get(key, 0) + r["count"]

    def named(counts: dict, labels: dict) -> list[dict]:
        return [{"key": k, "label": labels.get(k, k), "count": n}
                for k, n in sorted(counts.items(), key=lambda kv: -kv[1]) if n]

    # день недели отдаём отдельно: на графике за 90 дней подпись «12.09» ни о
    # чём не говорит, а «сб» сразу объясняет провал
    series = []
    for row in d["series"]:
        dt = utils._local(row["ts"])
        series.append({
            "date": dt.strftime("%d.%m"),
            "dow": _DOW[dt.weekday()],
            "weekend": dt.weekday() >= 5,
            "msgs": row["msgs"], "joins": row["joins"], "leaves": row["leaves"],
        })
    return js({
        "days": days,
        "ranges": list(CHART_RANGES),
        "series": series,
        "totals": {
            "msgs": sum(r["msgs"] for r in series),
            "joins": sum(r["joins"] for r in series),
            "leaves": sum(r["leaves"] for r in series),
            "punished": sum(d["kinds"].values()),
            "forgiven": sum(d["forgiven"].values()),
            "manual": rules.get("manual", 0),
        },
        "prev": d["prev"],
        "kinds": named(d["kinds"], um._KIND_WORD),
        "rules": named(rules, _RULE_LABELS),
        "forgiven": named(d["forgiven"], config.WL_SCOPE_LABELS),
        "hours": d["hours"],
    })


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

    if widget == "prof_words":
        return {"count": len(await db.words_list(cid, "prof"))}

    if widget == "sub_chat":
        title = None
        if s.sub_chat_id:
            row = await db.get_chat(s.sub_chat_id)
            title = row["title"] if row and row["title"] else str(s.sub_chat_id)
        return {"chat_id": s.sub_chat_id, "title": title}

    if widget == "sub_text":
        # в режиме отказа письма нет — прячем и заготовку
        return {"count": len(await db.ans_list("sub", cid)),
                "shown": s.sub_action == "hold"}

    if widget == "nn_shadow":
        path = nn.shadow_path(cid)
        size = os.path.getsize(path) if os.path.exists(path) else 0
        return {"size": size, "on": s.nn_mode > 1, "path": path}

    if widget == "nn_subs":
        return {"sem_on": bool(s.sem_on), "burst_on": bool(s.burst_on),
                "phrases": len(await db.phrases_list(cid))}

    if widget == "watch_subs":
        return {"cas_on": bool(s.cas_on), "prof_on": bool(s.prof_on),
                "spam_profiles": len(await db.spam_profiles(cid))}

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


@routes.get("/api/chat/{cid}/nn/clusters/{index}")
async def api_nn_cluster_items(request: web.Request) -> web.Response:
    """Сообщения одной кучки — для точечной правки оценок."""
    cid = await cid_of(request)
    ids = nn.cluster_ids(cid, int(request.match_info["index"]))
    if ids is None:
        raise web.HTTPNotFound(text="Разбивка устарела, пересчитайте кучки.")
    return js({"items": [{"id": r["id"], "text": r["text"], "label": r["label"]}
                         for r in await db.samples_by_ids(cid, ids)]})


@routes.post("/api/chat/{cid}/nn/sample/{sid}")
async def api_nn_sample_label(request: web.Request) -> web.Response:
    cid = await cid_of(request)
    label = (await body(request)).get("label")
    if label not in ("spam", "ok"):
        raise web.HTTPBadRequest(text="bad label")
    sid = int(request.match_info["sid"])
    if not await db.sample_set_label(cid, sid, label):
        raise web.HTTPNotFound(text="Этой улики уже нет.")
    await db.add_event(cid, "nn", f"улика #{sid} размечена как {label} (панель)")
    nn._profile.pop(cid, None)        # разбивку не трогаем, иначе уедут номера
    return js({"ok": True})


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

def _word_kind(request) -> str:
    """Какой список правим: слова сообщений или слова профилей."""
    raw = request.query.get("kind") or ""
    return "prof" if raw == "prof" else "msg"


@routes.get("/api/chat/{cid}/words")
async def api_words(request: web.Request) -> web.Response:
    cid = await cid_of(request)
    kind = _word_kind(request)
    return js({
        "items": [{"id": r["id"], "word": r["word"], "mode": r["mode"],
                   "label": um._word_label(r["word"], r["mode"]),
                   "weight": um._weight_of(r),
                   "weight_label": config.WORD_WEIGHT_LABELS[um._weight_of(r)]}
                  for r in await db.words_list(cid, kind)],
        # вес идёт списком: панель показывает его выбором, а не догадкой
        "weights": [{"value": w, "label": config.WORD_WEIGHT_LABELS[w],
                     "hint": config.WORD_WEIGHT_HINT[w]}
                    for w in config.WORD_WEIGHTS]})


@routes.post("/api/chat/{cid}/words")
async def api_words_add(request: web.Request) -> web.Response:
    """Список через запятую или с новой строки; звёздочка = любые окончания."""
    cid = await cid_of(request)
    payload = await body(request)
    text = payload.get("text") or ""
    kind = "prof" if payload.get("kind") == "prof" else "msg"
    added = dupes = 0
    for raw in text.replace("\n", ",").split(","):
        w = raw.strip().lower()
        if not w:
            continue
        mode = "stem" if w.endswith("*") else "strict"
        w = w.rstrip("*")
        if not w:
            continue
        if await db.words_add(cid, w, mode, kind):
            added += 1
        else:
            dupes += 1
    flt.invalidate_words(cid)
    return js({"added": added, "dupes": dupes})


@routes.post("/api/chat/{cid}/words/{rid}/weight")
async def api_word_weight(request: web.Request) -> web.Response:
    """Вес слова для будущей единой оценки. Нынешних наказаний не касается."""
    cid = await cid_of(request)
    rid = int(request.match_info["rid"])
    row = await db.words_get(rid)
    if row is None or row["chat_id"] != cid:
        raise web.HTTPNotFound(text="нет такого слова")
    weight = int((await body(request)).get("weight") or 0)
    if weight not in config.WORD_WEIGHTS:
        raise web.HTTPBadRequest(text="bad weight")
    await db.words_set_weight(rid, weight)
    flt.invalidate_words(cid)
    return js({"ok": True})


@routes.delete("/api/chat/{cid}/words/{rid}")
async def api_words_del(request: web.Request) -> web.Response:
    cid = await cid_of(request)
    await db.words_remove(int(request.match_info["rid"]))
    flt.invalidate_words(cid)
    return js({"ok": True})


@routes.post("/api/chat/{cid}/words/clear")
async def api_words_clear(request: web.Request) -> web.Response:
    cid = await cid_of(request)
    n = await db.words_clear(cid, _word_kind(request))
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
    cid = await cid_of(request)
    await db.link_wl_remove(int(request.match_info["rid"]), cid)
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
    cid = await cid_of(request)
    await db.inline_wl_remove(int(request.match_info["rid"]), cid)
    return js({"ok": True})


# ---------- триггеры ----------

async def _row_of(request, getter, what: str):
    """Запись по id из пути — только если она из этого чата.

    Проверки одного cid мало: id записей идут подряд, и владелец своего чата,
    подставив чужой id, читал бы, правил и удалял чужие триггеры и счётчики.
    """
    cid = await cid_of(request)
    rid = int(request.match_info["rid"])
    row = await getter(rid)
    if row is None or row["chat_id"] != cid:
        raise web.HTTPNotFound(text=f"no {what}")
    return rid, row


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
    rid, r = await _row_of(request, db.trig_get, "trigger")
    return js({"trigger": dict(r), "answers": rows(await db.ans_list("trig", rid)),
               "cooldowns": list(config.CMD_COOLDOWN_PRESETS),
               "cooldown_labels": _cd_labels()})


@routes.post("/api/chat/{cid}/trigs/{rid}")
async def api_trig_edit(request: web.Request) -> web.Response:
    rid, _ = await _row_of(request, db.trig_get, "trigger")
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
    rid, _ = await _row_of(request, db.trig_get, "trigger")
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
    rid, r = await _row_of(request, db.cmd_get, "counter")
    return js({"cmd": dict(r), "answers": rows(await db.ans_list("cmd", rid)),
               "cooldowns": list(config.CMD_COOLDOWN_PRESETS),
               "cooldown_labels": _cd_labels()})


@routes.post("/api/chat/{cid}/cmds/{rid}")
async def api_cmd_edit(request: web.Request) -> web.Response:
    rid, _ = await _row_of(request, db.cmd_get, "counter")
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
    rid, _ = await _row_of(request, db.cmd_get, "counter")
    await db.ans_clear("cmd", rid)
    await db.cmd_remove(rid)
    return js({"ok": True})


# ---------- варианты ответов (триггеры, счётчики, приветствие, правила) ----------

_ANS_OWNERS = {"trig", "cmd", "welcome", "rules", "sub", "paste"}
# у этих владелец варианта — сам чат
_ANS_CHAT_OWNED = ("welcome", "rules", "sub", "paste")


async def _ans_owner_ok(owner: str, oid: int, cid: int) -> bool:
    """Свой ли объект у варианта ответа: у приветствия, правил, заявок и паст
    это сам чат, у триггера и счётчика — запись этого чата.

    Проверка жила в трёх местах и разошлась: удаление знало только
    приветствие и правила, загрузка — ещё заявки, и заготовки паст и заявок
    из панели не удалялись, а медиа к пастам не загружалось.
    """
    if owner in _ANS_CHAT_OWNED:
        return oid == cid
    if owner not in ("trig", "cmd"):
        return False
    row = await (db.trig_get(oid) if owner == "trig" else db.cmd_get(oid))
    return row is not None and row["chat_id"] == cid


async def _ans_scope(request) -> tuple[int, str, int]:
    """Чат, владелец варианта и его id — с проверкой, что владелец из этого чата."""
    cid = await cid_of(request)
    owner = request.query.get("owner") or (await body(request)).get("owner")
    oid = request.query.get("oid") or (await body(request)).get("oid")
    if owner not in _ANS_OWNERS:
        raise web.HTTPBadRequest(text="bad owner")
    oid = int(oid)
    if not await _ans_owner_ok(owner, oid, cid):
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
            # права — до записи на диск: иначе отвергнутый файл оставался лежать
            if not await _ans_owner_ok(owner, oid, cid):
                raise web.HTTPForbidden(text="not your object")
            kind = _media_kind(part.filename or "")
            saved = await _save_upload(part, cid, kind, owner)
    if saved is None:
        raise web.HTTPBadRequest(text="Файл не пришёл.")
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


async def _save_upload(part, cid: int, kind: str, purpose: str = "trig") -> str:
    """Слить файл на диск с потолком по размеру. Возвращает путь."""
    ext = os.path.splitext(part.filename or "")[1][:8] or ".bin"
    path = os.path.join(triggers.media_dir(cid, purpose),
                        f"{int(time.time() * 1000)}{ext}")
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
    if not await _ans_owner_ok(a["owner"], a["owner_id"], cid):
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
    cid = await cid_of(request, "punish")
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
    cid = await cid_of(request, "punish")
    await db.warn_reset(cid, int(request.match_info["uid"]))
    return js({"ok": True})


# ---------- наказания ----------

@routes.get("/api/chat/{cid}/active")
async def api_active(request: web.Request) -> web.Response:
    cid = await cid_of(request, "punish")
    items = []
    for r in await db.active_punishments(cid, limit=ACTIVE_LIMIT):
        items.append({
            "id": r["id"], "user_id": r["user_id"],
            "who": r["name"] or await db.user_label(r["user_id"], r["username"]),
            # ник отдаём отдельно: панель вешает его ссылкой на само имя,
            # чтобы строка не росла вширь
            "username": r["username"],
            "link": (f"https://t.me/{r['username']}" if r["username"]
                     else f"tg://user?id={r['user_id']}"),
            # выданное, а не применённое: мут не-участнику Telegram ставит баном
            "kind": utils.shown_kind(r["kind"], r["reason"]),
            "kind_label": um._KIND_WORD.get(utils.shown_kind(r["kind"], r["reason"]),
                                            r["kind"]),
            "until": "навсегда" if not r["until_ts"] else utils.fmt_ts(r["until_ts"]),
            "since": utils.fmt_ts(r["created"]) if r["created"] else None,
            "reason": _short_reason(r["reason"]),
        })
    return js({"items": items})


@routes.get("/api/chat/{cid}/status")
async def api_status(request: web.Request) -> web.Response:
    """Проверка статуса человека — та же карточка, что в меню наказаний."""
    cid = await cid_of(request, "punish")
    bot = bot_of(request)
    uid, err = await status_svc.parse_target(bot, request.query.get("q", ""))
    if uid is None:
        return js({"ok": False, "error": err})
    chats = await db.chats_for(uid_of(request))
    here = await db.get_chat(cid)
    return js({"ok": True, "here": (here["title"] if here is not None else "") or "",
               **await status_svc.collect(bot, uid, chats, first=cid)})


@routes.post("/api/chat/{cid}/spamprofile")
async def api_spam_profile(request: web.Request) -> web.Response:
    """«Спам-профиль» со страницы проверки: профиль — в базу этого чата."""
    cid = await cid_of(request, "punish")
    data = await body(request)
    try:
        uid = int(data.get("user_id") or 0)
    except (TypeError, ValueError):
        uid = 0
    if uid <= 0:
        raise web.HTTPBadRequest(text="bad user_id")
    ok, note = await nn.remember_spam_profile(bot_of(request), cid, uid)
    if ok:
        await db.add_event(cid, "card", f"спам-профиль в базу: {uid} "
                                        f"by {uid_of(request)} (панель)")
    return js({"ok": ok, "note": note})


@routes.get("/api/chat/{cid}/forgiven")
async def api_forgiven(request: web.Request) -> web.Response:
    cid = await cid_of(request, "punish")
    items = []
    for r in await db.forgiven_list(cid):
        why, _swapped = utils.short_reason(r["reason"])
        items.append({
            "id": r["id"], "user_id": r["user_id"],
            "who": r["name"] or (f"@{r['username']}" if r["username"]
                                 else str(r["user_id"])),
            "link": (f"https://t.me/{r['username']}" if r["username"]
                     else f"tg://user?id={r['user_id']}"),
            "scope": r["scope"],
            "scope_label": config.WL_SCOPE_LABELS.get(r["scope"], r["scope"]),
            "since": utils.fmt_ts(r["created"]),
            "reason": why,
        })
    return js({"items": items})


@routes.get("/api/chat/{cid}/spamprofiles")
async def api_spam_profiles(request: web.Request) -> web.Response:
    cid = await cid_of(request)
    items = [{"id": r["id"], "user_id": r["user_id"],
              "who": await db.user_handle(r["user_id"]) if r["user_id"] else "—",
              "when": utils.fmt_ts(r["ts"]), "text": r["text"]}
             for r in await db.spam_profiles(cid)]
    return js({"items": items})


@routes.delete("/api/chat/{cid}/spamprofiles/{rid}")
async def api_spam_profile_del(request: web.Request) -> web.Response:
    cid = await cid_of(request)
    row = await db.spam_profile_delete(cid, int(request.match_info["rid"]))
    if row is None:
        raise web.HTTPNotFound(text="Этой записи уже нет.")
    nn.invalidate(cid)
    await db.add_event(cid, "card", f"спам-профиль убран из базы: "
                                    f"{row['user_id']} by {uid_of(request)} (панель)")
    return js({"ok": True})


@routes.delete("/api/chat/{cid}/forgiven/{rid}")
async def api_forgiven_del(request: web.Request) -> web.Response:
    cid = await cid_of(request, "punish")
    rid = int(request.match_info["rid"])
    row = await db.forgiven_get(rid)
    if row is None or row["chat_id"] != cid:
        raise web.HTTPNotFound(text="нет такой записи")
    await db.forgiven_remove(rid)
    await db.add_event(cid, "card", f"прощение снято: {row['user_id']} (панель)")
    return js({"ok": True})


@routes.post("/api/chat/{cid}/active/{pid}/lift")
async def api_lift(request: web.Request) -> web.Response:
    cid = await cid_of(request, "punish")
    pid = int(request.match_info["pid"])
    p = await db.get_punishment(pid)
    if p is None or p["chat_id"] != cid:
        raise web.HTTPForbidden(text="not your punishment")
    ok, msg, _ = await moderation.lift_punishment(bot_of(request), pid, invite=False)
    if ok:
        runtime.spawn(net_svc.lift(bot_of(request), p["chat_id"], p["user_id"]))
    return js({"ok": ok, "note": _strip_tags(msg)})


_MASS = {"ban": um._ban_one, "unban": um._unban_one, "kick": um._kick_one}


@routes.post("/api/chat/{cid}/mass")
async def api_mass(request: web.Request) -> web.Response:
    """Массовые бан/разбан/кик списком id и @username — как в меню."""
    cid = await cid_of(request, "punish")
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
    cid = await cid_of(request, "owner")
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


# ---------- админы чата в боте ----------

def _admin_row(r) -> dict:
    return {"user_id": r["user_id"], "level": r["level"],
            "who": r["name"] or (f"@{r['username']}" if r["username"]
                                 else str(r["user_id"])),
            "username": r["username"], "created": r["created"]}


@routes.get("/api/chat/{cid}/admins")
async def api_admins(request: web.Request) -> web.Response:
    cid = await cid_of(request, "owner")
    return js({"items": [_admin_row(r) for r in await db.chat_admin_list(cid)],
               "levels": {k: db.LEVEL_NAMES[k] for k in db.ADMIN_LEVELS}})


@routes.post("/api/chat/{cid}/admins")
async def api_admin_add(request: web.Request) -> web.Response:
    """Добавить админа или сменить ему уровень."""
    cid = await cid_of(request, "owner")
    data = await body(request)
    level = data.get("level") or "punish"
    if level not in db.ADMIN_LEVELS:
        raise web.HTTPBadRequest(text="Неизвестный уровень.")
    bot = bot_of(request)
    target = str(data.get("target") or "").strip()
    uid = int(data["user_id"]) if data.get("user_id") else None
    if uid is None:
        if target.lstrip("-").isdigit():
            uid = int(target)
        elif target.startswith("@") and len(target) > 3:
            uid, _name = await resolve.by_username(bot, target)
        if uid is None:
            raise web.HTTPBadRequest(text="Нужен id или @username.")
    if uid == uid_of(request):
        raise web.HTTPBadRequest(text="Это вы, у вас и так все права.")
    # доступ к чужому чату не должен появляться из ниоткуда: пускаем только
    # тех, кому владелец уже доверил админку в самом Telegram
    if uid not in await adm_cache.chat_admin_ids(bot, cid):
        raise web.HTTPBadRequest(
            text="Этот человек не админ чата. Сначала выдайте права в Telegram.")
    name = username = None
    try:
        member = await bot.get_chat_member(cid, uid)
        name, username = member.user.full_name, member.user.username
    except Exception:
        logger.debug("имя админа %s не узнать", uid, exc_info=True)
    await db.chat_admin_add(cid, uid, level, username, name, uid_of(request))
    await db.add_event(cid, "card",
                       f"доступ в бот: {uid} ({level}) by {uid_of(request)}")
    return js({"items": [_admin_row(r) for r in await db.chat_admin_list(cid)],
               "note": f"{name or uid}: {db.LEVEL_NAMES[level]}"})


@routes.delete("/api/chat/{cid}/admins/{uid}")
async def api_admin_del(request: web.Request) -> web.Response:
    cid = await cid_of(request, "owner")
    uid = int(request.match_info["uid"])
    await db.chat_admin_remove(cid, uid)
    await db.add_event(cid, "card", f"доступ в бот снят: {uid} by {uid_of(request)}")
    return js({"items": [_admin_row(r) for r in await db.chat_admin_list(cid)]})

@routes.get("/api/chat/{cid}/copy")
async def api_copy_sources(request: web.Request) -> web.Response:
    cid = await cid_of(request, "owner")
    others = [c for c in await db.chats_for(uid_of(request)) if c["chat_id"] != cid]
    return js({
        "chats": [{"chat_id": c["chat_id"], "title": c["title"] or str(c["chat_id"])}
                  for c in others],
        "groups": [{"key": k, "label": transfer.GROUPS[k][0]}
                   for k in transfer.shown_groups()],
    })


@routes.post("/api/chat/{cid}/copy")
async def api_copy(request: web.Request) -> web.Response:
    cid = await cid_of(request, "owner")
    data = await body(request)
    src = int(data.get("src") or 0)
    groups = [g for g in (data.get("groups") or []) if g in transfer.shown_groups()]
    if not await auth.owns(uid_of(request), src, "owner"):
        raise web.HTTPForbidden(text="Чужой чат-источник.")
    if not groups:
        raise web.HTTPBadRequest(text="Не выбрано ни одного раздела.")
    stats = await transfer.copy_chat(src, cid, set(groups))
    await db.kv_set(um.setup_key(cid), "1")
    flt.invalidate_words(cid)
    return js({"copied": stats})


# ---------- файл настроек ----------
#
# Скачивание прямо из мини-аппа ненадёжно: внутри Telegram у WebView свои
# представления о загрузках, а наш API ещё и требует подпись в заголовке,
# которую обычная ссылка не несёт. Поэтому архив бот присылает в личку —
# туда же, куда его присылает меню.

@routes.post("/api/chat/{cid}/export")
async def api_export(request: web.Request) -> web.Response:
    from aiogram.types import BufferedInputFile
    cid = await cid_of(request, "owner")
    uid = uid_of(request)
    data, stats = await transfer.export_chat(cid)
    ch = await db.get_chat(cid)
    inside = ", ".join(f"{k} {v}" for k, v in stats.items() if v)
    try:
        await bot_of(request).send_document(
            uid, BufferedInputFile(data, filename=transfer.export_name(cid)),
            caption=(f"📤 Настройки «{utils.esc(ch['title'] if ch else cid)}»"
                     + (f"\n{utils.esc(inside)}" if inside else "")))
    except Exception:
        logger.warning("выгрузка %s: не отправить в личку %s", cid, uid, exc_info=True)
        raise web.HTTPBadRequest(
            text="Не получилось прислать файл в личку. Напишите боту /start и повторите.")
    await db.add_event(cid, "card", f"настройки выгружены в файл by {uid}")
    return js({"note": "Файл отправлен вам в личку с ботом"})


@routes.post("/api/chat/{cid}/import")
async def api_import(request: web.Request) -> web.Response:
    """Принять файл и показать, что в нём. Ничего не меняет до подтверждения."""
    cid = await cid_of(request, "owner")
    reader = await request.multipart()
    raw = None
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "file":
            raw = await part.read(decode=False)
    if not raw:
        raise web.HTTPBadRequest(text="Файл не пришёл.")
    try:
        snap, media = transfer.parse_archive(bytes(raw))
    except transfer.BadArchive as e:
        raise web.HTTPBadRequest(text=str(e).capitalize())
    if not snap["groups"]:
        raise web.HTTPBadRequest(text="В файле нет ни одного раздела.")
    transfer.stash(uid_of(request), cid, snap, media)
    return js({
        "title": snap.get("chat_title"),
        "inside": transfer.describe(snap),
        "groups": [{"key": g, "label": transfer.GROUPS[g][0]}
                   for g in transfer.shown_groups() if g in snap["groups"]],
    })


@routes.post("/api/chat/{cid}/import/apply")
async def api_import_apply(request: web.Request) -> web.Response:
    cid = await cid_of(request, "owner")
    uid = uid_of(request)
    got = transfer.stashed(uid, cid)
    if got is None:
        raise web.HTTPBadRequest(text="Файл уже забыт — загрузите его ещё раз.")
    snap, media = got
    groups = {g for g in ((await body(request)).get("groups") or [])
              if g in snap["groups"] and g in transfer.shown_groups()}
    if not groups:
        raise web.HTTPBadRequest(text="Не выбрано ни одного раздела.")
    stats = await transfer.apply(cid, snap, groups, media.get)
    transfer.unstash(uid, cid)
    await db.kv_set(um.setup_key(cid), "1")
    flt.invalidate_words(cid)
    moved = ", ".join(f"{k}: {v}" for k, v in stats.items() if v)
    await db.add_event(cid, "card", f"настройки загружены из файла by {uid}: "
                                    f"{moved or 'пусто'}")
    return js({"copied": stats})


@routes.post("/api/chat/{cid}/setup-skip")
async def api_setup_skip(request: web.Request) -> web.Response:
    cid = await cid_of(request, "owner")
    await db.kv_set(um.setup_key(cid), "1")
    return js({"ok": True})


@routes.post("/api/chat/{cid}/leave")
async def api_leave(request: web.Request) -> web.Response:
    cid = await cid_of(request, "owner")
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
        by_hand = bit in config.GAME_FIELDS
        item = {"bit": bit, "label": label, "how": how, "about": about,
                "on": bool(s.games_on & bit), "admins": bool(s.games_adm & bit),
                "by_hand": by_hand}
        if bit == config.GAME_PASTE:
            item["paste"] = True
            item["min"] = s.paste_min
            item["cd"] = s.paste_cd
            item["cd_label"] = (utils.fmt_minutes(s.paste_cd) if s.paste_cd
                                else "без паузы")
            item["answers"] = len(await db.ans_list("paste", cid))
        if by_hand:
            kind_field, min_field = config.GAME_FIELDS[bit]
            kind, minutes = getattr(s, kind_field), getattr(s, min_field)
            item["kind"] = kind
            item["minutes"] = minutes
            item["prize"] = "бан" if kind == "ban" else f"мут на {utils.fmt_minutes(minutes)}"
        items.append(item)
    return js({"items": items,
               "mutes": [{"value": m, "label": utils.fmt_minutes(m)}
                         for m in config.MUTE_PRESETS],
               "paste_mins": list(config.PASTE_MIN_PRESETS),
               "paste_cds": [{"value": c,
                              "label": utils.fmt_minutes(c) if c else "без паузы"}
                             for c in config.PASTE_CD_PRESETS]})


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


@routes.post("/api/chat/{cid}/games/paste")
async def api_game_paste(request: web.Request) -> web.Response:
    """Порог длины и пауза у ответа на пасты."""
    cid = await cid_of(request)
    data = await body(request)
    if "min" in data:
        value = int(data["min"])
        if value not in config.PASTE_MIN_PRESETS:
            raise web.HTTPBadRequest(text="bad min")
        await db.set_setting(cid, "paste_min", value)
    if "cd" in data:
        value = int(data["cd"])
        if value not in config.PASTE_CD_PRESETS:
            raise web.HTTPBadRequest(text="bad cd")
        await db.set_setting(cid, "paste_cd", value)
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
    cid = await cid_of(request, "owner")
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


@routes.get("/api/knocks")
async def api_knocks(request: web.Request) -> web.Response:
    owner_only(request)
    items = []
    for r in await db.knock_list():
        items.append({
            "user_id": r["user_id"],
            "username": r["username"],
            "who": await db.user_label(r["user_id"], r["username"],
                                       fallback=r["name"]),
            "first": utils.rel_time(r["first_ts"]),
            "last": utils.rel_time(r["last_ts"]),
            "dm": r["dm_cnt"],
            "adds": r["add_cnt"],
            "chat": r["last_chat"],
            # допуск могли дать уже после отказа — показываем нынешнее
            "granted": await db.access_allowed(r["user_id"], r["username"]),
        })
    return js({"items": items})


@routes.post("/api/knocks/{uid}/access")
async def api_knock_grant(request: web.Request) -> web.Response:
    owner_only(request)
    uid = int(request.match_info["uid"])
    row = await db.knock_get(uid)
    await db.access_add(uid, row["username"] if row else None,
                        row["name"] if row else None)
    return js({"ok": True})


@routes.delete("/api/knocks/{uid}")
async def api_knock_del(request: web.Request) -> web.Response:
    owner_only(request)
    await db.knock_remove(int(request.match_info["uid"]))
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


@routes.post("/api/chat/{cid}/sub-chat")
async def api_sub_chat(request: web.Request) -> web.Response:
    """Канал, подписку на который проверяем у входящих."""
    cid = await cid_of(request)
    raw = ((await body(request)).get("target") or "").strip()
    if raw in ("", "-"):
        await db.set_setting(cid, "sub_chat_id", 0)
        return js({"ok": True, "chat_id": 0})
    target = int(raw) if raw.lstrip("-").isdigit() else raw
    try:
        ch = await bot_of(request).get_chat(target)
    except Exception as e:
        raise web.HTTPBadRequest(
            text=f"Канал не открылся: {e}. Бот должен быть в нём администратором.")
    if ch.type not in ("channel", "supergroup", "group"):
        raise web.HTTPBadRequest(text="Это не канал и не группа.")
    await db.set_setting(cid, "sub_chat_id", ch.id)
    return js({"ok": True, "chat_id": ch.id, "title": ch.title})


@routes.get("/api/seed")
async def api_seed(request: web.Request) -> web.Response:
    owner_only(request)
    label = request.query.get("label") or None
    if label not in ("spam", "ok", None):
        label = None
    kind = "prof" if request.query.get("kind") == "prof" else "msg"
    q = (request.query.get("q") or "").strip() or None
    try:
        page = max(0, int(request.query.get("page") or 0))
    except ValueError:
        raise web.HTTPBadRequest(text="page: нужно число.")
    total = await db.seed_count(label, q, kind)
    rows = await db.seed_page(label, q, page * SEED_PAGE, SEED_PAGE, kind)
    return js({
        "kind": kind,
        "stats": await db.seed_stats(kind),
        "msg_stats": await db.seed_stats("msg"),
        "prof_stats": await db.seed_stats("prof"),
        "face_seed": config.NN_FACE_SEED,
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
    kind = "prof" if p.get("kind") == "prof" else "msg"
    if p.get("all"):
        gone = await db.seed_delete_where(None, None, kind)
        what = f"очищен вид {kind}"
    elif p.get("ids"):
        try:
            ids = [int(x) for x in p["ids"]]
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text="ids: нужны числа.")
        gone = await db.seed_delete(ids)
        what = "удалены примеры"
    else:
        label = p.get("label") if p.get("label") in ("spam", "ok") else None
        q = (p.get("q") or "").strip() or None
        if not label and not q:
            raise web.HTTPBadRequest(text="Нечего удалять: задайте поиск или метку.")
        gone = await db.seed_delete_where(label, q, kind)
        what = f"удалено по фильтру ({q or label})"
    if gone:
        nn.invalidate()          # набор подмешан всем молодым чатам
        await db.add_event(None, "nn", f"стартовый набор: {what}, {gone} шт "
                                       f"by {uid_of(request)}")
    return js({"ok": True, "gone": gone})
