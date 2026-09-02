import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from provider_broker.app import create_app
from provider_broker.db import Store
from provider_broker.settings import Settings
from provider_broker.upstream import (
    AttemptFailure,
    UpstreamFailure,
    invoke_stream,
    route,
    research_plan_output_audit,
    research_plan_context_audit,
    sanitize_diagnostic,
    strict_schema_prompt,
    provider_native_schema,
    structured_schema,
    validate_structured_output,
)


def test_research_plan_audit_keeps_only_verifier_fields_and_redacts_secret_urls():
    prompt = json.dumps({"input": {
        "stage": "research_plan", "available_backends": ["gateway"],
        "coverage_gaps": ["current_market_state"],
        "market_time_context": {"timezone": "Asia/Shanghai", "requirements": [{
            "requirement_key": "current_market_state", "window_mode": "exact",
            "start_utc": "2026-09-01T07:00:00Z", "end_utc": "2026-09-01T07:00:00Z",
            "start_local": "2026-09-01T15:00:00+08:00", "end_local": "2026-09-01T15:00:00+08:00",
            "is_local_market_close": True, "ignored": "private",
        }]},
        "research_discoveries": [{
            "requirement_key": "current_market_state", "source_kind": "deterministic_public_market",
            "url": "https://example.test/close?token=secret", "title": "not-recorded",
        }],
    }})
    output = json.dumps({
        "version": 1,
        "operations": [{
            "requirement_key": "current_market_state",
            "backend": "gateway",
            "operation": "web_read",
            "arguments": {
                "query": "2026-09-01 market close",
                "categories": None,
                "url": "https://example.test/close?token=secret&date=2026-09-01",
                "symbol": None,
                "render": None,
                "session_id": None,
                "actions": None,
            },
            "fallback_backends": ["market"],
            "ignored": "must-not-be-recorded",
        }],
        "private": "must-not-be-recorded",
    })

    audit = research_plan_output_audit({"prompt": prompt}, output)
    context = research_plan_context_audit({"prompt": prompt})
    diagnostic = sanitize_diagnostic({
        "research_plan_output": audit, "research_plan_context": context, "prompt": "secret prompt",
    })

    assert diagnostic["research_plan_output"]["operations"][0] == {
        "requirement_key": "current_market_state",
        "backend": "gateway",
        "operation": "web_read",
        "arguments": {
            "query": "2026-09-01 market close",
            "categories": None,
            "url": "https://example.test/close?token=%3CREDACTED%3E&date=2026-09-01",
            "symbol": None,
            "render": None,
            "session_id": None,
            "actions": None,
        },
        "fallback_backends": ["market"],
    }
    assert "must-not-be-recorded" not in json.dumps(diagnostic)
    assert "secret prompt" not in json.dumps(diagnostic)
    assert diagnostic["research_plan_context"]["available_backends"] == ["gateway"]
    assert diagnostic["research_plan_context"]["research_discoveries"] == [{
        "requirement_key": "current_market_state", "source_kind": "deterministic_public_market",
        "url": "https://example.test/close?token=%3CREDACTED%3E",
    }]
    assert "not-recorded" not in json.dumps(diagnostic)


def provider(index: int, *, secret: str):
    return SimpleNamespace(
        id=index,
        fingerprint=f"fingerprint-{index}",
        name=f"Provider {index}",
        base_url=f"https://secret-host-{index}.invalid",
        api_key=secret,
        provider_type="openai",
        request_headers={"X-Private": f"private-header-{index}"},
        models=["gpt-5.6-luna"],
        pricing=None,
        price_group=100,
        max_parallel=4,
        enabled=True,
        multiplier=1.0,
    )


class FakeStore:
    def __init__(self, providers, tier="standard"):
        self.items = providers
        self.tier = tier
        self.inflight = {}
        self.observations = []
        self.route_scores = {}

    def providers(self, tier):
        return self.items if tier == self.tier else []

    def has_capacity(self, item):
        return self.inflight.get(item.fingerprint, 0) < item.max_parallel

    def try_acquire(self, item):
        if not self.has_capacity(item):
            return False
        self.inflight[item.fingerprint] = self.inflight.get(item.fingerprint, 0) + 1
        return True

    def release(self, item):
        self.inflight.pop(item.fingerprint, None)

    def observe(self, **data):
        self.observations.append(data)

    def block_route(self, *_):
        pass

    def route_score(self, item, _model, _body):
        return self.route_scores.get(item.id, 0)


def completed(text="recovered"):
    return {
        "text": text,
        "chunks": [text],
        "actual_model": "gpt-5.6-luna",
        "latency_ms": 1.0,
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "request_id": "safe-request-id",
        "cost": 0.0,
    }


