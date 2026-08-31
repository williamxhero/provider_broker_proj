import asyncio
import hashlib
import json
import random
import time
import uuid

from aiohttp import ClientConnectionError, ClientError, ClientSession, ClientTimeout
from jsonschema import Draft202012Validator, SchemaError, ValidationError

from .catalog import CATALOG, canonicalize


class AttemptFailure(Exception):
    def __init__(self, status: str, *, diagnostic: dict | None = None):
        super().__init__(status)
        self.status = status
        self.diagnostic = sanitize_diagnostic(diagnostic or {})


class UpstreamFailure(Exception):
    def __init__(self, attempts: list[dict]):
        super().__init__("all eligible providers failed")
        self.attempts = attempts


DIAGNOSTIC_FIELDS = {
    "endpoint", "http_status", "content_type", "schema_hash", "output_token_limit",
    "event_types", "finish_reason", "stream_completed", "received_bytes", "ttfb_ms",
    "ttft_ms", "first_event_timeout_ms", "structured_error_kind", "validator",
    "validation_path",
}


def sanitize_diagnostic(value: dict) -> dict:
    """Allowlist bounded transport facts; never return prompts, bodies, URLs, or headers."""
    result = {}
    for key in DIAGNOSTIC_FIELDS:
        item = value.get(key)
        if isinstance(item, str):
            result[key] = item[:160]
        elif isinstance(item, (int, float, bool)) or item is None:
            result[key] = item
        elif isinstance(item, list):
            result[key] = [str(part)[:80] for part in item[:12]]
    return result


def api_url(base_url: str, suffix: str) -> str:
    base = base_url.rstrip("/")
    return base + suffix if base.endswith("/v1") else base + "/v1" + suffix


def provider_headers(provider) -> dict[str, str]:
    return {str(name): str(value) for name, value in provider.request_headers.items()} | {
        "Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json",
    }


def extract_text(data: dict) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    chunks = []
    for item in data.get("output", []) if isinstance(data.get("output"), list) else []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content", []) if isinstance(item.get("content"), list) else []:
            if isinstance(part, dict) and part.get("type") in ("output_text", "text") and isinstance(part.get("text"), str):
                chunks.append(part["text"])
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        content = (choices[0].get("message") or {}).get("content")
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            chunks.extend(part.get("text", "") for part in content if isinstance(part, dict) and isinstance(part.get("text"), str))
    content = data.get("content")
    if isinstance(content, list):
        chunks.extend(part.get("text", "") for part in content if isinstance(part, dict) and isinstance(part.get("text"), str))
    return "".join(chunks)


def normalize_usage(data: dict) -> dict:
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    normalized = dict(usage)
    if "input_tokens" not in normalized and isinstance(usage.get("prompt_tokens"), int):
        normalized["input_tokens"] = usage["prompt_tokens"]
    if "output_tokens" not in normalized and isinstance(usage.get("completion_tokens"), int):
        normalized["output_tokens"] = usage["completion_tokens"]
    return normalized


def estimate_cost(model: str, usage: dict, multiplier: float, pricing=None) -> float | None:
    pricing = pricing or CATALOG.get(canonicalize(model))
    input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
    if pricing is None or not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return None
    details = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else {}
    cached = details.get("cached_tokens", 0) if isinstance(details.get("cached_tokens", 0), int) else 0
    uncached = max(0, input_tokens - cached)
    cost = (uncached * pricing["official_input_price"] + cached * pricing["official_cache_price"] + output_tokens * pricing["official_output_price"]) / 1_000_000
    return round(cost * multiplier, 10)


def structured_schema(body: dict) -> dict | None:
    if isinstance(body.get("output_schema"), dict):
        return body["output_schema"]
    prompt = body.get("prompt")
    if not isinstance(prompt, str):
        return None
    try:
        envelope = json.loads(prompt)
    except json.JSONDecodeError:
        return None
    schema = envelope.get("output_schema") if isinstance(envelope, dict) else None
    return schema if isinstance(schema, dict) else None


