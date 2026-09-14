import json

import pytest

from provider_broker.db import Store
from provider_broker.upstream import price_bands


def make_store(tmp_path):
    return Store(tmp_path / "spec71.sqlite", b"0" * 32)


def test_only_key_mapping_multiplier_changes_authoritative_cost(tmp_path):
    db = make_store(tmp_path)
    provider_id = db.create_pricing_provider("spec71-openai", provider_type="openai", name="Spec 71")
    db.create_canonical_model("spec71-model", stage="smart", family="Spec 71")
    db.insert_provider_model_price(
        provider_id=provider_id, model_id="spec71-model", output_price_cny=8,
        source_name="Spec 71 table", source_url="https://prices.example/spec71",
        source_evidence="fixed endpoint table", verified_at="2026-09-14T00:00:00Z",
    )
    fingerprint = Store.fingerprint("https://spec71.example/v1", "secret")
    db.conn.execute(
        "INSERT INTO source_provider(fingerprint,name,base_url,api_key,provider_type,models_json,source_json,synced_at) VALUES(?,?,?,?,?,?,?,?)",
        (fingerprint, "Spec 71 key", "https://spec71.example/v1", db._encrypt("secret"), "openai",
         json.dumps(["spec71-model"]), json.dumps({"inventory_status": "available"}), "2026-09-14T00:00:00Z"),
    )
    db.conn.execute("INSERT INTO policy(fingerprint) VALUES(?)", (fingerprint,))
    db.create_key_model_mapping(fingerprint, "spec71-model", target_provider_id=provider_id, multiplier=1.5)
    db.conn.execute("UPDATE policy SET multiplier=99 WHERE fingerprint=?", (fingerprint,))
    db.conn.execute("UPDATE pricing_provider SET multiplier=99 WHERE id=?", (provider_id,))
    db.conn.execute("UPDATE provider_model_price SET multiplier=99 WHERE provider_id=?", (provider_id,))
    db.conn.commit()

    resolved = db.effective_key_pricing(fingerprint, "spec71-model")

    assert resolved["multiplier"] == 1.5
    assert resolved["input_price"] == 12.0
    assert resolved["cache_price"] == 12.0
    assert resolved["output_price"] == 12.0
    assert "multiplier" not in db.pricing_providers()[0]
    assert "multiplier" not in db.provider_model_prices()[0]


def test_legacy_price_rows_and_relay_bindings_are_non_authoritative(tmp_path):
    db = make_store(tmp_path)
    provider_id = db.create_pricing_provider("spec71-legacy-carrier", provider_type="openai")
    db.create_canonical_model("spec71-legacy-model", stage="standard", family="Spec 71")
    db.conn.execute(
        "INSERT INTO provider_model_price(provider_id,model_id,source_kind,input_price,cache_price,output_price,multiplier,currency,legacy,unpriced,active) VALUES(?,?,?,?,?,?,?,?,?,?,1)",
        (provider_id, "spec71-legacy-model", "direct", 99, 9, 999, 1, "USD", 1, 0),
    )
    db.conn.commit()

    assert db.effective_pricing(provider_id, "spec71-legacy-model")["priced"] is False
    assert not [row for row in db.provider_model_prices() if row["model_id"] == "spec71-legacy-model"]
    assert db.relay_price_bindings() == []
    with pytest.raises(ValueError, match="relay price bindings have been removed"):
        db.bind_relay_price(provider_id, "spec71-legacy-model", provider_id, "spec71-legacy-model")


def test_unpriced_provider_model_is_not_zero_cost(tmp_path):
    db = make_store(tmp_path)
    relay_id = db.create_pricing_provider("spec71-anthropic", provider_type="anthropic")
    db.create_canonical_model("spec71-relay-model", stage="smart", family="Spec 71")
    db.insert_provider_model_price(
        provider_id=relay_id, model_id="spec71-relay-model", output_price_cny=0, unpriced=True,
        source_name="awaiting price quote", source_evidence="intentionally unpriced",
    )

    resolved = db.effective_pricing(relay_id, "spec71-relay-model")

    assert resolved["priced"] is False
    assert resolved["reason"] == "provider model price is explicitly unpriced"
    assert resolved["input_price"] is None


def test_accounting_keeps_all_release_windows_and_currency_buckets(tmp_path):
    db = make_store(tmp_path)
    db.observe(
        fingerprint="spec71-key", requested_model="gpt-5.6-luna", actual_model="gpt-5.6-luna",
        tier="standard", success=1, input_tokens=10, output_tokens=5,
        cost=1.25, currency="USD", status="completed",
    )
    db.observe(
        fingerprint="spec71-key", requested_model="gpt-5.6-luna", actual_model="gpt-5.6-luna",
        tier="standard", success=1, input_tokens=20, output_tokens=5,
        cost=2.5, currency="CNY", status="completed",
    )

    for window in ("1h", "24h", "7d", "30d"):
        report = db.accounting(window=window)
        assert report["window"] == window
        assert set(report["fee_buckets"]) == {"CNY", "USD"}
        assert report["fees_by_currency"] == {"CNY": 2.5, "USD": 1.25}


def test_price_bands_keep_currency_boundaries_and_unknowns_visible():
    from types import SimpleNamespace

    providers = [
        SimpleNamespace(id=1, price_group=100, price_currency="USD", price_comparable=True),
        SimpleNamespace(id=2, price_group=110, price_currency="USD", price_comparable=True),
        SimpleNamespace(id=3, price_group=10, price_currency="CNY", price_comparable=True),
        SimpleNamespace(id=4, price_group=None, price_currency=None, price_comparable=False),
    ]

    bands = price_bands(providers)

    assert [[item.id for item in band] for band in bands] == [[1], [2], [3], [4]]