def production_like_schema():
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["reply_markdown", "needs_fresh_search", "propositions", "actions"],
        "properties": {
            "reply_markdown": {"type": "string", "minLength": 600},
            "needs_fresh_search": {"type": "boolean"},
            "propositions": {"type": "array", "minItems": 2, "items": {"$ref": "#/$defs/proposition"}},
            "actions": {"type": "array", "minItems": 1, "items": {"oneOf": [
                {"$ref": "#/$defs/analysis_request"},
                {"$ref": "#/$defs/workflow_proposal"},
            ]}},
        },
        "$defs": {
            "source_span": {
                "type": "object", "additionalProperties": False,
                "required": ["message_id", "start", "end", "quote"],
                "properties": {
                    "message_id": {"type": "string"},
                    "start": {"type": "integer", "minimum": 0},
                    "end": {"type": "integer", "minimum": 0},
                    "quote": {"type": "string", "minLength": 1},
                },
            },
            "proposition": {
                "type": "object", "additionalProperties": False,
                "required": ["kind", "subject", "confidence", "source_span"],
                "properties": {
                    "kind": {"type": "string", "enum": ["user_fact", "user_view", "external_claim", "ai_inference"]},
                    "subject": {"type": "string"},
                    "confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
                    "source_span": {"$ref": "#/$defs/source_span"},
                },
            },
            "analysis_request": {
                "type": "object", "additionalProperties": False,
                "required": ["action_type", "subject", "time_scope", "source_span"],
                "properties": {
                    "action_type": {"type": "string", "const": "analysis.request"},
                    "subject": {"type": "string", "minLength": 1},
                    "time_scope": {"type": "string", "minLength": 1},
                    "source_span": {"$ref": "#/$defs/source_span"},
                },
            },
            "workflow_proposal": {
                "type": "object", "additionalProperties": False,
                "required": ["action_type", "category", "evidence", "source_span"],
                "properties": {
                    "action_type": {"type": "string", "const": "workflow.propose"},
                    "category": {"type": "string", "enum": ["workflow_efficiency", "search_coverage", "investment_method"]},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "source_span": {"$ref": "#/$defs/source_span"},
                },
            },
        },
    }


def production_like_body():
    return {
        # One compact token per repetition keeps the HTTP body below aiohttp's
        # default 1 MiB limit while preserving the production input-token class.
        "prompt": "e " * 72_000,
        "output_schema": production_like_schema(),
        "intellect": "standard",
        "effort": "medium",
        "deadline_ms": 500,
        "output_token_limit": 6_000,
    }


def test_strict_schema_prompt_only_reinforces_structured_requests():
    prompt = "private user request"
    assert strict_schema_prompt(prompt, None) == prompt
    reinforced = strict_schema_prompt(prompt, production_like_schema())
    assert reinforced.startswith(prompt)
    assert "exactly the declared object properties" in reinforced
    assert "validates without repair" in reinforced
    assert '"additionalProperties":false' in reinforced
    assert '"reply_markdown"' in reinforced
    repaired = strict_schema_prompt(prompt, production_like_schema(), "At propositions.0, omit undeclared properties: rationale.")
    assert "Generate the entire JSON again from scratch" in repaired


def test_strict_prompt_resurfaces_instruction_buried_in_large_request_envelope():
    schema = {"type": "object"}
    instruction = "Cover every blocking requirement and web_read every supplied discovery."
    prompt = json.dumps({
        "instruction": "Return only one JSON object matching output_schema.",
        "output_schema": schema,
        "input": {"instruction": instruction, "research_discoveries": [{"snippet": "x" * 100_000}]},
    })
    reinforced = strict_schema_prompt(prompt, schema)
    assert reinforced.endswith(instruction)
    assert reinforced.rfind(instruction) > reinforced.rfind("Exact JSON Schema")


def test_research_plan_prompt_repeats_exact_requirement_keys_and_gaps():
    schema = {"type": "object"}
    prompt = json.dumps({
        "output_schema": schema,
        "input": {
            "stage": "research_plan", "instruction": "Return a valid plan.",
            "evidence_contract": {"requirements": [{"key": "turnover_compare"}, {"key": "portfolio_close"}]},
            "coverage_gaps": ["research_plan_missing_requirement:turnover_compare"],
        },
    })
    reinforced = strict_schema_prompt(prompt, schema)
    assert "Use only these exact requirement_key values" in reinforced
    assert "turnover_compare, portfolio_close" in reinforced
    assert "research_plan_missing_requirement:turnover_compare" in reinforced
    assert "never split, suffix, prefix, or invent" in reinforced


def test_research_plan_missing_read_repair_forbids_more_search_and_repeats_candidate_urls():
    schema = {"type": "object"}
    prompt = json.dumps({
        "output_schema": schema,
        "input": {
            "stage": "research_plan", "instruction": "Return a valid plan.",
            "evidence_contract": {"requirements": [
                {"key": "current_market_state"}, {"key": "material_events_and_counterevidence"},
            ]},
            "coverage_gaps": [
                "research_plan_missing_verification_read:material_events_and_counterevidence",
            ],
            "research_discoveries": [
                {"requirement_key": "material_events_and_counterevidence", "url": "https://news.test/a"},
                {"requirement_key": "material_events_and_counterevidence", "url": "https://news.test/b"},
                {"requirement_key": "current_market_state", "url": "https://market.test/close"},
            ],
        },
    })

    reinforced = strict_schema_prompt(prompt, schema)

    assert "Do not use web_search for material_events_and_counterevidence" in reinforced
    assert "https://news.test/a" in reinforced
    assert "https://news.test/b" in reinforced
    assert "https://market.test/close" not in reinforced.rsplit("missing verification-read repair", 1)[-1]


