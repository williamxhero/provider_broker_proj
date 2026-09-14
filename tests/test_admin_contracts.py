import base64
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from provider_broker.app import create_app
from provider_broker.db import API_KEY_RESOURCE_FIELDS, STAGE_RESOURCE_FIELDS, Store
from provider_broker.settings import Settings


@pytest.fixture
async def admin_client(tmp_path):
    app = create_app(Settings(
        database_path=tmp_path / "contracts.sqlite3",
        admin_token="admin-secret",
        session_secret="session-secret",
        encryption_key=base64.b64encode(b"x" * 32).decode(),
    ))
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    yield client
    await client.close()


def seed_inventory(store: Store) -> list[str]:
    store.replace_source_snapshot([
        {"name": "Alpha", "base_url": "https://api.example.com/v1", "api_key": "secret-alpha",
         "provider_type": "openai", "models": ["gpt-5.6-luna"], "inventory_status": "available"},
        {"name": "Beta", "base_url": "https://api.example.com:443/alt", "api_key": "secret-beta",
         "provider_type": "relay", "models": ["gpt-5.6-luna"], "inventory_status": "available"},
    ], "2026-09-14T00:00:00Z")
    rows = store.conn.execute("SELECT fingerprint FROM source_provider ORDER BY id").fetchall()
    return [row["fingerprint"] for row in rows]


async def test_key_and_stage_contracts_are_allowlisted_and_windowed(admin_client):
    store = admin_client.app["store"]
    first, second = seed_inventory(store)
    store.observe(fingerprint=first, requested_model="gpt-5.6-luna", actual_model="gpt-5.6-luna",
                  tier="standard", success=1, latency_ms=100, input_tokens=10,
                  output_tokens=5, cost=.25, currency="USD")
    store.observe(fingerprint=second, requested_model="gpt-5.6-luna", actual_model="gpt-5.6-luna",
                  tier="standard", success=0, latency_ms=300, input_tokens=20,
                  output_tokens=5, cost=2, currency="CNY")
    assert store.update_policy(second, {"enabled": False})

    keys_response = await admin_client.get("/admin/v1/providers?window=1h")
    assert keys_response.status == 200
    keys = (await keys_response.json())["providers"]
    assert all(set(item) == API_KEY_RESOURCE_FIELDS for item in keys)
    assert keys[0]["normalized_hostname"] == "api.example.com"
    assert all("secret-" not in str(item) and "base_url" not in item and "models" not in item for item in keys)
    assert all(item["window"] == "1h" for item in keys)

    stages_response = await admin_client.get("/admin/v1/stages?window=24h")
    assert stages_response.status == 200
    stage = next(item for item in (await stages_response.json())["items"] if item["model"] == "gpt-5.6-luna")
    assert set(stage) == STAGE_RESOURCE_FIELDS
    assert stage["provider_types"] == ["openai", "relay"]
    assert stage["callable_key_count"] == 1
    assert stage["total_tokens"] == 40
    assert set(stage["fee_buckets"]) == {"CNY", "USD"}
    assert stage["fee_buckets"]["USD"]["total_fee"] == .25
    assert stage["fee_buckets"]["CNY"]["total_fee"] == 2

    models = await admin_client.get("/admin/v1/models?include_inactive=true")
    model = next(item for item in (await models.json())["items"] if item["id"] == "gpt-5.6-luna")
    assert set(model) == {"id", "stage", "family", "active"}

    assert (await admin_client.get("/admin/v1/stages?window=2h")).status == 400
    assert (await admin_client.patch(f"/admin/v1/keys/{first}", json={"multiplier": 2})).status == 400
    updated = await admin_client.patch(f"/admin/v1/keys/{first}", json={"note": "safe note", "max_parallel": 4})
    assert updated.status == 200
    assert (await updated.json())["note"] == "safe note"


async def test_stage_test_targets_enabled_pairs_and_returns_refresh_evidence(admin_client):
    store = admin_client.app["store"]
    first, second = seed_inventory(store)
    assert store.update_policy(second, {"enabled": False})
    with patch("provider_broker.app.run_probe", new=AsyncMock(return_value=[
        {"fingerprint": first, "model": "gpt-5.6-luna", "state": "succeeded"},
    ])) as run_probe:
        response = await admin_client.post("/admin/v1/stages/test", json={
            "stage": "standard", "model": "gpt-5.6-luna",
        })
    assert response.status == 200
    assert (await response.json())["items"][0]["state"] == "succeeded"
    targets = run_probe.await_args.kwargs["targets"]
    assert [target.fingerprint for target in targets] == [first]
