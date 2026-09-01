import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from provider_broker.app import create_app
from provider_broker.settings import Settings
from provider_broker.upstream import (
    AttemptFailure,
    invoke_stream,
    route,
    structured_schema,
    validate_structured_output,
)


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
        "output_token_limit": 2_000,
    }


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
            hedge_delay_ms=0, route_attempt_budget=2,
        )

    assert result["text"] == '{"healthy":true}'
    assert [attempt["status"] for attempt in result["attempts"]] == [
        "structured_output_invalid", "completed",
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