def test_open_object_schema_uses_prompt_enforcement_without_mutating_contract():
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["operation", "source_reference"],
        "properties": {
            "operation": {"enum": ["search", "complete"]},
            "source_reference": {"type": ["object", "null"]},
        },
    }
    original = json.loads(json.dumps(schema))
    assert provider_native_schema(schema, "openai") is None
    assert provider_native_schema(schema, "claude") is None
    assert schema == original


async def test_memory_research_open_object_avoids_rejected_native_schema_and_stays_strict():
    captured = {}
    valid = '{"operation":"complete","query":null,"episode_id":null,"url":null,"source_reference":null}'

    async def compatible_responses(request):
        captured.update(await request.json())
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        completed = {"type": "response.completed", "response": {
            "id": "memory-schema-compatible", "model": captured["model"],
            "output_text": valid, "usage": {},
        }}
        await response.write(("data: " + json.dumps(completed) + "\n\n").encode())
        await response.write_eof()
        return response

    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["operation", "query", "episode_id", "url", "source_reference"],
        "properties": {
            "operation": {"enum": ["search", "complete"]},
            "query": {"type": ["string", "null"]},
            "episode_id": {"type": ["string", "null"]},
            "url": {"type": ["string", "null"]},
            "source_reference": {"type": ["object", "null"]},
        },
    }
    upstream = web.Application()
    upstream.router.add_post("/v1/responses", compatible_responses)
    server = TestServer(upstream)
    await server.start_server()
    item = provider(0, secret="open-object-secret")
    item.base_url = str(server.make_url("/")).rstrip("/")
    try:
        output = await invoke_stream(item, {
            "prompt": "synthetic frozen memory is sufficient; choose complete",
            "deadline_ms": 500, "output_schema": schema, "output_token_limit": 2000,
        })
    finally:
        await server.close()

    assert "text" not in captured
    assert "authoritative" in captured["input"]
    assert output["text"] == valid


async def test_claude_compat_payload_reinforces_and_still_validates_strict_schema():
    captured = {}
    valid = '{"answer":"bounded"}'

    async def compatible_chat(request):
        captured.update(await request.json())
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        event = {
            "model": captured["model"],
            "choices": [{"delta": {"content": valid}, "finish_reason": "stop"}],
        }
        await response.write(("data: " + json.dumps(event) + "\n\n").encode())
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response

    upstream = web.Application()
    upstream.router.add_post("/v1/chat/completions", compatible_chat)
    server = TestServer(upstream)
    await server.start_server()
    item = provider(0, secret="claude-compat-secret")
    item.base_url = str(server.make_url("/")).rstrip("/")
    item.provider_type = "claude"
    item.models = ["claude-sonnet-5"]
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["answer"], "properties": {"answer": {"type": "string"}},
    }
    try:
        output = await invoke_stream(item, {
            "prompt": "original structured request", "deadline_ms": 500,
            "output_schema": schema, "output_token_limit": 6000,
        })
    finally:
        await server.close()

    assert output["text"] == valid
    assert captured["messages"][0]["content"].startswith("original structured request")
    assert "exactly the declared object properties" in captured["messages"][0]["content"]
    assert captured["response_format"]["json_schema"] == {
        "name": "broker_output", "strict": True, "schema": schema,
    }


async def test_codex_payload_uses_supported_union_without_mutating_caller_schema():
    captured = {}
    span = {"message_id": "message-1", "start": 0, "end": 8, "quote": "evidence"}
    valid = json.dumps({
        "reply_markdown": "x" * 600,
        "needs_fresh_search": False,
        "propositions": [
            {"kind": "user_fact", "subject": "fact", "confidence": 1, "source_span": span},
            {"kind": "ai_inference", "subject": "inference", "confidence": .5, "source_span": span},
        ],
        "actions": [{
            "action_type": "analysis.request", "subject": "market",
            "time_scope": "next session", "source_span": span,
        }],
    })

    async def compatible_responses(request):
        captured.update(await request.json())
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        completed = {
            "type": "response.completed",
            "response": {
                "id": "schema-compatible", "model": captured["model"],
                "output_text": valid, "usage": {},
            },
        }
        await response.write(("data: " + json.dumps(completed) + "\n\n").encode())
        await response.write_eof()
        return response

    upstream = web.Application()
    upstream.router.add_post("/v1/responses", compatible_responses)
    server = TestServer(upstream)
    await server.start_server()
    item = provider(0, secret="codex-compatible-secret")
    item.base_url = str(server.make_url("/")).rstrip("/")
    schema = production_like_schema()
    try:
        output = await invoke_stream(item, {
            "prompt": "structured request", "deadline_ms": 500,
            "output_schema": schema, "output_token_limit": 6000,
        })
    finally:
        await server.close()

    outbound_items = captured["text"]["format"]["schema"]["properties"]["actions"]["items"]
    assert "oneOf" not in outbound_items
    assert outbound_items["anyOf"] == [
        {"$ref": "#/$defs/analysis_request"},
        {"$ref": "#/$defs/workflow_proposal"},
    ]
    assert "oneOf" in schema["properties"]["actions"]["items"]
    assert output["text"] == valid


