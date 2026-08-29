import hmac
import json
import math
import re
from pathlib import Path

from aiohttp import web

from .db import Store
from .settings import Settings
from .source import sync_cpa
from .upstream import UpstreamFailure, invoke_stream, route

WEB_ROOT = Path(__file__).with_name("web")


def auth():
    @web.middleware
    async def middleware(request, handler):
        if not request.path.startswith("/v1/"):
            return await handler(request)
        required = request.app["settings"].client_token
        value = request.headers.get("Authorization", "")
        if not hmac.compare_digest(value, f"Bearer {required}"):
            return web.json_response({"error": "client authentication required"}, status=401)
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
    counts = request.app["store"].catalog_counts()
    built = {name: value | {"available_provider_count": counts.get(name, 0)} for name, value in request.app["store"].catalog().items()}
    return web.json_response({"catalog": built})


def valid_catalog_body(body, *, include_model=False):
    fields = {'family', 'intellect', 'official_input_price', 'official_cache_price', 'official_output_price'}
    if include_model:
        fields.add('model')
    numeric = lambda value: type(value) in (int, float) and math.isfinite(value) and value >= 0
    return (
        isinstance(body, dict) and set(body) == fields
        and (not include_model or isinstance(body['model'], str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', body['model']))
        and isinstance(body['family'], str) and bool(body['family'].strip())
        and body['intellect'] in ('standard', 'smart', 'expert')
        and all(numeric(body[name]) for name in ('official_input_price', 'official_cache_price', 'official_output_price'))
    )


async def create_catalog(request):
    body = await request.json()
    if not valid_catalog_body(body, include_model=True):
        return web.json_response({'error': 'invalid model catalog entry'}, status=400)
    from .catalog import canonicalize
    model = canonicalize(body['model'])
    entry = body | {'model': model}
    if not request.app['store'].create_catalog(model, entry):
        return web.json_response({'error': 'model already exists'}, status=409)
    return web.json_response({'model': model}, status=201)


async def update_catalog(request):
    body = await request.json()
    if not valid_catalog_body(body):
        return web.json_response({'error': 'invalid model catalog entry'}, status=400)
    from .catalog import canonicalize
    model = canonicalize(request.match_info['model'])
    if not request.app['store'].update_catalog(model, body):
        return web.json_response({'error': 'model not found'}, status=404)
    return web.json_response({'model': model, 'updated': True})


async def delete_catalog(request):
    from .catalog import canonicalize
    model = canonicalize(request.match_info['model'])
    if not request.app['store'].delete_catalog(model):
        return web.json_response({'error': 'model not found'}, status=404)
    return web.Response(status=204)


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
    return web.FileResponse(WEB_ROOT / "index.html")


async def health(request):
    return web.json_response({"status": "ok"})


def create_app(settings: Settings):
    app = web.Application(middlewares=[auth()])
    app["settings"] = settings
    app["store"] = Store(settings.database_path, settings.key_bytes())
    app.router.add_static("/static/", WEB_ROOT)
    app.add_routes([
        web.get("/", home), web.get("/healthz", health),
        web.post("/v1/generate", generate), web.post("/v1/generate/stream", stream), web.post("/admin/v1/sync", sync),
        web.get("/admin/v1/inventory", inventory), web.get("/admin/v1/providers", providers), web.get("/admin/v1/summary", summary),
        web.get("/admin/v1/quality", quality), web.get("/admin/v1/calls", calls), web.get("/admin/v1/catalog", catalog),
        web.post("/admin/v1/catalog", create_catalog),
        web.put("/admin/v1/catalog/{model}", update_catalog), web.patch("/admin/v1/catalog/{model}", update_catalog), web.delete("/admin/v1/catalog/{model}", delete_catalog), web.put("/admin/v1/policy/{fingerprint}", update_policy),
        web.patch("/admin/v1/policy/{fingerprint}", update_policy),
    ])
    return app


def main():
    settings = Settings.from_env()
    web.run_app(create_app(settings), host="192.168.50.2", port=8817)
