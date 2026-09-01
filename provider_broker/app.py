import json
import math
import re
import asyncio
from datetime import UTC, datetime
from pathlib import Path

from aiohttp import web

from .db import Store
from .catalog import blended_price
from .settings import Settings
from .source import sync_cpa
from .upstream import UpstreamFailure, invoke_stream, route
from .health import run_probe, scheduler
from .balances import BalanceFailure, login as balance_login, notify_low_balance, scheduler as balance_scheduler, sync_one as sync_balance

WEB_ROOT = Path(__file__).with_name("web")


async def generate(request):
    body = await request.json()
    if "model" in body or not isinstance(body.get("prompt"), str):
        return web.json_response({"error": "prompt and intellect are required; model is not a capability selector"}, status=400)
    tier = body.get("intellect")
    if tier not in ("standard", "smart", "expert"):
        return web.json_response({"error": "model must be standard, smart, or expert"}, status=400)
    try:
        # All production attempts are streaming internally.  The non-stream API only
        # buffers the winning stream before serialising its completed response.
        result = await route(request.app["store"], tier, body, request.app["store"].race_parallel_cap(), request.app["store"].hedge_delay_ms(), request.app["settings"].first_event_timeout_ms, request.app["settings"].route_attempt_budget)
    except UpstreamFailure as exc:
        return web.json_response({"error": "all eligible providers failed", "attempts": exc.attempts}, status=503)
    try:
        output = await result["attempt"].result()
    except UpstreamFailure as exc:
        return web.json_response({"error": "all eligible providers failed", "attempts": exc.attempts}, status=503)
    provider = result["attempt"].provider
    from .upstream import observe
    observe(request.app["store"], provider, result["attempt"].model, result["fulfilled_intellect"], body, "completed", output=output)
    request.app["store"].record_health(provider.fingerprint, result["attempt"].model, success=True, real=True, ttft_ms=output["latency_ms"])
    return web.json_response({
        "status": "completed", "intellect": tier, "fulfilled_intellect": result["fulfilled_intellect"],
        "effort": body.get("effort"), "deadline_ms": body.get("deadline_ms"), "output_token_limit": body.get("output_token_limit"),
        "actual_model": output["actual_model"], "output_text": output["text"], "provider": result["provider"],
        "request_id": output["request_id"], "usage": output["usage"],
        "ttft_ms": output["latency_ms"], "attempts": result["attempts"], "cost_estimate": output["cost"],
    })