async def test_multiple_first_token_timeouts_continue_to_later_success():
    items = [provider(index, secret=f"super-secret-{index}") for index in range(3)]
    store = FakeStore(items)

    async def invoke(item, _body):
        if item.id < 2:
            raise AttemptFailure("first_token_timeout", diagnostic={
                "endpoint": "/responses", "first_event_timeout_ms": 20,
                "authorization": item.api_key, "base_url": item.base_url,
            })
        return completed()

    with patch("provider_broker.upstream.random.sample", side_effect=lambda values, k: list(values)):
        result = await route(
            store, "standard", {"prompt": "never-return-this", "deadline_ms": 500},
            parallel_cap=1, invoker=invoke, hedge_delay_ms=0,
            first_event_timeout_ms=20, route_attempt_budget=3,
        )

    assert result["text"] == "recovered"
    assert [attempt["attempt"] for attempt in result["attempts"]] == [1, 2, 3]
    assert [attempt["status"] for attempt in result["attempts"]] == [
        "first_token_timeout", "first_token_timeout", "completed",
    ]
    serialized = json.dumps(result["attempts"])
    assert "super-secret" not in serialized
    assert "secret-host" not in serialized
    assert "private-header" not in serialized
    assert "never-return-this" not in serialized


async def test_long_structured_request_uses_remaining_budget_for_bounded_retry():
    items = [provider(index, secret=f"long-secret-{index}") for index in range(5)]
    for item in items:
        item.models = ["gpt-5.6-terra"]
    store = FakeStore(items, tier="smart")
    statuses = [
        "first_token_timeout", "unavailable", "stream_incomplete",
        "structured_output_invalid", "transport_failed",
    ]
    calls = {item.id: 0 for item in items}
    span = {"message_id": "synthetic", "start": 0, "end": 18, "quote": "synthetic evidence"}
    valid = json.dumps({
        "reply_markdown": "Detailed synthetic assessment. " * 30,
        "needs_fresh_search": False,
        "propositions": [
            {"kind": "external_claim", "subject": "event-a", "confidence": .8, "source_span": span},
            {"kind": "ai_inference", "subject": "impact-b", "confidence": .6, "source_span": span},
        ],
        "actions": [{
            "action_type": "analysis.request", "subject": "synthetic market",
            "time_scope": "next session", "source_span": span,
        }],
    })

    async def invoke(item, request_body):
        calls[item.id] += 1
        assert request_body["_first_event_timeout_ms"] == 40
        if item.id == 0 and calls[item.id] == 2:
            validate_structured_output(valid, structured_schema(request_body), None, {"endpoint": "/responses"})
            return completed(valid) | {"actual_model": "gpt-5.6-terra"}
        raise AttemptFailure(statuses[item.id], diagnostic={"endpoint": "/responses"})

    with patch("provider_broker.upstream.random.sample", side_effect=lambda values, k: list(values)):
        result = await route(
            store, "smart", production_like_body(), parallel_cap=1, invoker=invoke,
            hedge_delay_ms=0, first_event_timeout_ms=20, route_attempt_budget=6,
        )

    assert result["text"] == valid
    assert [attempt["attempt"] for attempt in result["attempts"]] == list(range(1, 7))
    assert [attempt["status"] for attempt in result["attempts"]] == statuses + ["completed"]
    assert calls == {0: 2, 1: 1, 2: 1, 3: 1, 4: 1}
    serialized = json.dumps(result["attempts"])
    assert "long-secret" not in serialized
    assert "e e e" not in serialized


async def test_next_price_band_can_fill_idle_capacity_before_stalled_band_finishes():
    low = provider(0, secret="low-band-secret")
    high = provider(1, secret="high-band-secret")
    high.price_group = 500
    store = FakeStore([low, high])
    low_started = asyncio.Event()

    async def invoke(item, _body):
        if item.id == 0:
            low_started.set()
            await asyncio.Event().wait()
        await low_started.wait()
        return completed()

    with patch("provider_broker.upstream.random.sample", side_effect=lambda values, k: list(values)):
        result = await route(
            store, "standard", {"prompt": "band-spill", "deadline_ms": 100},
            parallel_cap=2, invoker=invoke, hedge_delay_ms=0, route_attempt_budget=2,
        )

    assert result["text"] == "recovered"
    assert [attempt["status"] for attempt in result["attempts"]] == ["cancelled", "completed"]
    assert store.inflight == {}


async def test_all_failed_long_structured_stream_returns_503_with_every_budgeted_attempt(tmp_path, monkeypatch):
    items = [provider(index, secret=f"all-failed-secret-{index}") for index in range(3)]
    for item in items:
        item.models = ["gpt-5.6-terra"]
    store = FakeStore(items, tier="smart")
    statuses = ["first_token_timeout", "stream_incomplete", "structured_output_invalid"]

    async def invoke(item, _body):
        raise AttemptFailure(statuses[item.id], diagnostic={"endpoint": "/responses"})

    async def routed(_store, tier, body, _parallel_cap, **_kwargs):
        with patch("provider_broker.upstream.random.sample", side_effect=lambda values, k: list(values)):
            return await route(
                store, tier, body, parallel_cap=1, invoker=invoke,
                hedge_delay_ms=0, first_event_timeout_ms=20, route_attempt_budget=5,
            )

    settings = Settings(
        database_path=tmp_path / "broker.sqlite3",
        admin_token="admin-secret",
        session_secret="session-secret",
        encryption_key="MDEyMzQ1Njc4OWFiY2RlZg==",
    )
    app = create_app(settings)
    client = TestClient(TestServer(app))
    monkeypatch.setattr("provider_broker.app.route", routed)
    await client.start_server()
    try:
        response = await client.post("/v1/generate/stream", json=production_like_body())
        response_body = await response.json()
    finally:
        await client.close()

    assert response.status == 503
    attempts = response_body["attempts"]
    assert [attempt["attempt"] for attempt in attempts] == [1, 2, 3, 4, 5]
    assert [attempt["status"] for attempt in attempts] == [
        "first_token_timeout", "stream_incomplete", "structured_output_invalid",
        "first_token_timeout", "stream_incomplete",
    ]
    assert store.inflight == {}
    serialized = json.dumps(attempts)
    assert "all-failed-secret" not in serialized
    assert "e e e" not in serialized


