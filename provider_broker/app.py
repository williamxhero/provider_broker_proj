import base64
import hmac
import json
import math
from pathlib import Path

from aiohttp import web

from .db import Store
from .settings import Settings
from .source import sync_cpa
from .upstream import UpstreamFailure, invoke_stream, route

WEB_ROOT = Path(__file__).with_name("web")


def session_value(secret: str) -> str:
    signature = hmac.digest(secret.encode(), b"provider-broker-web-v1", "sha256")
    return base64.urlsafe_b64encode(signature).decode()


def session_ok(request) -> bool:
    expected = session_value(request.app["settings"].session_secret)
    return hmac.compare_digest(request.cookies.get("broker_session", ""), expected)


def auth(token_name):
    @web.middleware
    async def middleware(request, handler):
        if request.path in ("/healthz", "/", "/login") or request.path.startswith("/static/"):
            return await handler(request)
        if request.path.startswith("/admin/"):
            required = request.app["settings"].admin_token
        elif request.path.startswith("/v1/"):
            required = request.app["settings"].client_token
        else:
            return await handler(request)
        value = request.headers.get("Authorization", "")
        admin_session = request.path.startswith("/admin/") and session_ok(request)
        if not admin_session and not hmac.compare_digest(value, f"Bearer {required}"):
            return web.json_response({"error": f"{token_name} authentication required"}, status=401)
        return await handler(request)

    return middleware


async def generate(request):
    body = await request.json()
    if "model" in body or not isinstance(body.get("prompt"), str):
        return web.json_response({"error": "prompt and intellect are required; model is not a capability selector"}, status=400)
    tier = body.get("intellect")
    if tier not in ("standard", "smart", "expert"):
        return web.json_response({"error": "model must be standard, smart, or expert"}, status=400)
    try:
        result = await route(request.app["store"], tier, body, request.app["settings"].parallel_cap)
    except UpstreamFailure as exc:
        return web.json_response({"error": "all eligible providers failed", "attempts": exc.attempts}, status=503)
    return web.json_response({
        "status": "completed", "intellect": tier, "fulfilled_intellect": result["fulfilled_intellect"],
        "effort": body.get("effort"), "deadline_ms": body.get("deadline_ms"), "output_token_limit": body.get("output_token_limit"),
        "actual_model": result["actual_model"], "output_text": result["text"], "provider": result["provider"],
        "request_id": result["request_id"], "usage": result["usage"],
        "ttft_ms": result["latency_ms"], "attempts": result["attempts"], "cost_estimate": result["cost"],
    })


async def stream(request):
    body = await request.json()
    tier = body.get("intellect")
    if "model" in body or not isinstance(body.get("prompt"), str) or tier not in ("standard", "smart", "expert"):
        return web.json_response({"error": "prompt and valid intellect are required"}, status=400)
    try:
        result = await route(request.app["store"], tier, body, request.app["settings"].parallel_cap, invoker=invoke_stream)
    except UpstreamFailure as exc:
        return web.json_response({"error": "all eligible providers failed", "attempts": exc.attempts}, status=503)
    sse = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
    await sse.prepare(request)
    for chunk in result["chunks"]:
        await sse.write(f"event: delta\ndata: {json.dumps({'text': chunk})}\n\n".encode())
    final = {
        "status": "completed", "intellect": tier, "fulfilled_intellect": result["fulfilled_intellect"],
        "actual_model": result["actual_model"], "output_text": result["text"], "provider": result["provider"],
        "attempts": result["attempts"], "request_id": result["request_id"], "usage": result["usage"],
        "cost_estimate": result["cost"], "ttft_ms": result["latency_ms"],
    }
    await sse.write(f"event: final\ndata: {json.dumps(final)}\n\n".encode())
    await sse.write_eof()
    return sse


async def sync(request):
    before = {provider["fingerprint"] for provider in request.app["store"].inventory()}
    try:
        sync_result = await sync_cpa(request.app["store"], request.app["settings"].cpa_url, request.app["settings"].cpa_token)
    except Exception:
        return web.json_response({"error": "sync failed"}, status=502)
    providers_now = request.app["store"].inventory()
    after = {provider["fingerprint"] for provider in providers_now}
    return web.json_response({"added": len(after - before), "updated": len(after & before), "offlined": len(before - after), "inventory_failures": sync_result["inventory_failures"], "last_successful_sync": max((provider["synced_at"] for provider in providers_now), default=None)})


async def inventory(request):
    return web.json_response({"providers": request.app["store"].inventory()})


async def providers(request):
    return web.json_response({"providers": request.app["store"].inventory()})


async def summary(request):
    window = request.query.get("window", "24h")
    if window not in ("1h", "24h", "7d", "30d"):
        return web.json_response({"error": "invalid window"}, status=400)
    modifiers = {"1h": "-1 hour", "24h": "-24 hours", "7d": "-7 days", "30d": "-30 days"}
    db = request.app["store"].conn
    row = db.execute("SELECT avg(success),avg(latency_ms) FROM observation WHERE created_at >= datetime('now',?)", (modifiers[window],)).fetchone()
    routable = len({provider.fingerprint for tier in ("standard", "smart", "expert") for provider in request.app["store"].providers(tier)})
    synced = db.execute("SELECT max(synced_at) FROM source_provider").fetchone()[0]
    return web.json_response({"routable_apis": routable, "technical_success_rate": row[0], "avg_ttft_ms": row[1], "last_successful_sync": synced})


