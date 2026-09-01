"""Adaptive, isolated Provider health probes.

This module intentionally knows nothing about ordinary observations.  It records
only immutable probe evidence and the small per Provider/model circuit state.
"""
import asyncio
import json
import random
from datetime import UTC, datetime

from .upstream import AttemptFailure, invoke_stream as _invoke_stream, model_fulfills, price_bands

PROBE_SCHEMA = {
    "type": "object",
    "required": ["healthy"],
    "properties": {"healthy": {"type": "boolean"}},
    "additionalProperties": False,
}


def structured_probe_prompt() -> str:
    """Use the same schema envelope as desktop cognition, not a token ping."""
    return json.dumps({
        "instruction": "Return only one JSON object matching output_schema. Do not add prose or Markdown fences.",
        "output_schema": PROBE_SCHEMA,
        "input": {"probe": "provider-broker-contract"},
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


async def invoke_stream(provider, body: dict) -> dict:
    """Make every health probe prove the structured-output contract."""
    if body.get("probe_contract") == "structured":
        body = {**body, "prompt": structured_probe_prompt(), "output_token_limit": 32}
    return await _invoke_stream(provider, body)


def sanitize_error(value: object) -> str | None:
    """Errors are classification labels, never upstream bodies or credentials."""
    allowed = {"unavailable", "http_failure", "transport_failed", "timed_out", "first_token_timeout",
               "protocol_failed", "stream_incomplete", "structured_output_invalid", "structured_schema_invalid", "output_truncated", "no_text_token", "model_mismatch", "capacity_reached",
               "inventory_unavailable"}
    text = str(value or "")
    return text if text in allowed else "protocol_failed"


async def run_probe(store, *, tier: str, mode: str, fingerprint: str | None = None,
                    model: str | None = None, timeout_ms: int = 15_000,
                    concurrency: int = 2, contract: str = "structured", record: bool = True,
                    clock=None) -> list[dict]:
    """Probe selected inventory targets with the exact streaming transport used by routing."""
    if tier not in ("standard", "smart", "expert") or mode not in ("race", "all"):
        raise ValueError("invalid probe request")
    if contract not in ("plain", "structured"):
        raise ValueError("invalid probe contract")
    now = (clock or (lambda: datetime.now(UTC)))()
    candidates = store.providers(tier) if mode == "race" else []
    if mode == "all":
        # A filtered manual probe must always reach its explicit target, including
        # an open circuit. Due-target preselection can otherwise select another
        # row and leave the requested target with an empty result.
        if fingerprint and model:
            candidates = [store.probe_provider(fingerprint, model)]
        else:
            rows = store.health_results(tier, fingerprint, model)
            candidates = [store.probe_provider(row["fingerprint"], row["model"]) for row in rows]
    catalog = store.catalog()
    candidates = [candidate for candidate in candidates if candidate and catalog.get(candidate.models[0], {}).get("intellect") == tier
                  and (not fingerprint or candidate.fingerprint == fingerprint) and (not model or candidate.models[0] == model)]
    if mode == "race":
        bands = price_bands(candidates)
        candidates = bands[0] if bands else []
        # Match production's uniformly sampled global race set.
        cap = min(len(candidates), max(1, store.race_parallel_cap()))
        candidates = random.sample(candidates, cap) if candidates else []
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def one(provider):
        requested_model = provider.models[0]
        started = (clock or (lambda: datetime.now(UTC)))()
        result = {"fingerprint": provider.fingerprint, "provider": provider.name, "model": requested_model,
                  "state": "failed", "error_type": None, "ttfb_ms": None, "ttft_ms": None, "duration_ms": None}
        if not store.try_acquire(provider):
            result["error_type"] = "capacity_reached"
            if record:
                store.record_probe(fingerprint=provider.fingerprint, model=requested_model, tier=tier, mode=mode,
                                   reachable=False, responded=False, first_token=False, model_matched=False,
                                   ttfb_ms=None, ttft_ms=None, duration_ms=None, error_type="capacity_reached", error="capacity_reached", now=started)
            return result
        try:
            async with semaphore:
                output = await invoke_stream(provider, {"prompt": "只输出1", "output_token_limit": 1,
                                                        "deadline_ms": timeout_ms, "effort": "low", "probe_contract": contract})
            matched = model_fulfills(requested_model, output["actual_model"])
            result.update({"state": "succeeded" if matched else "failed", "ttfb_ms": output.get("ttfb_ms"),
                           "ttft_ms": output.get("latency_ms"), "duration_ms": output.get("duration_ms"),
                           "error_type": None if matched else "model_mismatch"})
            if record:
                store.record_probe(fingerprint=provider.fingerprint, model=requested_model, tier=tier, mode=mode,
                                   reachable=True, responded=True, first_token=True, model_matched=matched,
                                   ttfb_ms=output.get("ttfb_ms"), ttft_ms=output.get("latency_ms"), duration_ms=output.get("duration_ms"),
                                   error_type=result["error_type"], error=result["error_type"], now=started)
                store.record_health(provider.fingerprint, requested_model, success=matched, real=False,
                                    ttft_ms=output.get("latency_ms"), now=started)
        except AttemptFailure as exc:
            status = sanitize_error(exc.status)
            result["error_type"] = status
            result["duration_ms"] = round(((clock or (lambda: datetime.now(UTC)))() - started).total_seconds() * 1000, 2)
            if record:
                store.record_probe(fingerprint=provider.fingerprint, model=requested_model, tier=tier, mode=mode,
                                   reachable=status not in {"transport_failed", "timed_out"}, responded=False, first_token=False,
                                   model_matched=False, ttfb_ms=None, ttft_ms=None, duration_ms=result["duration_ms"],
                                   error_type=status, error=status, now=started)
                store.record_health(provider.fingerprint, requested_model, success=False, real=False, now=started)
        finally:
            store.release(provider)
        return result

    return await asyncio.gather(*(one(candidate) for candidate in candidates))


async def scheduler(app):
    """Run only due targets; jitter prevents many keys firing at the same boundary."""
    settings = app["settings"]
    store = app["store"]
    clock = app["clock"]
    try:
        while True:
            due = store.due_health_targets(clock(), settings.health_stale_seconds)
            if due:
                # A small bounded random delay prevents synchronized bursts after restart.
                await asyncio.sleep(random.uniform(0, min(3, settings.health_scheduler_seconds / 4)))
                for fingerprint, model in due:
                    provider = store.probe_provider(fingerprint, model)
                    if provider:
                        catalog = store.catalog()[model]
                        await run_probe(store, tier=catalog["intellect"], mode="all", fingerprint=fingerprint, model=model,
                                        timeout_ms=settings.probe_timeout_ms, concurrency=settings.probe_concurrency, clock=clock)
            await asyncio.sleep(max(1, settings.health_scheduler_seconds))
    except asyncio.CancelledError:
        raise