async def test_partial_sse_has_a_bounded_idle_lease_after_first_text():
    async def partial_stream(request):
        payload = await request.json()
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(b'data: {"type":"response.output_text.delta","delta":"{"}\n\n')
        await asyncio.sleep(.08)
        try:
            await response.write((
                'data: {"type":"response.completed","response":{"id":"late","model":"'
                + payload["model"] + '","usage":{}}}\n\n'
            ).encode())
            await response.write_eof()
        except ConnectionResetError:
            pass
        return response

    upstream = web.Application()
    upstream.router.add_post("/v1/responses", partial_stream)
    server = TestServer(upstream)
    await server.start_server()
    item = provider(0, secret="idle-secret")
    item.base_url = str(server.make_url("/")).rstrip("/")
    try:
        with pytest.raises(AttemptFailure) as failure:
            await invoke_stream(item, {
                "prompt": "idle", "deadline_ms": 500,
                "_first_event_timeout_ms": 20,
                "_stream_idle_timeout_ms": 20,
                "_attempt_timeout_ms": 200,
            })
    finally:
        await server.close()

    assert failure.value.status == "stream_incomplete"
    assert failure.value.diagnostic["idle_timeout_ms"] == 20


async def test_reasoning_events_keep_a_valid_long_generation_alive_without_becoming_output():
    valid = '{"answer":"qualified"}'

    async def reasoning_then_output(request):
        payload = await request.json()
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(b'event: response.reasoning_summary_text.delta\ndata: {"delta":"private reasoning"}\n\n')
        await asyncio.sleep(.04)
        await response.write((
            'event: response.output_text.delta\ndata: {"delta":'
            + json.dumps(valid) + '}\n\n'
        ).encode())
        await asyncio.sleep(.04)
        await response.write((
            'event: response.completed\ndata: {"response":{"id":"reasoning","model":"'
            + payload["model"] + '","usage":{}}}\n\n'
        ).encode())
        await response.write_eof()
        return response

    upstream = web.Application()
    upstream.router.add_post("/v1/responses", reasoning_then_output)
    server = TestServer(upstream)
    await server.start_server()
    item = provider(0, secret="reasoning-secret")
    item.base_url = str(server.make_url("/")).rstrip("/")
    try:
        output = await invoke_stream(item, {
            "prompt": "reasoning", "deadline_ms": 500,
            "output_schema": {
                "type": "object", "additionalProperties": False,
                "required": ["answer"], "properties": {"answer": {"type": "string"}},
            },
            "_first_event_timeout_ms": 20,
            "_stream_idle_timeout_ms": 80,
            "_attempt_timeout_ms": 200,
        })
    finally:
        await server.close()

    assert output["text"] == valid
    assert "private reasoning" not in output["text"]