def schema_hash(schema: dict | None) -> str | None:
    if schema is None:
        return None
    encoded = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def validate_structured_output(text: str, schema: dict, finish_reason: str | None, diagnostic: dict):
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise AttemptFailure("structured_schema_invalid", diagnostic=diagnostic | {
            "structured_error_kind": "schema_definition", "validator": str(exc.validator),
            "validation_path": [str(part) for part in list(exc.absolute_path)[:8]],
        }) from exc
    if finish_reason in {"length", "max_tokens", "max_output_tokens", "response.incomplete"}:
        raise AttemptFailure("output_truncated", diagnostic=diagnostic | {"structured_error_kind": "truncated"})
    try:
        parsed = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise AttemptFailure("structured_output_invalid", diagnostic=diagnostic | {"structured_error_kind": "json_decode"}) from exc
    try:
        Draft202012Validator(schema).validate(parsed)
    except ValidationError as exc:
        raise AttemptFailure("structured_output_invalid", diagnostic=diagnostic | {
            "structured_error_kind": "schema_validation", "validator": str(exc.validator),
            "validation_path": [str(part) for part in list(exc.absolute_path)[:8]],
        }) from exc


async def invoke_stream(provider, body: dict) -> dict:
    model = canonicalize(provider.models[0])
    schema = structured_schema(body)
    effort = body.get("effort")
    if provider.provider_type in ("anthropic", "claude"):
        payload = {"model": model, "max_tokens": body.get("output_token_limit", 1024), "messages": [{"role": "user", "content": body["prompt"]}], "stream": True}
        if schema is not None:
            payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "broker_output", "strict": True, "schema": schema}}
        endpoint = "/chat/completions"
    else:
        payload = {"model": model, "input": body["prompt"], "max_output_tokens": body.get("output_token_limit", 1024), "stream": True}
        if effort:
            payload["reasoning"] = {"effort": effort}
        if schema is not None:
            payload["text"] = {"format": {"type": "json_schema", "name": "broker_output", "strict": True, "schema": schema}}
        endpoint = "/responses"
    started = time.monotonic()
    route_deadline = float(body.get("_route_deadline", started + max(.001, body.get("deadline_ms", 60000) / 1000)))
    first_event_timeout = max(.001, float(body.get("_first_event_timeout_ms", 20000)) / 1000)
    first_event_deadline = min(route_deadline, started + first_event_timeout)
    metadata, chunks, event_types = {}, [], []
    completed = False
    finish_reason = None
    received_bytes = 0
    ttfb_ms = ttft_ms = None
    response_status = None
    content_type = None

    def diagnostic():
        return sanitize_diagnostic({
            "endpoint": endpoint, "http_status": response_status, "content_type": content_type,
            "schema_hash": schema_hash(schema), "output_token_limit": body.get("output_token_limit", 1024),
            "event_types": event_types, "finish_reason": finish_reason, "stream_completed": completed,
            "received_bytes": received_bytes, "ttfb_ms": ttfb_ms, "ttft_ms": ttft_ms,
            "first_event_timeout_ms": round(first_event_timeout * 1000),
        })

    def consume_event(event):
        nonlocal completed, finish_reason, ttft_ms
        if not isinstance(event, dict):
            return
        event_type = event.get("type") if isinstance(event.get("type"), str) else "chat.chunk"
        if event_type not in event_types and len(event_types) < 12:
            event_types.append(event_type)
        if event_type in ("response.completed", "response.incomplete") and isinstance(event.get("response"), dict):
            metadata.update(event["response"])
            completed = True
            details = event["response"].get("incomplete_details")
            finish_reason = details.get("reason") if isinstance(details, dict) else event_type
        else:
            metadata.update({key: event[key] for key in ("id", "model", "usage") if key in event})
        delta = event.get("delta") if isinstance(event.get("delta"), str) else None
        choices = event.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            choice = choices[0]
            choice_delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            if isinstance(choice_delta.get("content"), str):
                delta = choice_delta["content"]
            if choice.get("finish_reason") is not None:
                finish_reason = str(choice["finish_reason"])
                completed = True
        if delta:
            if not chunks:
                ttft_ms = round((time.monotonic() - started) * 1000, 2)
            chunks.append(delta)

    if schema is not None:
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise AttemptFailure("structured_schema_invalid", diagnostic=diagnostic() | {
                "structured_error_kind": "schema_definition", "validator": str(exc.validator),
                "validation_path": [str(part) for part in list(exc.absolute_path)[:8]],
            }) from exc

    timeout = ClientTimeout(total=max(.001, route_deadline - time.monotonic()))
    try:
        async with ClientSession(timeout=timeout) as session:
            try:
                response = await asyncio.wait_for(session.post(api_url(provider.base_url, endpoint), json=payload, headers=provider_headers(provider)), max(.001, first_event_deadline - time.monotonic()))
            except (asyncio.TimeoutError, TimeoutError) as exc:
                raise AttemptFailure("first_token_timeout", diagnostic=diagnostic()) from exc
            async with response:
                ttfb_ms = round((time.monotonic() - started) * 1000, 2)
                response_status, content_type = response.status, response.content_type
                if response.status >= 400:
                    await response.read()
                    raise AttemptFailure("unavailable", diagnostic=diagnostic())
                if response.content_type == "application/json":
                    try:
                        remaining = first_event_deadline - time.monotonic()
                        if remaining <= 0:
                            raise AttemptFailure("first_token_timeout", diagnostic=diagnostic())
                        data = await asyncio.wait_for(response.json(content_type=None), remaining)
                    except (asyncio.TimeoutError, TimeoutError) as exc:
                        raise AttemptFailure("first_token_timeout", diagnostic=diagnostic()) from exc
                    except AttemptFailure:
                        raise
                    except Exception as exc:
                        raise AttemptFailure("protocol_failed", diagnostic=diagnostic()) from exc
                    text = extract_text(data) if isinstance(data, dict) else ""
                    if not text.strip():
                        raise AttemptFailure("protocol_failed", diagnostic=diagnostic())
                    metadata = data
                    chunks = [text]
                    completed = True
                    ttft_ms = round((time.monotonic() - started) * 1000, 2)
                else:
                    while not completed:
                        now = time.monotonic()
                        if now >= route_deadline:
                            raise AttemptFailure("timed_out", diagnostic=diagnostic())
                        read_deadline = route_deadline if chunks else first_event_deadline
                        if now >= read_deadline:
                            raise AttemptFailure("first_token_timeout", diagnostic=diagnostic())
                        try:
                            raw = await asyncio.wait_for(response.content.readline(), read_deadline - now)
                        except (asyncio.TimeoutError, TimeoutError) as exc:
                            status = "timed_out" if chunks else "first_token_timeout"
                            raise AttemptFailure(status, diagnostic=diagnostic()) from exc
                        if not raw:
                            break
                        received_bytes += len(raw)
                        line = raw.decode("utf-8", errors="replace").strip()
                        if not line.startswith("data:"):
                            continue
                        value = line[5:].strip()
                        if value == "[DONE]":
                            completed = True
                            continue
                        try:
                            consume_event(json.loads(value))
                        except json.JSONDecodeError as exc:
                            raise AttemptFailure("protocol_failed", diagnostic=diagnostic()) from exc
    except AttemptFailure:
        raise
    except (asyncio.TimeoutError, TimeoutError) as exc:
        raise AttemptFailure("timed_out", diagnostic=diagnostic()) from exc
    except (ClientConnectionError, ClientError, OSError, ValueError) as exc:
        raise AttemptFailure("transport_failed", diagnostic=diagnostic()) from exc
    text = "".join(chunks)
    if not completed or not text.strip():
        raise AttemptFailure("stream_incomplete", diagnostic=diagnostic())
    if schema is not None:
        validate_structured_output(text, schema, finish_reason, diagnostic())
    usage = normalize_usage(metadata)
    actual_model = canonicalize(str(metadata.get("model") or model))
    return {
        "text": text, "chunks": chunks, "actual_model": actual_model,
        "latency_ms": ttft_ms, "usage": usage,
        "request_id": str(metadata.get("id") or uuid.uuid4()),
        "cost": estimate_cost(actual_model, usage, provider.multiplier, provider.pricing if actual_model == model else None),
    }


