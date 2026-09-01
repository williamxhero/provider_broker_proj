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

INTELLECT_RANK = {"standard": 0, "smart": 1, "expert": 2}


def fulfilled_intellect(model: str) -> str | None:
    entry = CATALOG.get(canonicalize(model))
    return entry.get("intellect") if entry else None


def model_fulfills(requested_model: str, actual_model: str | None) -> bool:
    requested = fulfilled_intellect(requested_model)
    actual = fulfilled_intellect(actual_model or "")
    return requested is not None and actual is not None and INTELLECT_RANK[actual] >= INTELLECT_RANK[requested]


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
    "client_deadline_ms", "route_budget_ms", "response_reserve_ms",
    "prompt_sha256", "prompt_chars", "prompt_bytes", "request_bytes", "schema_sha256",
    "route_score", "queue_kind",
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


def replace_one_of_with_any_of(value):
    """Copy a schema while replacing unsupported ``oneOf`` unions for Codex routes."""
    if isinstance(value, dict):
        return {
            ("anyOf" if key == "oneOf" else key): replace_one_of_with_any_of(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [replace_one_of_with_any_of(item) for item in value]
    return value


def provider_native_schema(schema: dict | None, provider_type: str) -> dict | None:
    """Return a provider-safe native schema, or fall back to prompt enforcement.

    OpenAI-compatible strict schema implementations reject object nodes without
    an explicit closed property set.  Sending such a schema causes a transport
    400 before the model can answer.  The Broker still embeds the authoritative
    schema in the prompt and validates the returned JSON against it locally.
    """
    if schema is None or _contains_open_object(schema):
        return None
    return schema if provider_type in ("anthropic", "claude") else replace_one_of_with_any_of(schema)


def _contains_open_object(value) -> bool:
    if isinstance(value, dict):
        node_types = value.get("type")
        is_object = node_types == "object" or isinstance(node_types, list) and "object" in node_types
        if is_object and not isinstance(value.get("properties"), dict):
            return True
        return any(_contains_open_object(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_open_object(item) for item in value)
    return False


def schema_hash(schema: dict | None) -> str | None:
    if schema is None:
        return None
    encoded = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def full_schema_hash(schema: dict | None) -> str | None:
    if schema is None:
        return None
    encoded = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def request_shape_diagnostic(body: dict) -> dict:
    prompt = body.get("prompt") if isinstance(body.get("prompt"), str) else ""
    prompt_bytes = prompt.encode("utf-8")
    public_body = {key: value for key, value in body.items() if not key.startswith("_")}
    schema = structured_schema(body)
    return {
        "prompt_sha256": hashlib.sha256(prompt_bytes).hexdigest(),
        "prompt_chars": len(prompt), "prompt_bytes": len(prompt_bytes),
        "request_bytes": len(json.dumps(public_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
        "schema_hash": schema_hash(schema), "schema_sha256": full_schema_hash(schema),
        "output_token_limit": body.get("output_token_limit", 1024),
    }


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
    reinforced += "\nExact JSON Schema (authoritative):\n" + json.dumps(
        schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    task_instruction = _enveloped_task_instruction(prompt)
    if task_instruction:
        reinforced += (
            "\nAuthoritative task-specific instruction from the request envelope (apply in addition to the schema):\n"
            + task_instruction
        )
    if repair_note:
        reinforced += "\nA prior attempt was rejected. " + repair_note + " Generate the entire JSON again from scratch."
    return reinforced


def _enveloped_task_instruction(prompt: str) -> str | None:
    """Surface a deeply nested task instruction after large structured packets."""
    try:
        envelope = json.loads(prompt)
    except json.JSONDecodeError:
        return None
    packet = envelope.get("input") if isinstance(envelope, dict) else None
    instruction = packet.get("instruction") if isinstance(packet, dict) else None
    if not isinstance(instruction, str) or not instruction.strip():
        return None
    contract = packet.get("evidence_contract") if isinstance(packet, dict) else None
    requirements = contract.get("requirements") if isinstance(contract, dict) else None
    keys = [
        str(row.get("key")) for row in requirements or []
        if isinstance(row, dict) and row.get("key")
    ]
    if keys and packet.get("stage") == "research_plan":
        gaps = [str(item) for item in packet.get("coverage_gaps") or []]
        instruction += (
            "\nUse only these exact requirement_key values; never split, suffix, prefix, or invent a requirement key: "
            + ", ".join(keys) + "."
            " Cover every requirement named by the current coverage gaps using its exact key."
        )
        if gaps:
            instruction += " Current coverage gaps: " + " | ".join(gaps) + "."
    return instruction


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
        raise AttemptFailure(
            "output_truncated",
            diagnostic=diagnostic | {"structured_error_kind": "truncated"},
            repair_note=(
                "The prior response reached the output limit. Regenerate concise, complete JSON: "
                "keep every required field, remove repetition, and finish well within the token limit."
            ),
        )
    try:
        parsed = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise AttemptFailure(
            "structured_output_invalid",
            diagnostic=diagnostic | {"structured_error_kind": "json_decode"},
            repair_note="The prior response was not one complete JSON value. Return only a complete JSON value with no prose or fences.",
        ) from exc
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
        elif str(exc.validator):
            repair_note = f"At {location}, satisfy the JSON Schema {exc.validator} constraint exactly."
        raise AttemptFailure("structured_output_invalid", diagnostic=diagnostic | {
            "structured_error_kind": "schema_validation", "validator": str(exc.validator),
            "validation_path": path, "unexpected_properties": unexpected,
            "normalized_properties": normalized,
        }, repair_note=repair_note) from exc


async def invoke_stream(provider, body: dict) -> dict:
    model = canonicalize(provider.models[0])
    schema = structured_schema(body)
    outbound_schema = provider_native_schema(schema, provider.provider_type)
    effort = body.get("effort")
    repair_note = body.get("_structured_repair_note") if isinstance(body.get("_structured_repair_note"), str) else None
    provider_prompt = body["prompt"] if body.get("_preserve_prompt_envelope") else strict_schema_prompt(body["prompt"], schema, repair_note)
    if provider.provider_type in ("anthropic", "claude"):
        payload = {"model": model, "max_tokens": body.get("output_token_limit", 1024), "messages": [{"role": "user", "content": provider_prompt}], "stream": True}
        if outbound_schema is not None:
            payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "broker_output", "strict": True, "schema": outbound_schema}}
        endpoint = "/chat/completions"
    else:
        payload = {"model": model, "input": provider_prompt, "max_output_tokens": body.get("output_token_limit", 1024), "stream": True}
        if effort:
            payload["reasoning"] = {"effort": effort}
        if outbound_schema is not None:
            payload["text"] = {"format": {"type": "json_schema", "name": "broker_output", "strict": True, "schema": outbound_schema}}
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
    request_shape = request_shape_diagnostic(body)

    def diagnostic():
        return sanitize_diagnostic({
            "endpoint": endpoint, "http_status": response_status, "content_type": content_type,
            **request_shape,
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
            "client_deadline_ms": body.get("_client_deadline_ms"),
            "route_budget_ms": body.get("_route_budget_ms"),
            "response_reserve_ms": body.get("_response_reserve_ms"),
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
    neutral = {"cancelled", "client_cancelled"}
    if status not in neutral and hasattr(store, "record_health"):
        store.record_health(
            provider.fingerprint, requested_model, success=status == "completed", real=True,
            ttft_ms=output.get("latency_ms"), immediate_open=status == "model_mismatch",
        )


class AttemptAudit:
    def __init__(self, route_started: float):
        self.route_started = route_started
        self.rows = {}

    def start(self, sequence: int, provider, *, queue_kind: str, route_score: int):
        self.rows[sequence] = {
            "attempt": sequence + 1, "provider": provider.name,
            "model": canonicalize(provider.models[0]),
            "started_ms": round((time.monotonic() - self.route_started) * 1000, 2),
            "diagnostic": {"queue_kind": queue_kind, "route_score": route_score},
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
        incoming = {key: value for key, value in (diagnostic or {}).items() if value is not None}
        safe = sanitize_diagnostic(row.get("diagnostic", {}) | incoming)
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
                route_attempt_budget: int = 32, response_reserve_ms: int = 5000,
                cancel_grace_ms: int = 50) -> dict:
    route_started = time.monotonic()
    deadline_ms = body.get("deadline_ms", 60000)
    client_deadline_ms = max(1, int(deadline_ms)) if isinstance(deadline_ms, (int, float)) else 60000
    reserve_cap_ms = max(0, client_deadline_ms // 10)
    reserve_ms = min(max(0, int(response_reserve_ms)), reserve_cap_ms, max(0, client_deadline_ms - 1))
    route_budget_ms = max(1, client_deadline_ms - reserve_ms)
    deadline_seconds = route_budget_ms / 1000
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
        "_client_deadline_ms": client_deadline_ms,
        "_route_budget_ms": route_budget_ms,
        "_response_reserve_ms": reserve_ms,
    }
    tiers = ("standard", "smart", "expert")
    primary = deque()
    repair = deque()
    priority_retry = deque()
    retry = deque()
    retries_scheduled = set()
    route_scores = {}

    def candidate_score(provider, candidate_tier):
        key = (provider.fingerprint, canonicalize(provider.models[0]), candidate_tier)
        if key not in route_scores:
            route_scores[key] = store.route_score(provider, key[1], body)
        return route_scores[key]

    for candidate_tier in tiers[tiers.index(tier):]:
        for band in price_bands(store.providers(candidate_tier)):
            available = [provider for provider in band if store.has_capacity(provider)]
            randomized = random.sample(available, k=len(available))
            ranked = sorted(
                randomized,
                key=lambda provider: candidate_score(provider, candidate_tier),
                reverse=True,
            )
            primary.extend(
                (provider, candidate_tier, None, candidate_score(provider, candidate_tier))
                for provider in ranked
            )

    active = {}
    next_hedge_at = route_started

    def launch_one():
        nonlocal attempts_started, next_hedge_at
        while (repair or priority_retry or primary or retry) and attempts_started < attempt_budget and time.monotonic() < route_deadline:
            if repair:
                provider, candidate_tier, repair_note, route_score = repair.popleft()
                queue_kind = "repair"
            elif priority_retry:
                provider, candidate_tier, repair_note, route_score = priority_retry.popleft()
                queue_kind = "priority_retry"
            elif primary:
                provider, candidate_tier, repair_note, route_score = primary.popleft()
                queue_kind = "primary"
            else:
                provider, candidate_tier, repair_note, route_score = retry.popleft()
                queue_kind = "retry"
            if not store.try_acquire(provider):
                continue
            sequence = attempts_started
            attempts_started += 1
            audit.start(sequence, provider, queue_kind=queue_kind, route_score=route_score)

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
        tasks = list(active)
        for task, (sequence, provider, candidate_tier) in active.items():
            task.cancel()
            diagnostic = request_shape_diagnostic(body) | {
                "client_deadline_ms": client_deadline_ms, "route_budget_ms": route_budget_ms,
                "response_reserve_ms": reserve_ms,
            }
            if status == "timed_out":
                diagnostic["first_event_timeout_ms"] = effective_first_event_timeout_ms
            audit.finish(sequence, status, diagnostic=diagnostic)
            observe(
                store, provider, canonicalize(provider.models[0]), candidate_tier, body, status,
                attempt=audit.row(sequence), route_id=route_id,
            )
        active.clear()
        if not tasks:
            return
        done, pending = await asyncio.wait(tasks, timeout=max(0, cancel_grace_ms) / 1000)

        def consume_result(task):
            if not task.cancelled():
                try:
                    task.exception()
                except (asyncio.CancelledError, Exception):
                    pass

        for task in done:
            consume_result(task)
        for task in pending:
            task.cancel()
            task.add_done_callback(consume_result)

    launch_one()
    try:
        while active or repair or priority_retry or primary or retry:
            now = time.monotonic()
            if now >= route_deadline or attempts_started >= attempt_budget and not active:
                if active:
                    await cancel_active("timed_out")
                break
            if not active:
                if not launch_one():
                    break
                continue
            can_hedge = bool(repair or priority_retry or primary or retry) and attempts_started < attempt_budget and len(active) < cap
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
                    retry_kind = "repair" if exc.repair_note else "transient"
                    retry_key = (provider.fingerprint, requested_model, candidate_tier, retry_kind)
                    if retryable_attempt(exc) and retry_key not in retries_scheduled:
                        retries_scheduled.add(retry_key)
                        route_score = candidate_score(provider, candidate_tier)
                        target = repair if exc.repair_note else priority_retry if route_score >= 500 else retry
                        target.append((provider, candidate_tier, exc.repair_note, route_score))
                    continue
                if not model_fulfills(requested_model, output["actual_model"]):
                    store.block_route(provider.fingerprint, requested_model)
                    audit.finish(sequence, "model_mismatch", output=output, diagnostic=output.get("diagnostic"), fulfilled=False)
                    observe(
                        store, provider, requested_model, candidate_tier, body, "model_mismatch", output=output,
                        attempt=audit.row(sequence), route_id=route_id,
                    )
                    continue
                audit.finish(sequence, "completed", output=output, diagnostic=output.get("diagnostic"), fulfilled=True)
                observe(
                    store, provider, requested_model, candidate_tier, body, "completed", output=output,
                    attempt=audit.row(sequence), route_id=route_id,
                )
                if winner is None:
                    actual_tier = fulfilled_intellect(output["actual_model"])
                    fulfilled_tier = actual_tier if actual_tier and INTELLECT_RANK[actual_tier] > INTELLECT_RANK[candidate_tier] else candidate_tier
                    winner = (output, provider, fulfilled_tier)

            if winner is not None:
                output, provider, candidate_tier = winner
                await cancel_active("cancelled")
                return output | {
                    "provider": provider.name, "attempts": audit.public(),
                    "fulfilled_intellect": candidate_tier, "fingerprint": provider.fingerprint,
                }

            while len(active) < cap and attempts_started < attempt_budget and (repair or priority_retry or primary or retry):
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