async def test_buffered_text_in_completed_event_is_a_valid_complete_stream():
    valid = '{"answer":"buffered"}'

    async def completed_only(request):
        payload = await request.json()
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        completed = {
            "type": "response.completed",
            "response": {
                "id": "buffered", "model": payload["model"], "output_text": valid,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        }
        await response.write(("data: " + json.dumps(completed) + "\n\n").encode())
        await response.write_eof()
        return response

    upstream = web.Application()
    upstream.router.add_post("/v1/responses", completed_only)
    server = TestServer(upstream)
    await server.start_server()
    item = provider(0, secret="buffered-secret")
    item.base_url = str(server.make_url("/")).rstrip("/")
    try:
        output = await invoke_stream(item, {
            "prompt": "buffered", "deadline_ms": 500,
            "output_schema": {
                "type": "object", "additionalProperties": False,
                "required": ["answer"], "properties": {"answer": {"type": "string"}},
            },
            "_first_event_timeout_ms": 20,
            "_stream_idle_timeout_ms": 80,
            "_attempt_timeout_ms": 200,
        })
    finally:
        await server.close()

    assert output["text"] == valid
    assert output["request_id"] == "buffered"


async def test_invalid_structured_output_continues_to_later_valid_candidate():
    items = [provider(index, secret=f"key-{index}") for index in range(2)]
    store = FakeStore(items)
    body = {
        "prompt": json.dumps({
            "instruction": "Return JSON",
            "output_schema": {
                "type": "object", "required": ["healthy"],
                "properties": {"healthy": {"type": "boolean"}},
                "additionalProperties": False,
            },
        }),
        "deadline_ms": 500,
    }

    async def invoke(item, request_body):
        text = "ordinary prose" if item.id == 0 else '{"healthy":true}'
        validate_structured_output(text, structured_schema(request_body), None, {"endpoint": "/responses"})
        return completed(text)

    with patch("provider_broker.upstream.random.sample", side_effect=lambda values, k: list(values)):
        result = await route(
            store, "standard", body, parallel_cap=1, invoker=invoke,
            hedge_delay_ms=0, route_attempt_budget=3,
        )

    assert result["text"] == '{"healthy":true}'
    assert [attempt["status"] for attempt in result["attempts"]] == [
        "structured_output_invalid", "structured_output_invalid", "completed",
    ]
    assert result["attempts"][0]["diagnostic"]["structured_error_kind"] == "json_decode"


@pytest.mark.parametrize(
    ("text", "schema", "finish_reason", "expected"),
    [
        ("not-json", {"type": "object"}, None, "structured_output_invalid"),
        ("{}", {"type": "not-a-json-schema-type"}, None, "structured_schema_invalid"),
        ("{", {"type": "object"}, "length", "output_truncated"),
    ],
)
def test_structured_failure_statuses_are_distinct(text, schema, finish_reason, expected):
    with pytest.raises(AttemptFailure, match=expected) as failure:
        validate_structured_output(text, schema, finish_reason, {"endpoint": "/responses"})
    assert failure.value.status == expected


def test_additional_properties_failure_exposes_only_a_bounded_repair_note():
    schema = {
        "type": "object", "additionalProperties": False,
        "properties": {"allowed": {"type": "string"}},
    }
    with pytest.raises(AttemptFailure) as failure:
        validate_structured_output(
            '{"allowed":"private value","rationale":"also private"}', schema, "stop",
            {"endpoint": "/chat/completions"},
        )
    assert failure.value.status == "structured_output_invalid"
    assert failure.value.diagnostic["unexpected_properties"] == ["rationale"]
    assert failure.value.repair_note == "At the root object, omit undeclared properties: rationale."
    assert "private value" not in failure.value.repair_note
    assert "also private" not in failure.value.repair_note


def test_repair_attempt_can_only_remove_forbidden_properties_then_revalidates():
    schema = {
        "type": "object", "additionalProperties": False, "required": ["answer"],
        "properties": {
            "answer": {"type": "string"},
            "nested": {
                "type": "object", "additionalProperties": False, "required": ["kept"],
                "properties": {"kept": {"type": "boolean"}},
            },
        },
    }
    rendered, removed = validate_structured_output(
        '{"answer":"meaning","extra":"drop","nested":{"kept":true,"type":"drop"}}',
        schema, "stop", {"endpoint": "/chat/completions"}, normalize_additional=True,
    )
    assert json.loads(rendered) == {"answer": "meaning", "nested": {"kept": True}}
    assert removed == ["extra", "nested.type"]
    with pytest.raises(AttemptFailure, match="structured_output_invalid"):
        validate_structured_output(
            '{"answer":7,"extra":"drop"}', schema, "stop",
            {"endpoint": "/chat/completions"}, normalize_additional=True,
        )


async def test_schema_repair_retry_is_a_separate_audited_attempt():
    item = provider(0, secret="repair-secret")
    store = FakeStore([item])
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["answer"], "properties": {"answer": {"type": "string"}},
    }
    bodies = []

    async def invoke(_item, request_body):
        bodies.append(request_body)
        if len(bodies) == 1:
            validate_structured_output(
                '{"answer":"valid meaning","rationale":"undeclared"}', schema, "stop",
                {"endpoint": "/chat/completions"},
            )
        return completed('{"answer":"valid meaning"}')

    with patch("provider_broker.upstream.random.sample", side_effect=lambda values, k: list(values)):
        result = await route(
            store, "standard", {"prompt": "repair", "output_schema": schema, "deadline_ms": 500},
            parallel_cap=1, invoker=invoke, hedge_delay_ms=0, route_attempt_budget=2,
            response_reserve_ms=0,
        )

    assert result["text"] == '{"answer":"valid meaning"}'
    assert [row["status"] for row in result["attempts"]] == ["structured_output_invalid", "completed"]
    assert "_structured_repair_note" not in bodies[0]
    assert bodies[1]["_structured_repair_note"] == "At the root object, omit undeclared properties: rationale."


async def test_schema_repair_retry_precedes_untried_unhealthy_candidates():
    items = [provider(index, secret=f"repair-priority-{index}") for index in range(3)]
    store = FakeStore(items)
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["answer"], "properties": {"answer": {"type": "string"}},
    }
    order = []

    async def invoke(item, request_body):
        order.append(item.id)
        if item.id == 0 and "_structured_repair_note" not in request_body:
            validate_structured_output(
                '{"answer":"meaning","type":"undeclared"}', schema, "stop",
                {"endpoint": "/chat/completions"},
            )
        if item.id == 0:
            return completed('{"answer":"meaning"}')
        raise AttemptFailure("unavailable", diagnostic={"http_status": 503})

    with patch("provider_broker.upstream.random.sample", side_effect=lambda values, k: list(values)):
        result = await route(
            store, "standard", {"prompt": "repair", "output_schema": schema, "deadline_ms": 500},
            parallel_cap=1, invoker=invoke, hedge_delay_ms=0, route_attempt_budget=4,
        )

    assert result["text"] == '{"answer":"meaning"}'
    assert order == [0, 0]
    assert [row["status"] for row in result["attempts"]] == ["structured_output_invalid", "completed"]


