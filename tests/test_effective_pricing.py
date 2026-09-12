from pathlib import Path
from types import SimpleNamespace

from provider_broker.db import Store
from provider_broker.upstream import estimate_cost_details, price_bands


def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "effective-pricing.sqlite", b"0" * 32)


def test_effective_direct_price_is_provider_specific_and_applies_provider_multiplier(tmp_path):
    db = store(tmp_path)
    provider_id = db.create_pricing_provider("direct-a", provider_type="direct", multiplier=1.5)
    db.create_canonical_model("model-a", stage="smart", family="A")
    db.insert_provider_model_price(
        provider_id=provider_id, model_id="model-a", source_kind="direct",
        input_price=1.0, cache_price=0.2, output_price=4.0, currency="USD",
    )

    resolved = db.effective_pricing(provider_id, "MODEL-A")

    assert resolved == {
        "model": "model-a", "stage": "smart", "currency": "USD",
        "input_price": 1.5, "cache_price": 0.3, "output_price": 6.0,
        "blended_price": 4.908, "multiplier": 1.5,
        "source": "direct", "priced": True, "reason": None,
    }


def test_effective_relay_price_uses_bound_benchmark_and_unknown_is_not_free(tmp_path):
    db = store(tmp_path)
    relay_id = db.create_pricing_provider("relay-a", provider_type="relay", multiplier=1.25)
    benchmark_id = db.create_pricing_provider("direct-a", provider_type="direct")
    db.create_canonical_model("relay-model", stage="smart", family="A")
    db.create_canonical_model("benchmark-model", stage="smart", family="A")
    db.insert_provider_model_price(
        provider_id=benchmark_id, model_id="benchmark-model", source_kind="direct",
        input_price=2.0, cache_price=0.5, output_price=8.0, currency="CNY",
    )
    db.bind_relay_price(relay_id, "relay-model", benchmark_id, "benchmark-model")

    resolved = db.effective_pricing(relay_id, "relay-model")
    missing = db.effective_pricing(relay_id, "missing-model")

    assert (resolved["currency"], resolved["source"], resolved["multiplier"]) == ("CNY", "relay", 1.25)
    assert (resolved["input_price"], resolved["cache_price"], resolved["output_price"]) == (2.5, 0.625, 10.0)
    assert resolved["priced"] is True
    assert missing["priced"] is False and missing["blended_price"] is None and missing["reason"]


def test_estimate_cost_details_uses_final_component_prices_and_cached_tokens():
    details = estimate_cost_details(
        "model-a",
        {"input_tokens": 900, "output_tokens": 100, "input_tokens_details": {"cached_tokens": 400}},
        pricing={"input_price": 1.5, "cache_price": 0.3, "output_price": 6.0, "priced": True},
    )

    assert details == {"cost": 0.00147, "reason": None}


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
        price=3.75, price_source="relay", price_comparable=True,
    )

    candidate = db.route_detail("route-1")["candidates"][0]

    assert candidate["stage"] == "smart"
    assert candidate["currency"] == "USD"
    assert candidate["multiplier"] == 1.25
    assert candidate["price"] == 3.75
    assert candidate["price_source"] == "relay"
    assert candidate["price_comparable"] == 1


def test_store_provider_candidates_use_effective_key_price_instead_of_global_catalog(tmp_path):
    db = store(tmp_path)
    db.replace_source_snapshot([{
        "name": "Direct A", "base_url": "https://direct.example/v1", "api_key": "secret",
        "provider_type": "openai", "models": ["gpt-5.6-terra"],
        "source": {"provider_type": "direct", "inventory_status": "available"},
    }], "2026-09-12T00:00:00Z")
    row = db.conn.execute("SELECT fingerprint FROM source_provider").fetchone()
    provider_id = db.create_pricing_provider("direct-runtime", provider_type="direct", multiplier=1.4)
    db.insert_provider_model_price(
        provider_id=provider_id, model_id="gpt-5.6-terra", source_kind="direct",
        input_price=1.0, cache_price=0.1, output_price=3.0, currency="EUR",
    )
    db.conn.execute("UPDATE source_provider SET pricing_provider_id=?", (provider_id,))
    db.conn.execute("UPDATE policy SET calibrated=1 WHERE fingerprint=?", (row["fingerprint"],))
    db.conn.commit()

    candidate = db.providers("smart")[0]

    assert candidate.pricing["input_price"] == 1.4
    assert candidate.pricing["output_price"] == 4.2
    assert candidate.pricing["currency"] == "EUR"
    assert candidate.price_currency == "EUR" and candidate.price_comparable is True
    assert candidate.price_group == int(candidate.pricing["blended_price"] * 100000)
