import asyncio
from collections import deque
import hashlib
import json
import random
import time
import uuid

from aiohttp import ClientConnectionError, ClientError, ClientSession, ClientTimeout
from jsonschema import Draft202012Validator, SchemaError, ValidationError

from .catalog import CATALOG, canonicalize


class AttemptFailure(Exception):
    def __init__(self, status: str, *, diagnostic: dict | None = None, repair_note: str | None = None):
        super().__init__(status)
        self.status = status
        self.diagnostic = sanitize_diagnostic(diagnostic or {})
        self.repair_note = repair_note


class UpstreamFailure(Exception):
    def __init__(self, attempts: list[dict]):
        super().__init__("all eligible providers failed")
        self.attempts = attempts


DIAGNOSTIC_FIELDS = {
    "endpoint", "http_status", "content_type", "schema_hash", "output_token_limit",
    "event_types", "finish_reason", "stream_completed", "received_bytes", "ttfb_ms",
    "ttft_ms", "first_event_timeout_ms", "idle_timeout_ms", "structured_error_kind", "validator",
    "validation_path", "attempt_timeout_ms", "progress_event_count", "max_event_gap_ms",
    "output_chars", "repair_retry", "unexpected_properties",
    "normalized_properties",
}

RETRYABLE_ATTEMPT_STATUSES = {
    "first_token_timeout", "stream_incomplete", "transport_failed",
    "protocol_failed", "timed_out", "structured_output_invalid",
    "output_truncated",
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


def strict_schema_prompt(prompt: str, schema: dict | None, repair_note: str | None = None) -> str:
    """Reinforce strictness for OpenAI-compatible gateways that only partially honor response_format."""
    if schema is None:
        return prompt
    reinforced = prompt + (
        "\n\n[Provider Broker structured-output contract]\n"
        "Return only the JSON value required by the supplied JSON Schema. "
        "Use exactly the declared object properties at every nesting level; do not add metadata, "
        "explanations, labels, identifiers, or any property absent from the schema. "
        "The response will be rejected unless it validates without repair."
    )
    if repair_note:
        reinforced += "\nA prior attempt was rejected. " + repair_note + " Generate the entire JSON again from scratch."
    return reinforced


def validate_structured_output(text: str, schema: dict, finish_reason: str | None, diagnostic: dict,
                               *, normalize_additional: bool = False) -> tuple[str, list[str]]:
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
    validator = Draft202012Validator(schema)
    normalized = []
    while True:
        errors = list(validator.iter_errors(parsed))
        if not errors:
            rendered = json.dumps(parsed, ensure_ascii=False, separators=(",", ":")) if normalized else text
            return rendered, normalized
        exc = errors[0]
        path = [str(part) for part in list(exc.absolute_path)[:8]]
        unexpected = []
        if exc.validator == "additionalProperties" and isinstance(exc.instance, dict) and isinstance(exc.schema, dict):
            declared = exc.schema.get("properties")
            if isinstance(declared, dict):
                unexpected = sorted(str(key) for key in set(exc.instance) - set(declared))[:12]
        if normalize_additional and unexpected and len(normalized) + len(unexpected) <= 32:
            for key in unexpected:
                exc.instance.pop(key, None)
                normalized.append((".".join(path) + "." if path else "") + key)
            continue
        location = ".".join(path) or "the root object"
        repair_note = None
        if unexpected:
            repair_note = f"At {location}, omit undeclared properties: {', '.join(unexpected)}."
        raise AttemptFailure("structured_output_invalid", diagnostic=diagnostic | {
            "structured_error_kind": "schema_validation", "validator": str(exc.validator),
            "validation_path": path, "unexpected_properties": unexpected,
        }, repair_note=repair_note) from exc


async def invoke_stream(provider, body: dict) -> dict:
    model = canonicalize(provider.models[0])
    schema = structured_schema(body)
    effort = body.get("effort")
    repair_note = body.get("_structured_repair_note") if isinstance(body.get("_structured_repair_note"), str) else None
    provider_prompt = strict_schema_prompt(body["prompt"], schema, repair_note)
    if provider.provider_type in ("anthropic", "claude"):
        payload = {"model": model, "max_tokens": body.get("output_token_limit", 1024), "messages": [{"role": "user", "content": provider_prompt}], "stream": True}
        if schema is not None:
            payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "broker_output", "strict": True, "schema": schema}}
        endpoint = "/chat/completions"
    else:
        payload = {"model": model, "input": provider_prompt, "max_output_tokens": body.get("output_token_limit", 1024), "stream": True}
        if effort:
            payload["reasoning"] = {"effort": effort}
        if schema is not None:
            payload["text"] = {"format": {"type": "json_schema", "name": "broker_output", "strict": True, "schema": schema}}
        endpoint = "/responses"
    started = time.monotonic()
    route_deadline = float(body.get("_route_deadline", started + max(.001, body.get("deadline_ms", 60000) / 1000)))
    first_event_timeout = max(.001, float(body.get("_first_event_timeout_ms", 20000)) / 1000)
    stream_idle_timeout = max(.001, float(body.get("_stream_idle_timeout_ms", 60000)) / 1000)
    attempt_timeout = max(.001, float(body.get("_attempt_timeout_ms", 120000)) / 1000)
    attempt_deadline = min(route_deadline, started + attempt_timeout)
    first_event_deadline = min(attempt_deadline, started + first_event_timeout)
    metadata, chunks, event_types = {}, [], []
    completed = False
    finish_reason = None
    received_bytes = 0
    ttfb_ms = ttft_ms = None
    last_progress_at = started
    progress_event_count = 0
    max_event_gap_ms = 0.0
    saw_progress = False
    response_status = None
    content_type = None
    normalized_properties = []

    def diagnostic():
        return sanitize_diagnostic({
            "endpoint": endpoint, "http_status": response_status, "content_type": content_type,
            "schema_hash": schema_hash(schema), "output_token_limit": body.get("output_token_limit", 1024),
            "event_types": event_types, "finish_reason": finish_reason, "stream_completed": completed,
            "received_bytes": received_bytes, "ttfb_ms": ttfb_ms, "ttft_ms": ttft_ms,
            "first_event_timeout_ms": round(first_event_timeout * 1000),
            "idle_timeout_ms": round(stream_idle_timeout * 1000),
            "attempt_timeout_ms": round(attempt_timeout * 1000),
            "progress_event_count": progress_event_count,
            "max_event_gap_ms": round(max_event_gap_ms, 2),
            "output_chars": sum(len(chunk) for chunk in chunks),
            "repair_retry": bool(repair_note),
            "normalized_properties": normalized_properties,
        })

    def record_progress():
        nonlocal last_progress_at, progress_event_count, max_event_gap_ms, saw_progress
        now = time.monotonic()
        max_event_gap_ms = max(max_event_gap_ms, (now - last_progress_at) * 1000)
        last_progress_at = now
        progress_event_count += 1
        saw_progress = True

    def consume_event(event):
        nonlocal completed, finish_reason, ttft_ms
        if not isinstance(event, dict):
            return False
        event_type = event.get("type") if isinstance(event.get("type"), str) else "chat.chunk"
        if event_type not in event_types and len(event_types) < 12:
            event_types.append(event_type)
        recognized = event_type.startswith("response.") or event_type in {
            "chat.chunk", "message_start", "content_block_start", "content_block_delta",
            "message_delta", "message_stop",
        }
        output_delta = None
        if event_type in ("response.completed", "response.incomplete") and isinstance(event.get("response"), dict):
            metadata.update(event["response"])
            completed = True
            details = event["response"].get("incomplete_details")
            finish_reason = details.get("reason") if isinstance(details, dict) else event_type
        else:
            metadata.update({key: event[key] for key in ("id", "model", "usage") if key in event})
        if event_type == "response.output_text.delta" and isinstance(event.get("delta"), str):
            output_delta = event["delta"]
        elif event_type == "response.output_text.done" and not chunks and isinstance(event.get("text"), str):
            output_delta = event["text"]
        elif event_type == "content_block_start":
            block = event.get("content_block")
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                output_delta = block["text"]
        elif event_type == "content_block_delta":
            delta = event.get("delta")
            if isinstance(delta, dict) and isinstance(delta.get("text"), str):
                output_delta = delta["text"]
        elif event_type == "message_delta":
            delta = event.get("delta")
            if isinstance(delta, dict) and delta.get("stop_reason") is not None:
                finish_reason = str(delta["stop_reason"])
        elif event_type == "message_stop":
            completed = True
        choices = event.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            recognized = True
            choice = choices[0]
            choice_delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            if isinstance(choice_delta.get("content"), str):
                output_delta = choice_delta["content"]
            if choice.get("finish_reason") is not None:
                finish_reason = str(choice["finish_reason"])
                completed = True
        elif event_type == "chat.chunk" and isinstance(event.get("delta"), str):
            output_delta = event["delta"]
        if output_delta:
            if not chunks:
                ttft_ms = round((time.monotonic() - started) * 1000, 2)
            chunks.append(output_delta)
        return recognized

    if schema is not None:
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise AttemptFailure("structured_schema_invalid", diagnostic=diagnostic() | {
                "structured_error_kind": "schema_definition", "validator": str(exc.validator),
                "validation_path": [str(part) for part in list(exc.absolute_path)[:8]],
            }) from exc

    timeout = ClientTimeout(total=max(.001, attempt_deadline - time.monotonic()))
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
                        remaining = attempt_deadline - time.monotonic()
                        if remaining <= 0:
                            raise AttemptFailure("timed_out", diagnostic=diagnostic())
                        data = await asyncio.wait_for(response.json(content_type=None), remaining)
                    except (asyncio.TimeoutError, TimeoutError) as exc:
                        raise AttemptFailure("timed_out", diagnostic=diagnostic()) from exc
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
                    record_progress()
                else:
                    sse_event_type = None
                    while not completed:
                        now = time.monotonic()
                        if now >= attempt_deadline:
                            raise AttemptFailure("timed_out", diagnostic=diagnostic())
                        read_deadline = min(attempt_deadline, last_progress_at + stream_idle_timeout) if saw_progress else first_event_deadline
                        if now >= read_deadline:
                            status = "timed_out" if read_deadline >= attempt_deadline else "stream_incomplete" if saw_progress else "first_token_timeout"
                            raise AttemptFailure(status, diagnostic=diagnostic())
                        try:
                            raw = await asyncio.wait_for(response.content.readline(), read_deadline - now)
                        except (asyncio.TimeoutError, TimeoutError) as exc:
                            status = "timed_out" if read_deadline >= attempt_deadline else "stream_incomplete" if saw_progress else "first_token_timeout"
                            raise AttemptFailure(status, diagnostic=diagnostic()) from exc
                        if not raw:
                            break
                        received_bytes += len(raw)
                        line = raw.decode("utf-8", errors="replace").strip()
                        if line.startswith("event:"):
                            sse_event_type = line[6:].strip() or None
                            continue
                        if not line.startswith("data:"):
                            continue
                        value = line[5:].strip()
                        if value == "[DONE]":
                            completed = True
                            record_progress()
                            continue
                        try:
                            event = json.loads(value)
                            if isinstance(event, dict) and sse_event_type and not isinstance(event.get("type"), str):
                                event = {**event, "type": sse_event_type}
                            sse_event_type = None
                            if consume_event(event):
                                record_progress()
                        except json.JSONDecodeError as exc:
                            raise AttemptFailure("protocol_failed", diagnostic=diagnostic()) from exc
    except AttemptFailure:
        raise
    except (asyncio.TimeoutError, TimeoutError) as exc:
        raise AttemptFailure("timed_out", diagnostic=diagnostic()) from exc
    except (ClientConnectionError, ClientError, OSError, ValueError) as exc:
        raise AttemptFailure("transport_failed", diagnostic=diagnostic()) from exc
    if completed and not chunks:
        buffered = extract_text(metadata)
        if buffered.strip():
            chunks.append(buffered)
            ttft_ms = ttft_ms or round((time.monotonic() - started) * 1000, 2)
    text = "".join(chunks)
    if not completed or not text.strip():
        raise AttemptFailure("stream_incomplete", diagnostic=diagnostic())
    if schema is not None:
        text, normalized_properties = validate_structured_output(
            text, schema, finish_reason, diagnostic(), normalize_additional=bool(repair_note),
        )
        if normalized_properties:
            chunks = [text]
    usage = normalize_usage(metadata)
    actual_model = canonicalize(str(metadata.get("model") or model))
    return {
        "text": text, "chunks": chunks, "actual_model": actual_model,
        "latency_ms": ttft_ms, "usage": usage,
        "request_id": str(metadata.get("id") or uuid.uuid4()),
        "cost": estimate_cost(actual_model, usage, provider.multiplier, provider.pricing if actual_model == model else None),
        "diagnostic": diagnostic(),
    }


