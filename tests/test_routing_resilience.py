import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
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
    def __init__(self, providers):
        self.items = providers
        self.inflight = {}
        self.observations = []

    def providers(self, tier):
        return self.items if tier == "standard" else []

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