async def invoke(provider, body: dict) -> dict:
    """Non-stream callers also use upstream streaming so first-token timeouts are enforceable."""
    return await invoke_stream(provider, body)


def observe(store, provider, requested_model, tier, body, status, *, output=None):
    output = output or {}
    usage = output.get("usage") or {}
    store.observe(
        fingerprint=provider.fingerprint, requested_model=requested_model,
        actual_model=output.get("actual_model"), tier=tier, effort=body.get("effort"),
        success=int(status == "completed"), latency_ms=output.get("latency_ms"), error=None,
        status=status, input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
        cost=output.get("cost"), request_id=output.get("request_id") or str(uuid.uuid4()),
    )


class AttemptAudit:
    def __init__(self, route_started: float):
        self.route_started = route_started
        self.rows = {}

    def start(self, sequence: int, provider):
        self.rows[sequence] = {
            "attempt": sequence + 1, "provider": provider.name,
            "model": canonicalize(provider.models[0]),
            "started_ms": round((time.monotonic() - self.route_started) * 1000, 2),
            "_started": time.monotonic(),
        }

    def finish(self, sequence: int, status: str, *, output=None, diagnostic=None, fulfilled=None):
        row = self.rows[sequence]
        row["status"] = status
        row["elapsed_ms"] = round((time.monotonic() - row.pop("_started")) * 1000, 2)
        if output and output.get("actual_model"):
            row["actual_model"] = output["actual_model"]
        if fulfilled is not None:
            row["fulfilled"] = fulfilled
        safe = sanitize_diagnostic(diagnostic or {})
        if safe:
            row["diagnostic"] = safe

    def public(self):
        return [dict(self.rows[index]) for index in sorted(self.rows)]