async def stream(request):
    body = await request.json()
    tier = body.get("intellect")
    if "model" in body or not isinstance(body.get("prompt"), str) or tier not in ("standard", "smart", "expert"):
        return web.json_response({"error": "prompt and valid intellect are required"}, status=400)
    try:
        result = await route(request.app["store"], tier, body, request.app["store"].race_parallel_cap(), request.app["store"].hedge_delay_ms(), request.app["settings"].first_event_timeout_ms, request.app["settings"].route_attempt_budget)
    except UpstreamFailure as exc:
        return web.json_response({"error": "all eligible providers failed", "attempts": exc.attempts}, status=503)
    sse = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
    await sse.prepare(request)
    attempt = result["attempt"]
    try:
        async for chunk in attempt.iter_text():
            await sse.write(f"event: delta\ndata: {json.dumps({'text': chunk})}\n\n".encode())
        output = await attempt.result()
    except (ConnectionResetError, asyncio.CancelledError):
        await attempt.close()
        raise
    provider = attempt.provider
    from .upstream import observe
    observe(request.app["store"], provider, attempt.model, result["fulfilled_intellect"], body, "completed", output=output)
    request.app["store"].record_health(provider.fingerprint, attempt.model, success=True, real=True, ttft_ms=output["latency_ms"])
    final = {
        "status": "completed", "intellect": tier, "fulfilled_intellect": result["fulfilled_intellect"],
        "actual_model": output["actual_model"], "output_text": output["text"], "provider": result["provider"],
        "attempts": result["attempts"], "request_id": output["request_id"], "usage": output["usage"],
        "cost_estimate": output["cost"], "ttft_ms": output["latency_ms"],
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
    request.app["store"].ensure_health_targets(request.app["clock"]())
    after = {provider["fingerprint"] for provider in providers_now}
    return web.json_response({"added": len(after - before), "updated": len(after & before), "offlined": len(before - after), "inventory_failures": sync_result["inventory_failures"], "last_successful_sync": max((provider["synced_at"] for provider in providers_now), default=None)})


async def inventory(request):
    window = request.query.get("window", "24h")
    if window not in ("1h", "24h", "7d", "30d"):
        return web.json_response({"error": "invalid window"}, status=400)
    return web.json_response({"providers": request.app["store"].inventory(window)})


async def providers(request):
    window = request.query.get("window", "24h")
    if window not in ("1h", "24h", "7d", "30d"):
        return web.json_response({"error": "invalid window"}, status=400)
    return web.json_response({"providers": request.app["store"].inventory(window)})


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
    window = request.query.get("window", "24h")
    if window not in ("1h", "24h", "7d", "30d"):
        return web.json_response({"error": "invalid window"}, status=400)
    sort, _, direction = request.query.get("sort", "time:desc").partition(":")
    if sort not in ("time", "note", "provider", "requested_model", "actual_model", "intellect", "effort", "ttft", "status", "input_tokens", "output_tokens", "cost", "request_id") or direction not in ("asc", "desc"):
        return web.json_response({"error": "invalid sort"}, status=400)
    cursor = request.query.get("cursor")
    default_order = sort == "time" and direction == "desc"
    if default_order:
        if cursor and (not cursor.isdigit() or int(cursor) < 1):
            return web.json_response({"error": "invalid cursor"}, status=400)
        offset = None
    else:
        if cursor and not re.fullmatch(r"offset-\d+", cursor):
            return web.json_response({"error": "invalid cursor"}, status=400)
        offset = int(cursor.removeprefix("offset-")) if cursor else 0
        cursor = None
    items = request.app["store"].calls(limit, cursor, request.query.get("provider"), request.query.get("status"), window, sort, direction, offset)
    next_cursor = str(items[-1]["id"]) if default_order and len(items) == limit else f"offset-{offset + len(items)}" if not default_order and len(items) == limit else None
    return web.json_response({"items": items, "next_cursor": next_cursor})


async def catalog(request):
    counts = request.app["store"].catalog_counts()
    built = {name: value | {"blended_price": blended_price(value), "available_provider_count": counts.get(name, 0)} for name, value in request.app["store"].catalog().items()}
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


async def apply_catalog(request):
    return web.json_response(request.app['store'].apply_catalog_to_inventory())


async def routing(request):
    store = request.app['store']
    if request.method == 'GET':
        return web.json_response({'race_parallel_cap': store.race_parallel_cap(), 'hedge_delay_ms': store.hedge_delay_ms()})
    body = await request.json()
    if not isinstance(body, dict) or not body or not set(body) <= {'race_parallel_cap', 'hedge_delay_ms'} or ('race_parallel_cap' in body and (type(body['race_parallel_cap']) is not int or not 1 <= body['race_parallel_cap'] <= 32)) or ('hedge_delay_ms' in body and (type(body['hedge_delay_ms']) is not int or not 0 <= body['hedge_delay_ms'] <= 10000)):
        return web.json_response({'error': 'invalid routing policy'}, status=400)
    store.update_routing(race_parallel_cap=body.get('race_parallel_cap'), hedge_delay_ms=body.get('hedge_delay_ms'))
    return web.json_response({'race_parallel_cap': store.race_parallel_cap(), 'hedge_delay_ms': store.hedge_delay_ms()})


async def update_policy(request):
    body = await request.json()
    allowed = {"note", "multiplier", "enabled", "max_parallel", "calibrated", "tiers"}
    numeric = lambda value: type(value) in (int, float) and math.isfinite(value)
    valid = (
        isinstance(body, dict) and bool(body) and set(body) <= allowed
        and ("note" not in body or isinstance(body["note"], str))
        and ("enabled" not in body or type(body["enabled"]) is bool)
        and ("calibrated" not in body or type(body["calibrated"]) is bool)
        and ("multiplier" not in body or numeric(body["multiplier"]) and body["multiplier"] > 0)
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


async def balance_sites(request):
    return web.json_response({"sites": request.app["store"].balance_sites(), "configuration": request.app["store"].balance_configuration()})


async def update_balance_site(request):
    try:
        body = await request.json()
    except Exception:
        body = None
    allowed = {"low_threshold", "enabled"}
    threshold = body.get("low_threshold") if isinstance(body, dict) else None
    enabled = body.get("enabled") if isinstance(body, dict) else None
    valid_threshold = type(threshold) in (int, float) and math.isfinite(threshold) and 0 <= threshold <= 1_000_000
    if not isinstance(body, dict) or not body or not set(body) <= allowed or ("low_threshold" in body and not valid_threshold) or ("enabled" in body and type(enabled) is not bool):
        return web.json_response({"error": "invalid balance site settings"}, status=400)
    if not request.app["store"].update_balance_site(request.match_info["site"], low_threshold=float(threshold) if "low_threshold" in body else None, enabled=enabled if "enabled" in body else None):
        return web.json_response({"error": "balance site not found"}, status=404)
    return web.json_response({"updated": True})


async def login_balance_site(request):
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict) or set(body) != {"account", "password"} or not all(isinstance(body[key], str) and body[key] for key in ("account", "password")):
        return web.json_response({"error": "account and password are required"}, status=400)
    site_id = request.match_info["site"]
    site = request.app["store"].balance_site_secret(site_id)
    if site is None:
        return web.json_response({"error": "balance site not found"}, status=404)
    try:
        balance, credential = await balance_login(site, body["account"], body["password"])
    except BalanceFailure as exc:
        request.app["store"].record_balance_error(site_id, str(exc))
        return web.json_response({"error": "site login failed", "detail": str(exc)}, status=502)
    event = request.app["store"].record_balance(site_id, balance, credential)
    if event["entered_low"]:
        await notify_low_balance(request.app["store"], event)
    return web.json_response({"logged_in": True, "balance": balance, "currency": site["currency"], "low": event["low"]})


async def sync_balance_sites(request):
    site_id = request.match_info.get("site")
    if site_id:
        result = await sync_balance(request.app["store"], site_id)
        return web.json_response(result, status=200 if result["ok"] else 502)
    sites = [site for site in request.app["store"].balance_sites() if site["enabled"] and site["configured"]]
    results = await asyncio.gather(*(sync_balance(request.app["store"], site["id"]) for site in sites))
    return web.json_response({"results": results})


async def balance_configuration(request):
    if request.method == "GET":
        return web.json_response(request.app["store"].balance_configuration())
    try:
        body = await request.json()
    except Exception:
        body = None
    webhook = body.get("webhook_url") if isinstance(body, dict) else None
    if not isinstance(body, dict) or set(body) != {"webhook_url"} or (webhook is not None and (not isinstance(webhook, str) or len(webhook) > 2048 or (webhook and not webhook.startswith("https://")))):
        return web.json_response({"error": "webhook_url must be an HTTPS URL or null"}, status=400)
    request.app["store"].update_balance_webhook(webhook.strip() if webhook else None)
    return web.json_response(request.app["store"].balance_configuration())


@web.middleware
async def json_contract(request, handler):
    """Keep malformed client JSON inside the Broker's structured error contract."""
    try:
        return await handler(request)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "request body must be valid JSON"}, status=400)