async def quality(request):
    window = request.query.get("window", "24h")
    if window not in ("1h", "24h", "7d", "30d"):
        return web.json_response({"error": "invalid window"}, status=400)
    return web.json_response(request.app["store"].quality(window))


async def calls(request):
    try:
        limit = int(request.query.get("limit", 50))
    except ValueError:
        limit = 0
    if not 1 <= limit <= 100:
        return web.json_response({"error": "invalid limit"}, status=400)
    cursor = request.query.get("cursor")
    if cursor and (not cursor.isdigit() or int(cursor) < 1):
        return web.json_response({"error": "invalid cursor"}, status=400)
    window = request.query.get("window", "24h")
    if window not in ("1h", "24h", "7d", "30d"):
        return web.json_response({"error": "invalid window"}, status=400)
    items = request.app["store"].calls(limit, cursor, request.query.get("provider"), request.query.get("status"), window)
    return web.json_response({"items": items, "next_cursor": str(items[-1]["id"]) if len(items) == limit else None})


async def catalog(request):
    from .catalog import CATALOG
    counts = request.app["store"].catalog_counts()
    built = {name: value | {"available_provider_count": counts.get(name, 0)} for name, value in CATALOG.items()}
    return web.json_response({"catalog": built})


async def calibrate_catalog(request):
    from .catalog import CATALOG
    body = await request.json()
    official = CATALOG.get(request.match_info["model"])
    if official is None or body != official:
        return web.json_response({"error": "explicit family, intellect, and all official prices required"}, status=400)
    return web.json_response({"calibrated": request.match_info["model"], "catalog": body})


async def update_policy(request):
    body = await request.json()
    allowed = {"note", "multiplier", "enabled", "preference", "max_parallel", "calibrated", "tiers"}
    numeric = lambda value: type(value) in (int, float) and math.isfinite(value)
    valid = (
        isinstance(body, dict) and bool(body) and set(body) <= allowed
        and ("note" not in body or isinstance(body["note"], str))
        and ("enabled" not in body or type(body["enabled"]) is bool)
        and ("calibrated" not in body or type(body["calibrated"]) is bool)
        and ("multiplier" not in body or numeric(body["multiplier"]) and body["multiplier"] > 0)
        and ("preference" not in body or type(body["preference"]) is int)
        and ("max_parallel" not in body or type(body["max_parallel"]) is int and 1 <= body["max_parallel"] <= 32)
        and ("tiers" not in body or isinstance(body["tiers"], list) and bool(body["tiers"])
             and all(tier in ("standard", "smart", "expert") for tier in body["tiers"]))
    )
    if not valid:
        return web.json_response({"error": "invalid policy"}, status=400)
    if not request.app["store"].update_policy(request.match_info["fingerprint"], body):
        return web.json_response({"error": "provider not found"}, status=404)
    return web.json_response({"updated": True})


async def home(request):
    if not session_ok(request):
        raise web.HTTPFound("/login")
    return web.FileResponse(WEB_ROOT / "index.html")


async def health(request):
    return web.json_response({"status": "ok"})


async def login(request):
    if request.method == "GET":
        return web.FileResponse(WEB_ROOT / "login.html")
    form = await request.post()
    if not hmac.compare_digest(str(form.get("token", "")), request.app["settings"].admin_token):
        return web.Response(text="invalid credentials", status=401)
    response = web.HTTPFound("/")
    response.set_cookie("broker_session", session_value(request.app["settings"].session_secret), httponly=True, samesite="Strict", secure=False, max_age=28800)
    return response


def create_app(settings: Settings):
    app = web.Application(middlewares=[auth("client")])
    app["settings"] = settings
    app["store"] = Store(settings.database_path, settings.key_bytes())
    app.router.add_static("/static/", WEB_ROOT)
    app.add_routes([
        web.get("/", home), web.get("/healthz", health), web.get("/login", login), web.post("/login", login),
        web.post("/v1/generate", generate), web.post("/v1/generate/stream", stream), web.post("/admin/v1/sync", sync),
        web.get("/admin/v1/inventory", inventory), web.get("/admin/v1/providers", providers), web.get("/admin/v1/summary", summary),
        web.get("/admin/v1/quality", quality), web.get("/admin/v1/calls", calls), web.get("/admin/v1/catalog", catalog),
        web.patch("/admin/v1/catalog/{model}", calibrate_catalog), web.put("/admin/v1/policy/{fingerprint}", update_policy),
        web.patch("/admin/v1/policy/{fingerprint}", update_policy),
    ])
    return app


def main():
    settings = Settings.from_env()
    web.run_app(create_app(settings), host="192.168.50.2", port=8817)