async def route(store, tier: str, body: dict, parallel_cap: int = 3, invoker=invoke,
                *, hedge_delay_ms: int = 750, first_event_timeout_ms: int = 20000,
                route_attempt_budget: int = 32) -> dict:
    route_started = time.monotonic()
    deadline_ms = body.get("deadline_ms", 60000)
    deadline_seconds = max(.001, float(deadline_ms) / 1000) if isinstance(deadline_ms, (int, float)) else 60
    route_deadline = route_started + deadline_seconds
    attempt_budget = max(1, int(route_attempt_budget))
    cap = max(1, int(parallel_cap))
    audit = AttemptAudit(route_started)
    attempts_started = 0
    invocation_body = {**body, "_route_deadline": route_deadline, "_first_event_timeout_ms": max(1, int(first_event_timeout_ms))}
    tiers = ("standard", "smart", "expert")

    async def race_band(providers, candidate_tier):
        nonlocal attempts_started
        queue = random.sample(providers, k=len(providers))
        active = {}
        next_candidate = 0
        next_hedge_at = time.monotonic()

        def launch_one():
            nonlocal attempts_started, next_candidate, next_hedge_at
            while next_candidate < len(queue) and attempts_started < attempt_budget and time.monotonic() < route_deadline:
                provider = queue[next_candidate]
                next_candidate += 1
                if not store.try_acquire(provider):
                    continue
                sequence = attempts_started
                attempts_started += 1
                audit.start(sequence, provider)

                async def run():
                    try:
                        return await invoker(provider, invocation_body)
                    finally:
                        store.release(provider)

                active[asyncio.create_task(run())] = (sequence, provider)
                next_hedge_at = time.monotonic() + max(0, hedge_delay_ms) / 1000
                return True
            return False

        launch_one()
        try:
            while active:
                now = time.monotonic()
                if now >= route_deadline:
                    for task, (sequence, provider) in active.items():
                        task.cancel()
                        observe(store, provider, canonicalize(provider.models[0]), candidate_tier, body, "timed_out")
                        audit.finish(sequence, "timed_out", diagnostic={"first_event_timeout_ms": first_event_timeout_ms})
                    await asyncio.gather(*active, return_exceptions=True)
                    active.clear()
                    return None
                can_hedge = next_candidate < len(queue) and attempts_started < attempt_budget and len(active) < cap
                timeout = min(route_deadline - now, max(0, next_hedge_at - now)) if can_hedge else route_deadline - now
                done, _ = await asyncio.wait(active, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    launch_one()
                    continue
                for task in sorted(done, key=lambda item: active[item][0]):
                    sequence, provider = active.pop(task)
                    requested_model = canonicalize(provider.models[0])
                    try:
                        output = task.result()
                    except AttemptFailure as exc:
                        observe(store, provider, requested_model, candidate_tier, body, exc.status)
                        audit.finish(sequence, exc.status, diagnostic=exc.diagnostic)
                        launch_one()
                        continue
                    if output["actual_model"] != requested_model:
                        observe(store, provider, requested_model, candidate_tier, body, "completed", output=output)
                        store.block_route(provider.fingerprint, requested_model)
                        audit.finish(sequence, "completed", output=output, fulfilled=False)
                        launch_one()
                        continue
                    observe(store, provider, requested_model, candidate_tier, body, "completed", output=output)
                    audit.finish(sequence, "completed", output=output, fulfilled=True)
                    for pending, (pending_sequence, pending_provider) in active.items():
                        pending.cancel()
                        observe(store, pending_provider, canonicalize(pending_provider.models[0]), candidate_tier, body, "cancelled")
                        audit.finish(pending_sequence, "cancelled")
                    await asyncio.gather(*active, return_exceptions=True)
                    active.clear()
                    return output | {
                        "provider": provider.name, "attempts": audit.public(),
                        "fulfilled_intellect": candidate_tier, "fingerprint": provider.fingerprint,
                    }
                while len(active) < cap and next_candidate < len(queue) and attempts_started < attempt_budget and time.monotonic() >= next_hedge_at:
                    if not launch_one():
                        break
        except asyncio.CancelledError:
            for task, (sequence, provider) in active.items():
                task.cancel()
                observe(store, provider, canonicalize(provider.models[0]), candidate_tier, body, "cancelled")
                audit.finish(sequence, "cancelled")
            await asyncio.gather(*active, return_exceptions=True)
            active.clear()
            raise
        return None

    for candidate_tier in tiers[tiers.index(tier):]:
        for providers in price_bands(store.providers(candidate_tier)):
            if attempts_started >= attempt_budget or time.monotonic() >= route_deadline:
                raise UpstreamFailure(audit.public())
            available = [provider for provider in providers if store.has_capacity(provider)]
            if not available:
                continue
            result = await race_band(available, candidate_tier)
            if result is not None:
                return result
    raise UpstreamFailure(audit.public())


def price_bands(providers):
    """Split all Key prices at their median, ordered from lower to higher price."""
    ordered = sorted(providers, key=lambda provider: (provider.price_group, provider.id))
    if not ordered:
        return []
    midpoint = len(ordered) // 2
    median = ordered[midpoint].price_group if len(ordered) % 2 else (ordered[midpoint - 1].price_group + ordered[midpoint].price_group) / 2
    lower = [provider for provider in ordered if provider.price_group <= median]
    higher = [provider for provider in ordered if provider.price_group > median]
    return [lower] + ([higher] if higher else [])
