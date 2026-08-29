import asyncio
import random
import time
import uuid

from aiohttp import ClientConnectionError, ClientError, ClientSession

from .catalog import CATALOG, canonicalize


class AttemptFailure(Exception):
    def __init__(self, status: str):
        super().__init__(status)
        self.status = status


class UpstreamFailure(Exception):
    def __init__(self, attempts: list[dict]):
        super().__init__("all eligible providers failed")
        self.attempts = attempts


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


def estimate_cost(model: str, usage: dict, multiplier: float) -> float | None:
    pricing = CATALOG.get(canonicalize(model))
    input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
    if pricing is None or not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return None
    details = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else {}
    cached = details.get("cached_tokens", 0) if isinstance(details.get("cached_tokens", 0), int) else 0
    uncached = max(0, input_tokens - cached)
    cost = (uncached * pricing["official_input_price"] + cached * pricing["official_cache_price"] + output_tokens * pricing["official_output_price"]) / 1_000_000
    return round(cost * multiplier, 10)


async def invoke(provider, body: dict) -> dict:
    model = provider.models[0]
    effort = body.get("effort")
    headers = provider_headers(provider)
    if provider.provider_type in ("anthropic", "claude"):
        payload = {"model": model, "max_tokens": body.get("output_token_limit", 1024), "messages": [{"role": "user", "content": body["prompt"]}]}
        endpoint = "/chat/completions"
    else:
        payload = {"model": model, "input": body["prompt"], "max_output_tokens": body.get("output_token_limit", 1024), "stream": False}
        if effort:
            payload["reasoning"] = {"effort": effort}
        endpoint = "/responses"
    started = time.perf_counter()
    timeout = max(1, body.get("deadline_ms", 60000) / 1000)
    try:
        async with ClientSession() as session:
            async with session.post(api_url(provider.base_url, endpoint), json=payload, headers=headers, timeout=timeout) as response:
                if response.status >= 400:
                    await response.read()
                    raise AttemptFailure("unavailable")
                try:
                    data = await response.json(content_type=None)
                except Exception as exc:
                    raise AttemptFailure("protocol_failed") from exc
    except AttemptFailure:
        raise
    except (asyncio.TimeoutError, TimeoutError) as exc:
        raise AttemptFailure("timed_out") from exc
    except (ClientConnectionError, ClientError, OSError) as exc:
        raise AttemptFailure("transport_failed") from exc
    text = extract_text(data) if isinstance(data, dict) else ""
    if not text.strip():
        raise AttemptFailure("protocol_failed")
    usage = normalize_usage(data)
    actual_model = canonicalize(str(data.get("model") or model))
    return {
        "text": text, "actual_model": actual_model,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "usage": usage, "request_id": str(data.get("id") or uuid.uuid4()),
        "cost": estimate_cost(actual_model, usage, provider.multiplier),
    }


