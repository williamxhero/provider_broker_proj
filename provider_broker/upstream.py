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
    def __init__(self, status: str, *, actual_model=None, latency_ms=None, diagnostic=None):
        super().__init__(status)
        self.status = status
        self.actual_model, self.latency_ms = actual_model, latency_ms
        self.diagnostic = diagnostic or {}


class UpstreamFailure(Exception):
    def __init__(self, attempts: list[dict]):
        super().__init__("all eligible providers failed")
        self.attempts = attempts


DEFAULT_ROUTE_ATTEMPT_BUDGET = 32

INTELLECT_RANK = {"standard": 0, "smart": 1, "expert": 2}


def fulfilled_intellect(model: str) -> str | None:
    entry = CATALOG.get(canonicalize(model))
    return entry.get("intellect") if entry else None


def model_fulfills(requested_model: str, actual_model: str | None) -> bool:
    """Accept a transport substitution only when it meets the requested tier."""
    requested = fulfilled_intellect(requested_model)
    actual = fulfilled_intellect(actual_model or "")
    return requested is not None and actual is not None and INTELLECT_RANK[actual] >= INTELLECT_RANK[requested]


def api_url(base_url: str, suffix: str) -> str:
    base = base_url.rstrip("/")
    return base + suffix if base.endswith("/v1") else base + "/v1" + suffix


def provider_headers(provider) -> dict[str, str]:
    return {str(name): str(value) for name, value in provider.request_headers.items()} | {"Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json"}


def extract_text(data: dict) -> str:
    if isinstance(data.get("output_text"), str): return data["output_text"]
    chunks = []
    for item in data.get("output", []) if isinstance(data.get("output"), list) else []:
        for part in item.get("content", []) if isinstance(item, dict) and isinstance(item.get("content"), list) else []:
            if isinstance(part, dict) and part.get("type") in ("output_text", "text") and isinstance(part.get("text"), str): chunks.append(part["text"])
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        content = (choices[0].get("message") or {}).get("content")
        if isinstance(content, str): chunks.append(content)
        elif isinstance(content, list): chunks.extend(part.get("text", "") for part in content if isinstance(part, dict) and isinstance(part.get("text"), str))
    return "".join(chunks)


def normalize_usage(data: dict) -> dict:
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    result = dict(usage)
    if "input_tokens" not in result and isinstance(usage.get("prompt_tokens"), int): result["input_tokens"] = usage["prompt_tokens"]
    if "output_tokens" not in result and isinstance(usage.get("completion_tokens"), int): result["output_tokens"] = usage["completion_tokens"]
    return result


def estimate_cost(model: str, usage: dict, multiplier: float, pricing=None) -> float | None:
    pricing = pricing or CATALOG.get(canonicalize(model)); input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
    if pricing is None or not isinstance(input_tokens, int) or not isinstance(output_tokens, int): return None
    details = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else {}
    cached = details.get("cached_tokens", 0) if isinstance(details.get("cached_tokens", 0), int) else 0
    return round((max(0, input_tokens - cached) * pricing["official_input_price"] + cached * pricing["official_cache_price"] + output_tokens * pricing["official_output_price"]) / 1_000_000 * multiplier, 10)


def structured_schema(body: dict) -> dict | None:
    """Recover the schema embedded by the desktop client's stable prompt envelope.

    The public Broker contract predates an explicit ``output_schema`` field.  The
    desktop client therefore sends a JSON envelope containing it in ``prompt``.
    Recognising that envelope here lets the Broker enforce the same contract at
    the provider boundary, without a coordinated desktop rollout.
    """
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
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


