import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from provider_broker.db import Store
from provider_broker.source import register_cpa


def _store(tmp_path):
    return Store(tmp_path / "broker.sqlite3", b"0123456789abcdef")


@pytest.mark.asyncio
async def test_registration_uses_authenticated_cpa_config_and_is_idempotent():
    state = {"config": {"providers": []}, "puts": []}

    async def get_config(request):
        assert request.headers["Authorization"] == "Bearer management-secret"
        assert request.headers["X-Management-Key"] == "management-secret"
        return web.json_response(state["config"])

    async def put_config(request):
        assert request.headers["Authorization"] == "Bearer management-secret"
        assert request.headers["Content-Type"].startswith("application/yaml")
        raw = await request.read()
        state["puts"].append(raw)
        state["config"] = json.loads(raw)
        return web.json_response({"updated": True})

    app = web.Application()
    app.router.add_get("/v0/management/config", get_config)
    app.router.add_put("/v0/management/config.yaml", put_config)
    server = TestServer(app)
    await server.start_server()
    provider = {
        "name": "DeepSeek production",
        "provider_type": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "registration-secret",
        "models": ["deepseek-v4-flash"],
    }
    try:
        first = await register_cpa(str(server.make_url("")), "management-secret", {"providers": [provider]})
        second = await register_cpa(str(server.make_url("")), "management-secret", {"providers": [provider]})
    finally:
        await server.close()

    assert first == {"added": 1, "updated": 0, "registered": 1}
    assert second == {"added": 0, "updated": 1, "registered": 1}
    assert len(state["puts"]) == 2
    assert json.loads(state["puts"][-1])["providers"][0]["keys"][0]["key"] == "registration-secret"


@pytest.mark.asyncio
async def test_registration_uses_cpa_openai_compatibility_management_shape():
    state = {"rows": [], "puts": []}

    async def get_compatibility(request):
        assert request.headers["Authorization"] == "Bearer management-secret"
        return web.json_response({"openai-compatibility": state["rows"]})

    async def put_compatibility(request):
        assert request.headers["Content-Type"] == "application/json"
        payload = await request.json()
        assert isinstance(payload, list)
        state["puts"].append(payload)
        state["rows"] = payload
        return web.json_response({"updated": True})

    app = web.Application()
    app.router.add_get("/v0/management/openai-compatibility", get_compatibility)
    app.router.add_put("/v0/management/openai-compatibility", put_compatibility)
    server = TestServer(app)
    await server.start_server()
    provider = {
        "name": "DeepInfra production", "provider_type": "deepinfra",
        "base_url": "https://api.deepinfra.com/v1/openai", "api_key": "registration-secret",
        "models": ["deepseek-v4-flash-0731"],
    }
    try:
        result = await register_cpa(str(server.make_url("")), "management-secret", {"providers": [provider]})
    finally:
        await server.close()

    assert result == {"added": 1, "updated": 0, "registered": 1}
    assert state["puts"] == [[{
        "name": "DeepInfra production", "base-url": "https://api.deepinfra.com/v1/openai",
        "api-key-entries": [{"api-key": "registration-secret"}],
        "models": [{"name": "deepseek-v4-flash-0731", "alias": "deepseek-v4-flash-0731"}],
    }]]


def test_model_refresh_keeps_provider_identity_policy_and_health(tmp_path):
    store = _store(tmp_path)
    first = {
        "name": "Qwen site", "site_name": "qwen-site", "base_url": "https://qwen.example/v1",
        "api_key": "qwen-secret", "provider_type": "openai_chat",
        "models": ["gpt-5.6-luna"], "inventory_status": "available",
    }
    store.replace_source_snapshot([first], "2026-09-12T00:00:00+00:00")
    fingerprint = store.inventory()[0]["fingerprint"]
    assert store.update_policy(fingerprint, {"enabled": False, "multiplier": 0.45, "note": "operator block", "calibrated": False})
    store.record_health(fingerprint, "gpt-5.6-luna", success=False, real=True, immediate_open=True)
    store.block_route(fingerprint, "gpt-5.6-luna")

    refreshed = first | {"models": ["gpt-5.6-luna", "gpt-5.6-terra"]}
    store.replace_source_snapshot([refreshed], "2026-09-12T01:00:00+00:00")

    row = store.inventory()[0]
    assert row["fingerprint"] == fingerprint
    assert row["models"] == ["gpt-5.6-luna", "gpt-5.6-terra"]
    assert (row["enabled"], row["multiplier"], row["note"], row["calibrated"]) == (False, 0.45, "operator block", False)
    assert store.health(fingerprint, "gpt-5.6-luna")["state"] == "open"
    assert store.providers("standard") == []


def test_inventory_mask_does_not_decrypt_provider_key(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.replace_source_snapshot([{
        "name": "Masked", "base_url": "https://masked.example/v1", "api_key": "masked-secret",
        "models": ["gpt-5.6-luna"], "inventory_status": "available",
    }], "2026-09-12T00:00:00+00:00")
    monkeypatch.setattr(store, "_decrypt", lambda _value: (_ for _ in ()).throw(AssertionError("inventory decrypted a key")))

    store.replace_source_snapshot([{
        "name": "Masked", "base_url": "https://masked.example/v1", "api_key": "masked-secret",
        "models": ["gpt-5.6-terra"], "inventory_status": "available",
    }], "2026-09-12T01:00:00+00:00")
    inventory = store.inventory()

    assert inventory[0]["api_key_mask"] == "mas***ret"
    raw = store.conn.execute("SELECT api_key FROM source_provider").fetchone()[0]
    assert b"masked-secret" not in raw