async def test_json_decode_repair_precedes_untried_unhealthy_candidates():
    items = [provider(index, secret=f"json-repair-priority-{index}") for index in range(3)]
    store = FakeStore(items)
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["answer"], "properties": {"answer": {"type": "string"}},
    }
    order = []

    async def invoke(item, request_body):
        order.append(item.id)
        if item.id == 0 and "_structured_repair_note" not in request_body:
            validate_structured_output("ordinary prose", schema, "stop", {"endpoint": "/chat/completions"})
        if item.id == 0:
            return completed('{"answer":"recovered"}')
        raise AttemptFailure("unavailable", diagnostic={"http_status": 503})

    with patch("provider_broker.upstream.random.sample", side_effect=lambda values, k: list(values)):
        result = await route(
            store, "standard", {"prompt": "repair", "output_schema": schema, "deadline_ms": 500},
            parallel_cap=1, invoker=invoke, hedge_delay_ms=0, route_attempt_budget=2,
        )

    assert result["text"] == '{"answer":"recovered"}'
    assert order == [0, 0]
    assert [row["status"] for row in result["attempts"]] == ["structured_output_invalid", "completed"]


async def test_recent_exact_winner_is_ranked_before_random_same_band_candidates():
    unhealthy = provider(0, secret="unhealthy-first")
    exact_winner = provider(1, secret="exact-winner")
    store = FakeStore([unhealthy, exact_winner])
    store.route_scores = {0: -10, 1: 1000}
    order = []

    async def invoke(item, _body):
        order.append(item.id)
        if item.id == 1:
            return completed()
        raise AttemptFailure("unavailable", diagnostic={"http_status": 503})

    with patch("provider_broker.upstream.random.sample", side_effect=lambda values, k: list(values)):
        result = await route(
            store, "standard", {"prompt": "same exact request", "deadline_ms": 500},
            parallel_cap=1, invoker=invoke, hedge_delay_ms=0,
            route_attempt_budget=1, response_reserve_ms=0,
        )

    assert result["text"] == "recovered"
    assert order == [1]


async def test_recent_exact_winner_gets_one_priority_transient_retry():
    exact_winner = provider(0, secret="exact-winner")
    untried = provider(1, secret="untried")
    store = FakeStore([exact_winner, untried])
    store.route_scores = {0: 1000, 1: 0}
    order = []

    async def invoke(item, _body):
        order.append(item.id)
        if item.id == 0 and order.count(0) == 1:
            raise AttemptFailure("first_token_timeout", diagnostic={"first_event_timeout_ms": 60_000})
        if item.id == 0:
            return completed()
        raise AttemptFailure("unavailable", diagnostic={"http_status": 503})

    with patch("provider_broker.upstream.random.sample", side_effect=lambda values, k: list(values)):
        result = await route(
            store, "standard", {"prompt": "same exact request", "deadline_ms": 500},
            parallel_cap=1, invoker=invoke, hedge_delay_ms=0,
            route_attempt_budget=2, response_reserve_ms=0,
        )

    assert result["text"] == "recovered"
    assert order == [0, 0]
    assert [row["status"] for row in result["attempts"]] == ["first_token_timeout", "completed"]
    assert [row["diagnostic"]["queue_kind"] for row in result["attempts"]] == ["primary", "priority_retry"]


async def test_exact_winner_can_repair_truncation_after_one_transient_retry():
    exact_winner = provider(0, secret="exact-winner")
    store = FakeStore([exact_winner])
    store.route_scores = {0: 1000}
    calls = 0

    async def invoke(_item, _body):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise AttemptFailure("first_token_timeout")
        if calls == 2:
            raise AttemptFailure(
                "output_truncated",
                repair_note="The prior response reached the output limit. Regenerate concise, complete JSON.",
            )
        return completed()

    result = await route(
        store, "standard", {"prompt": "same exact request", "deadline_ms": 500},
        parallel_cap=1, invoker=invoke, hedge_delay_ms=0,
        route_attempt_budget=3, response_reserve_ms=0,
    )

    assert result["text"] == "recovered"
    assert [row["diagnostic"]["queue_kind"] for row in result["attempts"]] == [
        "primary", "priority_retry", "repair",
    ]


async def test_slow_upstream_cancellation_cannot_delay_winner_response():
    winner, slow_loser = provider(0, secret="winner"), provider(1, secret="slow-loser")
    store = FakeStore([winner, slow_loser])
    loser_started = asyncio.Event()

    async def invoke(item, _body):
        if item.id == 0:
            await loser_started.wait()
            return completed()
        loser_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(.2)
            raise

    started = asyncio.get_running_loop().time()
    result = await route(
        store, "standard", {"prompt": "winner", "deadline_ms": 500},
        parallel_cap=2, invoker=invoke, hedge_delay_ms=0,
        route_attempt_budget=2, response_reserve_ms=0, cancel_grace_ms=10,
    )
    elapsed = asyncio.get_running_loop().time() - started

    assert result["text"] == "recovered"
    assert elapsed < .1
    assert sorted(row["status"] for row in result["attempts"]) == ["cancelled", "completed"]
    await asyncio.sleep(.25)


def test_truncated_structured_output_requests_a_concise_complete_repair():
    with pytest.raises(AttemptFailure) as failure:
        validate_structured_output(
            '{"answer":"partial', {"type": "object"}, "length", {"output_token_limit": 6000},
        )

    assert failure.value.status == "output_truncated"
    assert "concise" in failure.value.repair_note
    assert "complete JSON" in failure.value.repair_note


