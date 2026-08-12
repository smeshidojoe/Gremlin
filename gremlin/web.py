"""Mini App: aiohttp-сервер поверх той же db.py и schema.py, что и меню бота.

Настройки не дублируются — фронт читает schema.to_dict() и пишет через db.set_setting.
Авторизация: Telegram WebApp initData (HMAC по токену бота) в заголовке X-Init-Data.
"""
import hashlib
import hmac
import json
import logging
import os
import time
from urllib.parse import parse_qsl

from aiohttp import web

from . import config, db, schema
from .services import filters as flt

logger = logging.getLogger("gremlin.web")

_INDEX = os.path.join(os.path.dirname(__file__), "webapp", "index.html")


# ---------- авторизация ----------

def _check_init_data(init_data: str, bot_token: str, max_age: int = 86400) -> dict | None:
    """Проверить подпись Telegram WebApp initData. Вернуть dict пользователя или None."""
    if not init_data:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received:
        return None
    check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        return None
    try:
        auth_date = int(pairs.get("auth_date", "0"))
        if max_age and time.time() - auth_date > max_age:
            return None
        return json.loads(pairs["user"])
    except (KeyError, ValueError):
        return None


@web.middleware
async def _auth_mw(request: web.Request, handler):
    if request.path.startswith("/api/"):
        user = _check_init_data(request.headers.get("X-Init-Data", ""), config.BOT_TOKEN)
        if user is None:
            return web.json_response({"error": "auth"}, status=401)
        request["user_id"] = int(user["id"])
    return await handler(request)


async def _guard(request: web.Request) -> int:
    """Вернуть cid, если юзер вправе им управлять, иначе бросить HTTPForbidden."""
    cid = int(request.query.get("cid") or (await request.json()).get("cid"))
    uid = request["user_id"]
    if uid in config.ADMIN_IDS:
        return cid
    ch = await db.get_chat(cid)
    if ch is None or ch["owner_id"] != uid:
        raise web.HTTPForbidden(text=json.dumps({"error": "forbidden"}))
    return cid


# ---------- статика ----------

async def index(request: web.Request) -> web.Response:
    return web.FileResponse(_INDEX)


# ---------- API ----------

async def api_chats(request: web.Request) -> web.Response:
    uid = request["user_id"]
    chats = await db.chats_of(uid)
    return web.json_response([
        {"id": c["chat_id"], "title": c["title"] or str(c["chat_id"])} for c in chats
    ])


async def api_settings(request: web.Request) -> web.Response:
    cid = await _guard(request)
    s = await db.get_settings(cid)
    ch = await db.get_chat(cid)
    data = schema.to_dict(s)
    data["chat"] = {"id": cid, "title": ch["title"] if ch else str(cid)}
    data["card_bits"] = [
        {"bit": bit, "label": label, "on": bool(s.card_mask & bit)}
        for bit, label in config.CARD_BITS
    ]
    data["log_chat_id"] = s.log_chat_id
    data["log_candidates"] = [
        {"id": c["chat_id"], "title": c["title"] or str(c["chat_id"])}
        for c in await db.chats_of(ch["owner_id"] if ch else request["user_id"])
    ]
    data["scopes"] = [{"key": k, "label": config.WL_SCOPE_LABELS[k]} for k in config.WL_SCOPES]
    data["words"] = [
        {"id": r["id"], "word": r["word"], "mode": r["mode"]} for r in await db.words_list(cid)
    ]
    data["wl"] = [
        {"id": r["id"], "who": (f"@{r['username']}" if r["username"] else str(r["user_id"])),
         "scope": r["scope"], "scope_label": config.WL_SCOPE_LABELS[r["scope"]]}
        for r in await db.wl_list(cid)
    ]
    data["anon"] = [
        {"id": r["id"], "title": r["title"] or str(r["sender_id"]), "sender_id": r["sender_id"]}
        for r in await db.anon_list(cid)
    ]
    return web.json_response(data)