class StreamingAttempt:
    """One capacity-owning upstream stream; it can be handed to the winning caller."""
    def __init__(self, provider, body, store=None):
        self.provider, self.body, self.store = provider, body, store
        self.model = canonicalize(provider.models[0]); self.started = time.perf_counter()
        self.schema = structured_schema(body)
        self.session = self.response = None; self.ttfb_ms = self.ttft_ms = None
        self.metadata, self.chunks = {}, []; self.first_text = None; self.completed = False
        self.endpoint = None; self.response_status = None; self.content_type = None
        self.received_bytes = 0; self.event_types = []; self.finish_reason = None
        self.first_event_timeout_ms = max(1, int(body.get("_first_event_timeout_ms", 20_000)))
        self.first_event_deadline = time.monotonic() + self.first_event_timeout_ms / 1000
        self._acquired = False; self._closed = False

    def diagnostic(self) -> dict:
        """Return bounded transport evidence with no prompt, body, or credential."""
        result = {
            "endpoint": self.endpoint,
            "http_status": self.response_status,
            "content_type": self.content_type,
            "schema_hash": schema_hash(self.schema),
            "output_token_limit": self.body.get("output_token_limit", 1024),
            "event_types": list(self.event_types),
            "finish_reason": self.finish_reason,
            "stream_completed": self.completed,
            "received_bytes": self.received_bytes,
            "ttfb_ms": self.ttfb_ms,
            "ttft_ms": self.ttft_ms,
            "first_event_timeout_ms": self.first_event_timeout_ms,
        }
        return {key: value for key, value in result.items() if value is not None}

    def _request(self):
        if self.provider.provider_type in ("anthropic", "claude"):
            payload = {"model": self.model, "max_tokens": self.body.get("output_token_limit", 1024), "messages": [{"role": "user", "content": self.body["prompt"]}], "stream": True}
            if self.schema is not None:
                payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "broker_output", "strict": True, "schema": self.schema}}
            return "/chat/completions", payload
        payload = {"model": self.model, "input": self.body["prompt"], "max_output_tokens": self.body.get("output_token_limit", 1024), "stream": True}
        if self.body.get("effort"): payload["reasoning"] = {"effort": self.body["effort"]}
        if self.schema is not None:
            payload["text"] = {"format": {"type": "json_schema", "name": "broker_output", "strict": True, "schema": self.schema}}
        return "/responses", payload

    @classmethod
    async def start(cls, provider, body, store=None):
        attempt = cls(provider, body, store)
        try:
            if store is not None:
                if not store.try_acquire(provider): raise AttemptFailure("capacity_reached")
                attempt._acquired = True
            timeout_seconds = max(1, body.get("deadline_ms", 60000) / 1000)
            route_deadline = body.get("_route_deadline")
            if isinstance(route_deadline, (int, float)):
                remaining = route_deadline - time.monotonic()
                if remaining <= 0:
                    raise AttemptFailure("timed_out")
                timeout_seconds = min(timeout_seconds, remaining)
            endpoint, payload = attempt._request(); attempt.endpoint = endpoint; timeout = ClientTimeout(total=max(.1, timeout_seconds))
            attempt.session = ClientSession(timeout=timeout)
            first_event_remaining = min(timeout_seconds, attempt.first_event_deadline - time.monotonic())
            if first_event_remaining <= 0:
                raise AttemptFailure("first_token_timeout", diagnostic=attempt.diagnostic())
            try:
                attempt.response = await asyncio.wait_for(
                    attempt.session.post(api_url(provider.base_url, endpoint), json=payload, headers=provider_headers(provider)),
                    timeout=first_event_remaining,
                )
            except (asyncio.TimeoutError, TimeoutError) as exc:
                raise AttemptFailure("first_token_timeout", diagnostic=attempt.diagnostic()) from exc
            attempt.ttfb_ms = round((time.perf_counter() - attempt.started) * 1000, 2)
            attempt.response_status = attempt.response.status; attempt.content_type = attempt.response.content_type
            if attempt.response.status >= 400: raise AttemptFailure("unavailable", diagnostic=attempt.diagnostic())
            if attempt.response.content_type == "application/json":
                data = await attempt.response.json(content_type=None)
                text = extract_text(data) if isinstance(data, dict) else ""
                if not text.strip(): raise AttemptFailure("protocol_failed", diagnostic=attempt.diagnostic())
                attempt.metadata = data; attempt.first_text = text; attempt.chunks = [text]; attempt.completed = True
                attempt.ttft_ms = round((time.perf_counter() - attempt.started) * 1000, 2)
            return attempt
        except asyncio.CancelledError:
            await attempt.close()
            raise
        except AttemptFailure:
            await attempt.close(); raise
        except (asyncio.TimeoutError, TimeoutError):
            diagnostic = attempt.diagnostic(); await attempt.close(); raise AttemptFailure("timed_out", diagnostic=diagnostic)
        except (ClientConnectionError, ClientError, OSError, ValueError):
            diagnostic = attempt.diagnostic(); await attempt.close(); raise AttemptFailure("transport_failed", diagnostic=diagnostic)

    def _actual_model(self):
        value = self.metadata.get("model") if isinstance(self.metadata, dict) else None
        return canonicalize(str(value)) if value else None

    def _consume_event(self, event):
        if not isinstance(event, dict): return None
        event_type = event.get("type") if isinstance(event.get("type"), str) else "chat.chunk"
        if event_type not in self.event_types and len(self.event_types) < 12: self.event_types.append(event_type)
        if event_type in ("response.completed", "response.incomplete") and isinstance(event.get("response"), dict):
            self.metadata.update(event["response"]); self.completed = True
            self.finish_reason = event["response"].get("incomplete_details", {}).get("reason") if isinstance(event["response"].get("incomplete_details"), dict) else event_type
        else: self.metadata.update({key: event[key] for key in ("id", "model", "usage") if key in event})
        delta = event.get("delta") if isinstance(event.get("delta"), str) else None
        choices = event.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            choice = choices[0]; part = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            if isinstance(part.get("content"), str): delta = part["content"]
            if choice.get("finish_reason") is not None: self.finish_reason = str(choice["finish_reason"]); self.completed = True
        # reasoning and tool events intentionally have no recognised text delta.
        if delta and delta.strip():
            self.chunks.append(delta)
            if self.first_text is None:
                self.first_text = delta; self.ttft_ms = round((time.perf_counter() - self.started) * 1000, 2)
            return delta
        return None

    async def _read_event(self):
        raw = await self.response.content.readline()
        if not raw: self.completed = True; return None
        self.received_bytes += len(raw)
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"): return None
        value = line[5:].strip()
        if value == "[DONE]": self.completed = True; return None
        try: return self._consume_event(json.loads(value))
        except Exception as exc: raise AttemptFailure("protocol_failed", diagnostic=self.diagnostic()) from exc

    def _validate_structured_output(self):
        try:
            parsed = json.loads("".join(self.chunks).strip())
        except json.JSONDecodeError as exc:
            status = "output_truncated" if self.finish_reason in {"length", "max_tokens", "max_output_tokens"} else "structured_output_invalid"
            diagnostic = self.diagnostic(); diagnostic["structured_error_kind"] = "json_decode"
            raise AttemptFailure(status, diagnostic=diagnostic) from exc
        if not isinstance(parsed, (dict, list)):
            diagnostic = self.diagnostic(); diagnostic["structured_error_kind"] = "non_container"
            raise AttemptFailure("structured_output_invalid", diagnostic=diagnostic)
        try:
            Draft202012Validator(self.schema).validate(parsed)
        except SchemaError as exc:
            diagnostic = self.diagnostic()
            diagnostic.update({"structured_error_kind": "schema_definition", "validator": str(exc.validator),
                               "validation_path": [str(part) for part in list(exc.absolute_path)[:8]]})
            raise AttemptFailure("structured_schema_invalid", diagnostic=diagnostic) from exc
        except ValidationError as exc:
            diagnostic = self.diagnostic()
            diagnostic.update({"structured_error_kind": "schema_validation", "validator": str(exc.validator),
                               "validation_path": [str(part) for part in list(exc.absolute_path)[:8]]})
            raise AttemptFailure("structured_output_invalid", diagnostic=diagnostic) from exc

    async def wait_for_winner(self):
        """Wait for text and identity, and fully validate structured responses.

        A schema-bound request is intentionally buffered until completion.  This
        prevents a provider's ordinary prose from being leaked as a successful
        stream before the Broker can reject it and route to another provider.
        """
        try:
            while True:
                if self.first_text is not None and self._actual_model() is not None:
                    if not model_fulfills(self.model, self._actual_model()): raise AttemptFailure("model_mismatch", actual_model=self._actual_model(), latency_ms=self.ttft_ms, diagnostic=self.diagnostic())
                    if self.schema is not None:
                        if not self.completed:
                            await self._read_event()
                            continue
                        self._validate_structured_output()
                    return self
                if self.completed:
                    if self.first_text is None: raise AttemptFailure("stream_incomplete", diagnostic=self.diagnostic())
                    # A protocol which completes without model metadata cannot prove identity.
                    raise AttemptFailure("protocol_failed", diagnostic=self.diagnostic())
                remaining = self.first_event_deadline - time.monotonic()
                if remaining <= 0:
                    raise AttemptFailure("first_token_timeout", diagnostic=self.diagnostic())
                try:
                    await asyncio.wait_for(self._read_event(), timeout=remaining)
                except (asyncio.TimeoutError, TimeoutError) as exc:
                    raise AttemptFailure("first_token_timeout", diagnostic=self.diagnostic()) from exc
        except AttemptFailure:
            await self.close(); raise
        except (asyncio.TimeoutError, TimeoutError):
            diagnostic = self.diagnostic(); await self.close(); raise AttemptFailure("timed_out", diagnostic=diagnostic)
        except (ClientConnectionError, ClientError, OSError):
            diagnostic = self.diagnostic(); await self.close(); raise AttemptFailure("transport_failed", diagnostic=diagnostic)

    async def iter_text(self):
        """Yield buffered first text once, then relay the winning response live."""
        emitted = 0
        while emitted < len(self.chunks):
            yield self.chunks[emitted]; emitted += 1
        try:
            while not self.completed:
                delta = await self._read_event()
                if delta:
                    emitted = len(self.chunks); yield delta
        finally:
            await self.close()

    async def result(self):
        async for _ in self.iter_text(): pass
        actual = self._actual_model()
        if not model_fulfills(self.model, actual): raise AttemptFailure("model_mismatch", actual_model=actual, latency_ms=self.ttft_ms, diagnostic=self.diagnostic())
        if not self.first_text: raise AttemptFailure("stream_incomplete", diagnostic=self.diagnostic())
        usage = normalize_usage(self.metadata)
        return {"text": "".join(self.chunks), "chunks": list(self.chunks), "actual_model": actual,
                "latency_ms": self.ttft_ms, "ttfb_ms": self.ttfb_ms, "duration_ms": round((time.perf_counter() - self.started) * 1000, 2),
                "usage": usage, "request_id": str(self.metadata.get("id") or uuid.uuid4()),
                "cost": estimate_cost(actual, usage, self.provider.multiplier, self.provider.pricing)}

    async def close(self):
        if self._closed: return
        self._closed = True
        if self.response is not None: self.response.close()
        if self.session is not None: await self.session.close()
        if self._acquired: self.store.release(self.provider); self._acquired = False