async def health_results(request):
    tier = request.query.get("stage")
    if tier not in ("standard", "smart", "expert"):
        return web.json_response({"error": "invalid stage"}, status=400)
    return web.json_response({"items": request.app["store"].health_results(tier, request.query.get("fingerprint"), request.query.get("model"))})


async def probe(request):
    try:
        body = await request.json()
    except Exception:
        body = None
    allowed = {"stage", "mode", "fingerprint", "model", "timeout_ms", "concurrency", "contract", "record"}
    if not isinstance(body, dict) or not set(body) <= allowed or body.get("stage") not in ("standard", "smart", "expert") or body.get("mode") not in ("race", "all"):
        return web.json_response({"error": "invalid probe request"}, status=400)
    timeout_ms = body.get("timeout_ms", request.app["settings"].probe_timeout_ms)
    concurrency = body.get("concurrency", request.app["settings"].probe_concurrency)
    contract = body.get("contract", "structured")
    record = body.get("record", True)
    if type(timeout_ms) is not int or not 100 <= timeout_ms <= 120_000 or type(concurrency) is not int or not 1 <= concurrency <= 32 or contract not in {"plain", "structured"} or type(record) is not bool:
        return web.json_response({"error": "invalid probe request"}, status=400)
    results = await run_probe(request.app["store"], tier=body["stage"], mode=body["mode"], fingerprint=body.get("fingerprint"),
                              model=body.get("model"), timeout_ms=timeout_ms, concurrency=concurrency, contract=contract, record=record,
                              clock=request.app["clock"])
    return web.json_response({"items": results})