async def api_set(request: web.Request) -> web.Response:
    cid = await _guard(request)
    body = await request.json()
    try:
        value = schema.validate(body["field"], body["value"])
    except (KeyError, ValueError) as e:
        return web.json_response({"error": str(e)}, status=400)
    await db.set_setting(cid, body["field"], value)
    return web.json_response({"ok": True, "value": value})


async def api_cardbit(request: web.Request) -> web.Response:
    cid = await _guard(request)
    bit = int((await request.json())["bit"])
    if bit not in {b for b, _ in config.CARD_BITS}:
        return web.json_response({"error": "bad bit"}, status=400)
    s = await db.get_settings(cid)
    await db.set_setting(cid, "card_mask", s.card_mask ^ bit)
    return web.json_response({"ok": True})


async def api_log(request: web.Request) -> web.Response:
    cid = await _guard(request)
    log = (await request.json()).get("log_chat_id")
    await db.set_setting(cid, "log_chat_id", int(log) if log else None)
    return web.json_response({"ok": True})


async def api_words_add(request: web.Request) -> web.Response:
    cid = await _guard(request)
    text = (await request.json()).get("text", "")
    added = 0
    for raw in text.replace("\n", ",").split(","):
        w = raw.strip().lower()
        if not w:
            continue
        mode = "stem" if w.endswith("*") else "strict"
        w = w.rstrip("*")
        if w:
            await db.words_add(cid, w, mode)
            added += 1
    flt.invalidate_words(cid)
    return web.json_response({"ok": True, "added": added})


async def api_words_del(request: web.Request) -> web.Response:
    cid = await _guard(request)
    await db.words_remove(int((await request.json())["id"]))
    flt.invalidate_words(cid)
    return web.json_response({"ok": True})


async def api_wl_add(request: web.Request) -> web.Response:
    cid = await _guard(request)
    body = await request.json()
    target = str(body.get("target", "")).strip()
    scope = body.get("scope", "all")
    if scope not in config.WL_SCOPES:
        return web.json_response({"error": "bad scope"}, status=400)
    user_id, username = None, None
    if target.lstrip("-").isdigit():
        user_id = int(target)
    elif target.startswith("@") and len(target) > 3:
        username = target
    else:
        return web.json_response({"error": "нужен id или @username"}, status=400)
    await db.wl_add(cid, user_id, username, scope)
    return web.json_response({"ok": True})


async def api_wl_del(request: web.Request) -> web.Response:
    cid = await _guard(request)
    await db.wl_remove(int((await request.json())["id"]))
    return web.json_response({"ok": True})


async def api_anon_add(request: web.Request) -> web.Response:
    cid = await _guard(request)
    body = await request.json()
    sid = str(body.get("sender_id", "")).strip()
    if not sid.lstrip("-").isdigit():
        return web.json_response({"error": "нужен числовой id"}, status=400)
    await db.anon_add(cid, int(sid), body.get("title") or None)
    return web.json_response({"ok": True})


async def api_anon_del(request: web.Request) -> web.Response:
    cid = await _guard(request)
    await db.anon_remove(int((await request.json())["id"]))
    return web.json_response({"ok": True})


def build_app() -> web.Application:
    app = web.Application(middlewares=[_auth_mw])
    app.router.add_get("/", index)
    app.router.add_get("/api/chats", api_chats)
    app.router.add_get("/api/settings", api_settings)
    app.router.add_post("/api/set", api_set)
    app.router.add_post("/api/cardbit", api_cardbit)
    app.router.add_post("/api/log", api_log)
    app.router.add_post("/api/words/add", api_words_add)
    app.router.add_post("/api/words/del", api_words_del)
    app.router.add_post("/api/wl/add", api_wl_add)
    app.router.add_post("/api/wl/del", api_wl_del)
    app.router.add_post("/api/anon/add", api_anon_add)
    app.router.add_post("/api/anon/del", api_anon_del)
    return app


async def start() -> web.AppRunner:
    runner = web.AppRunner(build_app())
    await runner.setup()
    site = web.TCPSite(runner, config.WEBAPP_HOST, config.WEBAPP_PORT)
    await site.start()
    logger.info("mini app on %s:%s (public %s)", config.WEBAPP_HOST, config.WEBAPP_PORT, config.WEBAPP_URL)
    return runner