async def invoke_stream(provider, body: dict) -> dict:
    attempt = await StreamingAttempt.start(provider, body)
    try:
        await attempt.wait_for_winner()
        return await attempt.result()
    finally:
        await attempt.close()


def observe(store, provider, requested_model, tier, body, status, *, output=None, diagnostic=None):
    output = output or {}; usage = output.get("usage") or {}
    store.observe(fingerprint=provider.fingerprint, requested_model=requested_model, actual_model=output.get("actual_model"), tier=tier, effort=body.get("effort"), success=int(status == "completed"), latency_ms=output.get("latency_ms"), error=None, status=status, input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"), cost=output.get("cost"), request_id=output.get("request_id") or str(uuid.uuid4()), diagnostic=diagnostic)


async def _hedged_band(store, providers, tier, body, cap, hedge_delay_ms, attempts):
    # cap limits simultaneous attempts, not the total number of candidates that
    # may be tried.  Keep the full band queued so a failed or silent stream can
    # immediately release its slot to the next healthy route.
    shuffled = random.sample(providers, k=len(providers))
    state_rank = {"healthy": 0, "half_open": 1, "unknown": 2, "suspect": 3}
    def quality(provider):
        evidence = store.health(provider.fingerprint, canonicalize(provider.models[0]))
        success = evidence.get("smoothed_success")
        ttft = evidence.get("smoothed_ttft_ms")
        return (
            state_rank.get(evidence.get("state"), 4),
            0 if evidence.get("last_real_success") else 1,
            -float(success) if success is not None else 1.0,
            float(ttft) if ttft is not None else float("inf"),
        )
    # Shuffle first so equally evidenced routes remain load-balanced, then use
    # stable quality ordering to keep proven real successes ahead of unknown or
    # repeatedly failing candidates.
    selected = sorted(shuffled, key=quality)
    cap = min(len(selected), max(1, cap))
    active, next_index, loop = {}, 0, asyncio.get_running_loop()
    async def launch(provider):
        if body["_route_attempts_started"] >= body["_route_attempt_budget"]:
            return False
        body["_route_attempts_started"] += 1
        async def run():
            attempt = None
            try:
                attempt = await StreamingAttempt.start(provider, body, store)
                await attempt.wait_for_winner()
                return attempt
            except BaseException:
                if attempt is not None: await attempt.close()
                raise
        active[asyncio.create_task(run())] = provider
        return True
    if not await launch(selected[0]):
        return None
    next_index = 1; deadline = loop.time() + hedge_delay_ms / 1000
    while active:
        timeout = None if next_index >= len(selected) or len(active) >= cap else max(0, deadline - loop.time())
        done, _ = await asyncio.wait(active, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if not done:
            if await launch(selected[next_index]):
                next_index += 1; deadline = loop.time() + hedge_delay_ms / 1000
            else:
                deadline = loop.time() + hedge_delay_ms / 1000
            continue
        for task in done:
            provider = active.pop(task); model = canonicalize(provider.models[0])
            try: winner = task.result()
            except AttemptFailure as exc:
                mismatch_output = {"actual_model": exc.actual_model, "latency_ms": exc.latency_ms} if exc.status == "model_mismatch" else None
                observe(store, provider, model, tier, body, "completed" if exc.status == "model_mismatch" else exc.status, output=mismatch_output, diagnostic=exc.diagnostic)
                store.record_health(provider.fingerprint, model, success=False, real=True, immediate_open=exc.status == "model_mismatch")
                if exc.status == "model_mismatch": store.block_route(provider.fingerprint, model)
                attempts.append({"provider": provider.name, "status": "completed" if exc.status == "model_mismatch" else exc.status, "actual_model": exc.actual_model, "fulfilled": False} if exc.status == "model_mismatch" else {"provider": provider.name, "status": exc.status})
                if next_index < len(selected) and len(active) < cap and await launch(selected[next_index]):
                    next_index += 1; deadline = loop.time() + hedge_delay_ms / 1000
                continue
            for loser, loser_provider in list(active.items()):
                loser.cancel(); await asyncio.gather(loser, return_exceptions=True)
                observe(store, loser_provider, canonicalize(loser_provider.models[0]), tier, body, "cancelled")
                attempts.append({"provider": loser_provider.name, "status": "cancelled"})
            active.clear()
            return winner
    return None


async def route(store, tier: str, body: dict, parallel_cap: int = 3, hedge_delay_ms: int = 750,
                first_event_timeout_ms: int = 20_000,
                route_attempt_budget: int = DEFAULT_ROUTE_ATTEMPT_BUDGET) -> dict:
    attempts = []; tiers = ("standard", "smart", "expert")
    effort_multiplier = {"medium": 2, "high": 3}.get(body.get("effort"), 1)
    effective_first_event_timeout_ms = max(1, first_event_timeout_ms) * effort_multiplier
    body = {**body, "_route_deadline": time.monotonic() + max(1, body.get("deadline_ms", 60_000) / 1000),
            "_route_attempts_started": 0, "_route_attempt_budget": max(1, route_attempt_budget),
            "_first_event_timeout_ms": effective_first_event_timeout_ms}
    for candidate_tier in tiers[tiers.index(tier):]:
        for band in price_bands(store.providers(candidate_tier)):
            if body["_route_attempts_started"] >= body["_route_attempt_budget"] or time.monotonic() >= body["_route_deadline"]:
                raise UpstreamFailure(attempts)
            available = [provider for provider in band if store.has_capacity(provider)]
            if not available: continue
            winner = await _hedged_band(store, available, candidate_tier, body, parallel_cap, hedge_delay_ms, attempts)
            if winner is None: continue
            actual_model = winner._actual_model() or winner.model
            fulfilled = candidate_tier if actual_model == winner.model else fulfilled_intellect(actual_model) or candidate_tier
            output = {"actual_model": actual_model, "latency_ms": winner.ttft_ms}
            attempts.append({"provider": winner.provider.name, "status": "completed", "actual_model": actual_model, "fulfilled": True})
            return {"attempt": winner, "provider": winner.provider.name, "attempts": attempts, "fulfilled_intellect": fulfilled, "fingerprint": winner.provider.fingerprint, "output": output}
    raise UpstreamFailure(attempts)


def price_bands(providers):
    ordered = sorted(providers, key=lambda provider: (provider.price_group, provider.id))
    if not ordered: return []
    midpoint = len(ordered) // 2; median = ordered[midpoint].price_group if len(ordered) % 2 else (ordered[midpoint - 1].price_group + ordered[midpoint].price_group) / 2
    lower = [provider for provider in ordered if provider.price_group <= median]; higher = [provider for provider in ordered if provider.price_group > median]
    return [lower] + ([higher] if higher else [])