def test_route_score_uses_only_safe_exact_request_shape_history(tmp_path):
    store = Store(tmp_path / "broker.sqlite3", b"x" * 32)
    exact_winner = provider(0, secret="exact-winner")
    invalid = provider(1, secret="invalid")
    schema = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}
    body = {"prompt": "same exact request", "output_schema": schema}
    diagnostic = {
        "prompt_sha256": hashlib.sha256(body["prompt"].encode()).hexdigest(),
        "schema_sha256": hashlib.sha256(json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
    }

    def observe(item, status, actual_model):
        store.observe(
            fingerprint=item.fingerprint, requested_model="gpt-5.6-luna", actual_model=actual_model,
            tier="standard", effort="medium", success=int(status == "completed"), latency_ms=1,
            error=None, status=status, input_tokens=None, output_tokens=None, cost=None,
            request_id=f"safe-{item.id}", diagnostic_json=json.dumps(diagnostic),
        )

    observe(exact_winner, "completed", "gpt-5.6-luna")
    observe(invalid, "structured_output_invalid", None)

    assert store.route_score(exact_winner, "gpt-5.6-luna", body) == 1000
    assert store.route_score(invalid, "gpt-5.6-luna", body) == -100


async def test_route_reserves_time_to_serialize_and_transmit_before_client_deadline():
    item = provider(0, secret="deadline-reserve-secret")
    store = FakeStore([item])

    async def stalled(_provider, _body):
        await asyncio.Event().wait()

    started = asyncio.get_running_loop().time()
    with pytest.raises(UpstreamFailure) as failure:
        await route(
            store, "standard", {"prompt": "deadline", "deadline_ms": 300},
            parallel_cap=1, invoker=stalled, hedge_delay_ms=0,
            route_attempt_budget=1, response_reserve_ms=30,
        )
    elapsed_ms = (asyncio.get_running_loop().time() - started) * 1000

    assert 240 <= elapsed_ms < 300
    assert failure.value.attempts[0]["status"] == "timed_out"
    assert failure.value.attempts[0]["diagnostic"]["client_deadline_ms"] == 300
    assert failure.value.attempts[0]["diagnostic"]["response_reserve_ms"] == 30
    assert failure.value.attempts[0]["diagnostic"]["route_budget_ms"] == 270
    assert store.inflight == {}


async def test_invalid_schema_is_rejected_by_broker_before_contacting_provider():
    item = provider(0, secret="must-not-be-used")
    with patch("provider_broker.upstream.ClientSession") as session:
        with pytest.raises(AttemptFailure) as failure:
            await invoke_stream(item, {
                "prompt": "structured", "output_schema": {"type": "not-a-json-schema-type"},
            })
    assert failure.value.status == "structured_schema_invalid"
    session.assert_not_called()


async def test_cancelling_route_cancels_active_upstreams_and_releases_capacity():
    items = [provider(index, secret=f"cancel-secret-{index}") for index in range(2)]
    store = FakeStore(items)
    both_started = asyncio.Event()
    started = 0

    async def stalled(_provider, _body):
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await asyncio.Event().wait()

    with patch("provider_broker.upstream.random.sample", side_effect=lambda values, k: list(values)):
        task = asyncio.create_task(route(
            store, "standard", {"prompt": "cancel", "deadline_ms": 1000},
            parallel_cap=2, invoker=stalled, hedge_delay_ms=0,
            route_attempt_budget=2,
        ))
        await asyncio.wait_for(both_started.wait(), timeout=.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert store.inflight == {}
    assert [row["status"] for row in store.observations] == ["cancelled", "cancelled"]


async def test_budget_and_deadline_exhaustion_returns_503_with_complete_attempts(tmp_path, monkeypatch):
    settings = Settings(
        database_path=tmp_path / "broker.sqlite3",
        admin_token="admin-secret",
        session_secret="session-secret",
        encryption_key="MDEyMzQ1Njc4OWFiY2RlZg==",
    )
    app = create_app(settings)
    client = TestClient(TestServer(app))
    await client.start_server()
    items = [provider(index, secret=f"deadline-secret-{index}") for index in range(4)]
    store = FakeStore(items)

    async def slow(_provider, _body):
        await asyncio.sleep(1)
        return completed()

    async def routed(_store, tier, body, _parallel_cap, **_kwargs):
        with patch("provider_broker.upstream.random.sample", side_effect=lambda values, k: list(values)):
            return await route(
                store, tier, body, parallel_cap=2, invoker=slow,
                hedge_delay_ms=0, first_event_timeout_ms=10,
                route_attempt_budget=2,
            )

    monkeypatch.setattr("provider_broker.app.route", routed)
    try:
        response = await client.post("/v1/generate", json={
            "prompt": "deadline-prompt-secret", "intellect": "standard", "deadline_ms": 25,
        })
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 503
    assert [attempt["attempt"] for attempt in body["attempts"]] == [1, 2]
    assert [attempt["status"] for attempt in body["attempts"]] == ["timed_out", "timed_out"]
    serialized = json.dumps(body["attempts"])
    assert "deadline-secret" not in serialized
    assert "deadline-prompt-secret" not in serialized
    assert store.inflight == {}