def create_app(settings: Settings, *, clock=None):
    app = web.Application(middlewares=[json_contract])
    app["settings"] = settings
    app["store"] = Store(settings.database_path, settings.key_bytes(), settings.parallel_cap)
    app["clock"] = clock or (lambda: datetime.now(UTC))

    async def start_scheduler(app):
        app["store"].ensure_health_targets(app["clock"]())
        app["health_scheduler"] = asyncio.create_task(scheduler(app))
        app["balance_scheduler"] = asyncio.create_task(balance_scheduler(app))

    async def stop_scheduler(app):
        task = app.get("health_scheduler")
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        task = app.get("balance_scheduler")
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    app.on_startup.append(start_scheduler)
    app.on_cleanup.append(stop_scheduler)
    app.router.add_static("/static/", WEB_ROOT)
    app.add_routes([
        web.get("/", home), web.get("/healthz", health),
        web.get("/admin/v1/balances", balance_sites), web.post("/admin/v1/balances/sync", sync_balance_sites),
        web.patch("/admin/v1/balances/configuration", balance_configuration), web.get("/admin/v1/balances/configuration", balance_configuration),
        web.patch("/admin/v1/balances/{site}", update_balance_site), web.post("/admin/v1/balances/{site}/login", login_balance_site), web.post("/admin/v1/balances/{site}/sync", sync_balance_sites),
        web.post("/v1/generate", generate), web.post("/v1/generate/stream", stream), web.post("/admin/v1/sync", sync),
        web.get("/admin/v1/inventory", inventory), web.get("/admin/v1/providers", providers), web.get("/admin/v1/summary", summary),
        web.get("/admin/v1/quality", quality), web.get("/admin/v1/calls", calls), web.get("/admin/v1/catalog", catalog), web.get("/admin/v1/routing", routing), web.patch("/admin/v1/routing", routing),
        web.post("/admin/v1/catalog", create_catalog), web.post("/admin/v1/catalog/apply", apply_catalog),
        web.put("/admin/v1/catalog/{model}", update_catalog), web.patch("/admin/v1/catalog/{model}", update_catalog), web.delete("/admin/v1/catalog/{model}", delete_catalog), web.put("/admin/v1/policy/{fingerprint}", update_policy),
        web.patch("/admin/v1/policy/{fingerprint}", update_policy),
        web.get("/admin/v1/health", health_results), web.post("/admin/v1/probes", probe),
    ])
    return app


def main():
    settings = Settings.from_env()
    web.run_app(create_app(settings), host="192.168.50.2", port=8817)
