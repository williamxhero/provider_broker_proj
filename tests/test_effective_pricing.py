import json
from pathlib import Path
from types import SimpleNamespace

from provider_broker.db import Store
from provider_broker.upstream import estimate_cost_details, price_bands


def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "effective-pricing.sqlite", b"0" * 32)


def test_effective_key_price_is_mapping_specific_and_applies_mapping_multiplier(tmp_path):
    db = store(tmp_path)
    provider_id = db.create_pricing_provider("openai-a", provider_type="openai", multiplier=1.5)
    db.create_canonical_model("model-a", stage="smart", family="A")
    db.insert_provider_model_price(
        provider_id=provider_id, model_id="model-a", output_price_cny=4.0,
    )
    db.conn.execute(
        "INSERT INTO source_provider(fingerprint,name,base_url,api_key,provider_type,models_json,source_json,synced_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("direct-key", "Direct", "https://direct.example/v1", db._encrypt("secret"), "openai",
         json.dumps(["model-a"]), json.dumps({"provider_type": "openai"}), "2026-09-14T00:00:00Z"),
    )
    db.conn.execute("INSERT INTO policy(fingerprint) VALUES(?)", ("direct-key",))
    db.create_key_model_mapping("direct-key", "model-a", target_provider_id=provider_id, multiplier=1.5)

    resolved = db.effective_key_pricing("direct-key", "MODEL-A")

    assert {key: resolved[key] for key in (
        "model", "stage", "currency", "input_price", "cache_price", "output_price",
        "output_price_cny", "blended_price", "multiplier", "source", "priced", "reason",
    )} == {
        "model": "model-a", "stage": "smart", "currency": "CNY",
        "input_price": 6.0, "cache_price": 6.0, "output_price": 6.0,
        "output_price_cny": 6.0, "blended_price": 6.0, "multiplier": 1.5,
        "source": "pricing", "priced": True, "reason": None,
    }


def test_effective_cross_provider_key_price_uses_mapping_multiplier(tmp_path):
    db = store(tmp_path)
    key_provider_id = db.create_pricing_provider("anthropic-a", provider_type="anthropic")
    benchmark_id = db.create_pricing_provider("openai-b", provider_type="openai")
    db.create_canonical_model("relay-model", stage="smart", family="A")
    db.create_canonical_model("benchmark-model", stage="smart", family="A")
    db.insert_provider_model_price(
        provider_id=benchmark_id, model_id="benchmark-model", output_price_cny=8.0,
    )
    db.insert_provider_model_price(
        provider_id=key_provider_id, model_id="relay-model", output_price_cny=8.0,
    )
    db.conn.execute(
        "INSERT INTO source_provider(fingerprint,name,base_url,api_key,provider_type,models_json,source_json,synced_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("relay-key", "Anthropic", "https://anthropic.example/v1", db._encrypt("secret"), "anthropic",
         json.dumps(["relay-model"]), json.dumps({"provider_type": "anthropic"}), "2026-09-14T00:00:00Z"),
    )
    db.conn.execute("INSERT INTO policy(fingerprint,multiplier) VALUES(?,?)", ("relay-key", 1.25))
    db.create_key_model_mapping(
        "relay-key", "relay-model", target_provider_id=benchmark_id,
        target_model_id="benchmark-model", multiplier=1.25,
    )

    resolved = db.effective_key_pricing("relay-key", "relay-model")
    missing = db.effective_key_pricing("relay-key", "missing-model")

    assert (resolved["currency"], resolved["source"], resolved["multiplier"]) == ("CNY", "pricing", 1.25)
    assert (resolved["input_price"], resolved["cache_price"], resolved["output_price"]) == (10.0, 10.0, 10.0)
    assert resolved["priced"] is True
    assert missing["priced"] is False and missing["blended_price"] is None and missing["reason"]


def test_estimate_cost_details_uses_final_component_prices_and_cached_tokens():
    details = estimate_cost_details(
        "model-a",
        {"input_tokens": 900, "output_tokens": 100, "input_tokens_details": {"cached_tokens": 400}},
        pricing={"output_price_cny": 6.0, "priced": True},
    )

    assert details == {"cost": 0.006, "reason": None}


def test_price_bands_never_compare_different_currencies_or_unpriced_candidates():
    providers = [
        SimpleNamespace(id=1, price_group=100, price_currency="USD", price_comparable=True),
        SimpleNamespace(id=2, price_group=200, price_currency="USD", price_comparable=True),
        SimpleNamespace(id=3, price_group=1, price_currency="CNY", price_comparable=True),
        SimpleNamespace(id=4, price_group=None, price_currency=None, price_comparable=False),
    ]

    bands = price_bands(providers)

    assert [[item.id for item in band] for band in bands] == [[1], [2], [3], [4]]


def test_route_candidate_audit_preserves_effective_price_projection(tmp_path):
    db = store(tmp_path)
    db.route_started("route-1", "smart", "request-1")
    db.record_candidate(
        "route-1", fingerprint="key-1", model="model-a", site_id="site-a", eligible=True,
        initial_rank=1, stage="smart", currency="USD", multiplier=1.25,
        price=3.75, price_source="pricing", price_comparable=True,
    )

    candidate = db.route_detail("route-1")["candidates"][0]

    assert candidate["stage"] == "smart"
    assert candidate["currency"] == "USD"
    assert candidate["multiplier"] == 1.25
    assert candidate["price"] == 3.75
    assert candidate["price_source"] == "pricing"
    assert candidate["price_comparable"] == 1


def test_store_provider_candidates_use_effective_key_price_instead_of_global_catalog(tmp_path):
    db = store(tmp_path)
    db.replace_source_snapshot([{
        "name": "Direct A", "base_url": "https://direct.example/v1", "api_key": "secret",
        "provider_type": "openai", "models": ["gpt-5.6-terra"],
        "source": {"provider_type": "direct", "inventory_status": "available"},
    }], "2026-09-12T00:00:00Z")
    row = db.conn.execute("SELECT fingerprint FROM source_provider").fetchone()
    provider_id = db.create_pricing_provider("openai-runtime", provider_type="openai", multiplier=1.4)
    db.insert_provider_model_price(
        provider_id=provider_id, model_id="gpt-5.6-terra", output_price_cny=3.0,
    )
    db.conn.execute("UPDATE source_provider SET pricing_provider_id=?", (provider_id,))
    db.upsert_key_model_mapping(
        row["fingerprint"], "gpt-5.6-terra", target_provider_id=provider_id, multiplier=1.4,
    )
    db.conn.execute("UPDATE policy SET calibrated=1 WHERE fingerprint=?", (row["fingerprint"],))
    db.conn.commit()

    candidate = db.providers("smart")[0]

    assert candidate.pricing["input_price"] == 4.2
    assert candidate.pricing["output_price"] == 4.2
    assert candidate.pricing["currency"] == "CNY"
    assert candidate.price_currency == "CNY" and candidate.price_comparable is True
    assert candidate.price_group == int(candidate.pricing["blended_price"] * 100000)