async def invoke(provider, body: dict) -> dict:
    """Non-stream callers also use upstream streaming so first-token timeouts are enforceable."""
    return await invoke_stream(provider, body)


def observe(store, provider, requested_model, tier, body, status, *, output=None, attempt=None, route_id=None):
    output = output or {}
    attempt = attempt or {}
    usage = output.get("usage") or {}
    store.observe(
        fingerprint=provider.fingerprint, requested_model=requested_model,
        actual_model=output.get("actual_model"), tier=tier, effort=body.get("effort"),
        success=int(status == "completed"), latency_ms=output.get("latency_ms"), error=None,
        status=status, input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
        cost=output.get("cost"), request_id=output.get("request_id") or str(uuid.uuid4()),
        diagnostic_json=json.dumps(sanitize_diagnostic(attempt.get("diagnostic") or output.get("diagnostic") or {}), sort_keys=True),
        route_id=route_id, attempt_number=attempt.get("attempt"), started_ms=attempt.get("started_ms"),
        elapsed_ms=attempt.get("elapsed_ms"),
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

    def row(self, sequence: int):
        return dict(self.rows[sequence])


def retryable_attempt(failure: AttemptFailure) -> bool:
    if failure.status in RETRYABLE_ATTEMPT_STATUSES:
        return True
    if failure.status != "unavailable":
        return False
    status = failure.diagnostic.get("http_status")
    return status is None or status in {408, 409, 425, 429} or isinstance(status, int) and status >= 500


async def route(store, tier: str, body: dict, parallel_cap: int = 3, invoker=invoke,
                *, hedge_delay_ms: int = 750, first_event_timeout_ms: int = 30000,
                stream_idle_timeout_ms: int = 90000, attempt_timeout_ms: int = 180000,
                route_attempt_budget: int = 32) -> dict:
    route_started = time.monotonic()
    deadline_ms = body.get("deadline_ms", 60000)
    deadline_seconds = max(.001, float(deadline_ms) / 1000) if isinstance(deadline_ms, (int, float)) else 60
    route_deadline = route_started + deadline_seconds
    attempt_budget = max(1, int(route_attempt_budget))
    cap = max(1, int(parallel_cap))
    audit = AttemptAudit(route_started)
    route_id = str(uuid.uuid4())
    attempts_started = 0
    effort_multiplier = {"medium": 2, "high": 3}.get(body.get("effort"), 1)
    effective_first_event_timeout_ms = max(1, int(first_event_timeout_ms) * effort_multiplier)
    invocation_body = {
        **body,
        "_route_deadline": route_deadline,
        "_first_event_timeout_ms": effective_first_event_timeout_ms,
        "_stream_idle_timeout_ms": max(1, int(stream_idle_timeout_ms)),
        "_attempt_timeout_ms": max(1, int(attempt_timeout_ms)),
    }
    tiers = ("standard", "smart", "expert")
    primary = deque()
    repair = deque()
    retry = deque()
    retries_scheduled = set()
    for candidate_tier in tiers[tiers.index(tier):]:
        for band in price_bands(store.providers(candidate_tier)):
            available = [provider for provider in band if store.has_capacity(provider)]
            primary.extend((provider, candidate_tier, None) for provider in random.sample(available, k=len(available)))

    active = {}
    next_hedge_at = route_started

    def launch_one():
        nonlocal attempts_started, next_hedge_at
        while (repair or primary or retry) and attempts_started < attempt_budget and time.monotonic() < route_deadline:
            provider, candidate_tier, repair_note = repair.popleft() if repair else primary.popleft() if primary else retry.popleft()
            if not store.try_acquire(provider):
                continue
            sequence = attempts_started
            attempts_started += 1
            audit.start(sequence, provider)

            async def run(selected=provider, selected_repair_note=repair_note):
                try:
                    selected_body = invocation_body
                    if selected_repair_note:
                        selected_body = {**invocation_body, "_structured_repair_note": selected_repair_note}
                    return await invoker(selected, selected_body)
                finally:
                    store.release(selected)

            active[asyncio.create_task(run())] = (sequence, provider, candidate_tier)
            next_hedge_at = time.monotonic() + max(0, hedge_delay_ms) / 1000
            return True
        return False

    async def cancel_active(status: str):
        for task, (sequence, provider, candidate_tier) in active.items():
            task.cancel()
            diagnostic = {"first_event_timeout_ms": effective_first_event_timeout_ms} if status == "timed_out" else None
            audit.finish(sequence, status, diagnostic=diagnostic)
            observe(
                store, provider, canonicalize(provider.models[0]), candidate_tier, body, status,
                attempt=audit.row(sequence), route_id=route_id,
            )
        await asyncio.gather(*active, return_exceptions=True)
        active.clear()

    launch_one()
    try:
        while active or repair or primary or retry:
            now = time.monotonic()
            if now >= route_deadline or attempts_started >= attempt_budget and not active:
                if active:
                    await cancel_active("timed_out")
                break
            if not active:
                if not launch_one():
                    break
                continue
            can_hedge = bool(repair or primary or retry) and attempts_started < attempt_budget and len(active) < cap
            timeout = min(route_deadline - now, max(0, next_hedge_at - now)) if can_hedge else route_deadline - now
            done, _ = await asyncio.wait(active, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                launch_one()
                continue

            winner = None
            for task in sorted(done, key=lambda item: active[item][0]):
                sequence, provider, candidate_tier = active.pop(task)
                requested_model = canonicalize(provider.models[0])
                try:
                    output = task.result()
                except AttemptFailure as exc:
                    audit.finish(sequence, exc.status, diagnostic=exc.diagnostic)
                    observe(
                        store, provider, requested_model, candidate_tier, body, exc.status,
                        attempt=audit.row(sequence), route_id=route_id,
                    )
                    retry_key = (provider.fingerprint, requested_model, candidate_tier)
                    if retryable_attempt(exc) and retry_key not in retries_scheduled:
                        retries_scheduled.add(retry_key)
                        target = repair if exc.repair_note else retry
                        target.append((provider, candidate_tier, exc.repair_note))
                    continue
                if output["actual_model"] != requested_model:
                    store.block_route(provider.fingerprint, requested_model)
                    audit.finish(sequence, "completed", output=output, diagnostic=output.get("diagnostic"), fulfilled=False)
                    observe(
                        store, provider, requested_model, candidate_tier, body, "completed", output=output,
                        attempt=audit.row(sequence), route_id=route_id,
                    )
                    continue
                audit.finish(sequence, "completed", output=output, diagnostic=output.get("diagnostic"), fulfilled=True)
                observe(
                    store, provider, requested_model, candidate_tier, body, "completed", output=output,
                    attempt=audit.row(sequence), route_id=route_id,
                )
                if winner is None:
                    winner = (output, provider, candidate_tier)

            if winner is not None:
                output, provider, candidate_tier = winner
                await cancel_active("cancelled")
                return output | {
                    "provider": provider.name, "attempts": audit.public(),
                    "fulfilled_intellect": candidate_tier, "fingerprint": provider.fingerprint,
                }

            while len(active) < cap and attempts_started < attempt_budget and (repair or primary or retry):
                if not launch_one():
                    break
                if hedge_delay_ms > 0:
                    break
    except asyncio.CancelledError:
        await cancel_active("cancelled")
        raise
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