async def invoke_stream(provider, body: dict) -> dict:
    model = provider.models[0]
    effort = body.get("effort")
    headers = provider_headers(provider)
    if provider.provider_type in ("anthropic", "claude"):
        payload = {"model": model, "max_tokens": body.get("output_token_limit", 1024), "messages": [{"role": "user", "content": body["prompt"]}], "stream": True}
        endpoint = "/chat/completions"
    else:
        payload = {"model": model, "input": body["prompt"], "max_output_tokens": body.get("output_token_limit", 1024), "stream": True}
        if effort:
            payload["reasoning"] = {"effort": effort}
        endpoint = "/responses"
    started = time.perf_counter()
    first_delta = None
    chunks = []
    completed = False
    metadata = {}
    timeout = max(1, body.get("deadline_ms", 60000) / 1000)
    try:
        async with ClientSession() as session:
            async with session.post(api_url(provider.base_url, endpoint), json=payload, headers=headers, timeout=timeout) as response:
                if response.status >= 400:
                    await response.read()
                    raise AttemptFailure("unavailable")
                while True:
                    raw = await response.content.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload_text = line[5:].strip()
                    if payload_text == "[DONE]":
                        completed = True
                        continue
                    try:
                        event = __import__("json").loads(payload_text)
                    except Exception as exc:
                        raise AttemptFailure("protocol_failed") from exc
                    if not isinstance(event, dict):
                        continue
                    delta = event.get("delta") if isinstance(event.get("delta"), str) else None
                    choices = event.get("choices")
                    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                        choice = choices[0]
                        choice_delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
                        if isinstance(choice_delta.get("content"), str):
                            delta = choice_delta["content"]
                        if choice.get("finish_reason") is not None:
                            completed = True
                    if delta:
                        if first_delta is None:
                            first_delta = time.perf_counter()
                        chunks.append(delta)
                    if event.get("type") == "response.completed" and isinstance(event.get("response"), dict):
                        metadata = event["response"]
                        completed = True
                    elif any(key in event for key in ("id", "model", "usage")):
                        metadata.update({key: event[key] for key in ("id", "model", "usage") if key in event})
    except AttemptFailure:
        raise
    except (asyncio.TimeoutError, TimeoutError) as exc:
        raise AttemptFailure("timed_out") from exc
    except (ClientConnectionError, ClientError, OSError) as exc:
        raise AttemptFailure("transport_failed") from exc
    if not completed or not "".join(chunks).strip():
        raise AttemptFailure("stream_incomplete")
    usage = normalize_usage(metadata)
    actual_model = canonicalize(str(metadata.get("model") or model))
    ttft = ((first_delta or time.perf_counter()) - started) * 1000
    return {
        "text": "".join(chunks), "chunks": chunks, "actual_model": actual_model,
        "latency_ms": round(ttft, 2), "usage": usage,
        "request_id": str(metadata.get("id") or uuid.uuid4()),
        "cost": estimate_cost(actual_model, usage, provider.multiplier),
    }


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


async def route(store, tier: str, body: dict, parallel_cap: int = 3, invoker=invoke) -> dict:
    attempts = []
    tiers = ("standard", "smart", "expert")
    groups = {}
    for candidate_tier in tiers[tiers.index(tier):]:
        for provider in store.providers(candidate_tier):
            groups.setdefault((candidate_tier, provider.price_group), []).append(provider)
    for (candidate_tier, _), providers in sorted(groups.items(), key=lambda item: (tiers.index(item[0][0]), item[0][1])):
        selected = random.sample(providers, k=min(len(providers), max(1, parallel_cap)))
        tasks = {asyncio.create_task(invoker(provider, body)): provider for provider in selected}
        while tasks:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                provider = tasks.pop(task)
                requested_model = canonicalize(provider.models[0])
                try:
                    output = task.result()
                except AttemptFailure as exc:
                    observe(store, provider, requested_model, candidate_tier, body, exc.status)
                    attempts.append({"provider": provider.name, "status": exc.status})
                    continue
                if output["actual_model"] != requested_model:
                    observe(store, provider, requested_model, candidate_tier, body, "completed", output=output)
                    store.block_route(provider.fingerprint, requested_model)
                    attempts.append({"provider": provider.name, "status": "completed", "actual_model": output["actual_model"], "fulfilled": False})
                    continue
                observe(store, provider, requested_model, candidate_tier, body, "completed", output=output)
                attempts.append({"provider": provider.name, "status": "completed", "actual_model": output["actual_model"], "fulfilled": True})
                pending = list(tasks.items())
                for pending_task, _ in pending:
                    pending_task.cancel()
                await asyncio.gather(*(pending_task for pending_task, _ in pending), return_exceptions=True)
                for _, pending_provider in pending:
                    observe(store, pending_provider, canonicalize(pending_provider.models[0]), candidate_tier, body, "cancelled")
                    attempts.append({"provider": pending_provider.name, "status": "cancelled"})
                return output | {"provider": provider.name, "attempts": attempts, "fulfilled_intellect": candidate_tier, "fingerprint": provider.fingerprint}
    raise UpstreamFailure(attempts)
