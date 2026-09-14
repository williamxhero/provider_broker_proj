import json
from pathlib import Path

from provider_broker.db import Store
from provider_broker.upstream import estimate_cost_details


def make_store(tmp_path: Path) -> Store:
    return Store(tmp_path / "spec82.sqlite", b"0" * 32)


def add_inventory(store: Store, entries: list[dict]) -> list[str]:
    store.replace_source_snapshot(entries, "2026-09-14T00:00:00Z")
    return [row[0] for row in store.conn.execute(
        "SELECT fingerprint FROM source_provider ORDER BY id"
    ).fetchall()]


def test_runtime_cost_uses_cny_output_rate_for_all_token_classes_and_mapping_multiplier():
    details = estimate_cost_details(
        "canonical-model",
        {
            "input_tokens": 900,
            "output_tokens": 100,
            "input_tokens_details": {"cached_tokens": 900},
        },
        multiplier=1.5,
        pricing={"priced": True, "output_price_cny": 7.0, "input_price": 0.01, "cache_price": 0.02, "output_price": 100.0},
    )

    assert details == {"cost": 0.0105, "reason": None}
    assert estimate_cost_details(
        "canonical-model", {"input_tokens": 1, "output_tokens": 1},
        pricing={"priced": True, "input_price": 1, "cache_price": 1, "output_price": 1},
    ) == {"cost": None, "reason": "model price is unknown"}
    assert estimate_cost_details(
        "canonical-model", {"input_tokens": True, "output_tokens": 1},
        pricing={"priced": True, "output_price_cny": 7.0},
    ) == {"cost": None, "reason": "token usage is incomplete"}


def test_key_resources_merge_by_last_three_hostname_labels_and_keep_windowed_totals(tmp_path):
    store = make_store(tmp_path)
    first, second = add_inventory(store, [
        {
            "name": "First", "base_url": "https://a.route.vendor.example.com:443/v1",
            "api_key": "secret-one", "provider_type": "openai", "models": ["gpt-5.6-luna"],
            "inventory_status": "available",
        },
        {
            "name": "Second", "base_url": "https://b.route.vendor.example.com/alt",
            "api_key": "secret-two", "provider_type": "openai", "models": ["gpt-5.6-luna"],
            "inventory_status": "available",
        },
    ])
    store.update_policy(first, {"note": "first note", "max_parallel": 2})
    store.update_policy(second, {"note": "second note", "max_parallel": 3, "enabled": False})
    store.observe(
        fingerprint=first, requested_model="gpt-5.6-luna", actual_model="gpt-5.6-luna",
        tier="standard", success=1, latency_ms=100, input_tokens=10,
        output_tokens=5, cost=0.5, currency="CNY", status="completed",
    )
    store.observe(
        fingerprint=second, requested_model="gpt-5.6-luna", actual_model="gpt-5.6-luna",
        tier="standard", success=1, latency_ms=120, input_tokens=20,
        output_tokens=5, cost=0.75, currency="CNY", status="completed",
    )

    resources = store.api_key_resources("24h")

    assert len(resources) == 2
    assert {item["normalized_hostname"] for item in resources} == {"vendor.example.com"}
    by_key = {item["api_key_mask"]: item for item in resources}
    assert {item["status"] for item in resources} == {"enabled", "disabled"}
    assert {item["max_parallel"] for item in resources} == {2, 3}
    assert by_key["sec***one"]["total_tokens"] == 15
    assert by_key["sec***two"]["total_tokens"] == 25
    assert by_key["sec***one"]["fee_buckets"]["CNY"]["total_fee"] == 0.5
    assert by_key["sec***two"]["fee_buckets"]["CNY"]["total_fee"] == 0.75
    assert "secret-one" not in json.dumps(resources)
    assert store.api_key_resource(first, "24h")["fingerprint"] == first


def test_stage_resources_are_one_row_per_stage_with_stable_output_price_bands(tmp_path):
    store = make_store(tmp_path)
    store.create_canonical_model("standard-low", stage="standard", family="Low family")
    store.create_canonical_model("standard-high", stage="standard", family="High family")
    fingerprint = add_inventory(store, [{
        "name": "Stage key", "base_url": "https://stage.vendor.example.com/v1",
        "api_key": "stage-secret", "provider_type": "openai",
        "models": ["standard-low", "standard-high"], "inventory_status": "available",
    }])[0]
    provider_id = store.conn.execute(
        "SELECT id FROM pricing_provider WHERE provider_key='openai'"
    ).fetchone()[0]
    store.upsert_provider_model_price(provider_id=provider_id, model_id="standard-low", output_price_cny=5)
    store.upsert_provider_model_price(provider_id=provider_id, model_id="standard-high", output_price_cny=15)
    store.observe(
        fingerprint=fingerprint, requested_model="standard-low", actual_model="standard-low",
        tier="standard", success=1, latency_ms=100, input_tokens=10,
        output_tokens=2, cost=0.06, currency="CNY", status="completed",
    )
    store.observe(
        fingerprint=fingerprint, requested_model="standard-high", actual_model="standard-high",
        tier="standard", success=0, latency_ms=200, input_tokens=20,
        output_tokens=3, cost=0.345, currency="CNY", status="completed",
    )

    stages = store.stage_resources("24h")
    standard = next(item for item in stages if item["stage"] == "standard")

    assert len([item for item in stages if item["stage"] == "standard"]) == 1
    assert standard["model"] == "UNKNOWN"
    assert standard["models"] == ["standard-high", "standard-low"]
    assert standard["callable_key_count"] == 1
    assert standard["total_tokens"] == 35
    assert standard["price_boundary_cny"] == 10.0
    assert standard["price_bands"]["low"]["output_prices_cny"] == [5.0]
    assert standard["price_bands"]["low"]["models"] == ["standard-low"]
    assert standard["price_bands"]["high"]["output_prices_cny"] == [15.0]
    assert standard["price_bands"]["high"]["models"] == ["standard-high"]
    assert standard["price_bands"]["unknown"]["models"] == []
