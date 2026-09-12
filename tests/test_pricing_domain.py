import json
import sqlite3
from pathlib import Path

import pytest

from provider_broker.db import Store


def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "pricing.sqlite", b"0" * 32)


def test_pricing_entities_keep_model_metadata_separate_from_provider_prices(tmp_path):
    db = store(tmp_path)

    provider_id = db.create_pricing_provider(
        "relay-acme", provider_type="relay", name="Acme Relay", multiplier=1.25
    )
    db.create_canonical_model("model-a", stage="smart", family="Family A")
    price_id = db.upsert_provider_model_price(
        provider_id=provider_id,
        model_id="model-a",
        source_kind="relay",
        input_price=1.0,
        cache_price=0.2,
        output_price=4.0,
        currency="USD",
        source_url="https://prices.example/acme",
        source_evidence="published table",
        verified_at="2026-09-12T00:00:00+00:00",
    )

    assert price_id > 0
    assert db.canonical_models()["model-a"] == {
        "id": "model-a",
        "stage": "smart",
        "family": "Family A",
        "active": True,
    }
    assert db.provider_model_prices()[0] | {"provider_id": provider_id, "model_id": "model-a"} | {
        "source_kind": "relay",
        "currency": "USD",
        "active": True,
    }
    db.upsert_provider_model_price(
        provider_id=provider_id, model_id="model-a", source_kind="relay",
        input_price=1.1, cache_price=0.2, output_price=4.0, currency="USD",
    )
    assert db.conn.execute(
        "SELECT source FROM configuration_event WHERE setting=?",
        ("pricing:%s:model-a" % provider_id,),
    ).fetchone()[0] == "pricing"
    columns = {row[1] for row in db.conn.execute("PRAGMA table_info(canonical_model)")}
    assert not columns.intersection({"input_price", "cache_price", "output_price"})


def test_active_provider_model_price_is_unique_but_can_be_deactivated(tmp_path):
    db = store(tmp_path)
    provider_id = db.create_pricing_provider("direct-a", provider_type="direct", name="Direct A")
    db.create_canonical_model("model-a", stage="standard", family="A")
    db.upsert_provider_model_price(
        provider_id=provider_id, model_id="model-a", source_kind="direct",
        input_price=1, cache_price=0.1, output_price=2, currency="USD",
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_provider_model_price(
            provider_id=provider_id, model_id="model-a", source_kind="direct",
            input_price=2, cache_price=0.2, output_price=3, currency="USD",
        )
    db.deactivate_provider_model_price(provider_id, "model-a")
    assert db.insert_provider_model_price(
        provider_id=provider_id, model_id="model-a", source_kind="direct",
        input_price=2, cache_price=0.2, output_price=3, currency="USD",
    ) > 0


def test_relay_binding_requires_active_objects_and_protects_referenced_rows(tmp_path):
    db = store(tmp_path)
    relay_id = db.create_pricing_provider("relay-a", provider_type="relay", name="Relay A")
    base_id = db.create_pricing_provider("direct-a", provider_type="direct", name="Direct A")
    db.create_canonical_model("model-a", stage="smart", family="A")
    db.insert_provider_model_price(
        provider_id=base_id, model_id="model-a", source_kind="direct",
        input_price=1, cache_price=0.1, output_price=2, currency="USD",
    )
    db.bind_relay_price(relay_id, "model-a", base_id, "model-a")

    assert db.relay_price_bindings()[0]["benchmark_provider_id"] == base_id
    assert db.deactivate_pricing_provider(base_id) is True
    with pytest.raises(sqlite3.IntegrityError):
        db.bind_relay_price(relay_id, "model-a", base_id, "model-a")
    with pytest.raises(ValueError, match="referenced"):
        db.delete_pricing_provider(base_id)
    with pytest.raises(ValueError, match="referenced"):
        db.delete_canonical_model("model-a")


def test_legacy_migration_is_idempotent_lossless_and_does_not_reprice_observations(tmp_path):
    db = store(tmp_path)
    db.conn.execute(
        "INSERT INTO source_provider(fingerprint,name,base_url,api_key,provider_type,models_json,source_json,synced_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("legacy-fp", "Legacy", "https://relay.example/v1", db._encrypt("secret"), "relay", json.dumps(["legacy-model"]), json.dumps({"provider_type": "relay"}), "2026-09-12T00:00:00Z"),
    )
    db.conn.execute("INSERT INTO policy(fingerprint,multiplier) VALUES(?,?)", ("legacy-fp", 1.7))
    db.conn.execute(
        "INSERT INTO model_catalog(model,family,intellect,input_price,cache_price,output_price,currency) VALUES(?,?,?,?,?,?,?)",
        ("legacy-model", "Legacy Family", "standard", 3.0, 0.0, 8.0, "USD"),
    )
    db.conn.execute(
        "INSERT INTO observation(fingerprint,requested_model,actual_model,tier,effort,success,latency_ms,error,status,cost) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("legacy-fp", "legacy-model", "legacy-model", "standard", "low", 1, 10, None, "completed", 123.45),
    )
    db.conn.commit()

    first = db.migrate_pricing()
    second = db.migrate_pricing()
    prices = [row for row in db.provider_model_prices() if row["model_id"] == "legacy-model"]

    assert first["migrated"] is True
    assert second["migrated"] is False
    assert len(prices) == 2
    assert any(row["source_kind"] == "official" and row["input_price"] == 3.0 for row in prices)
    assert any(row["source_kind"] == "legacy-migration" and row["unpriced"] is True for row in prices)
    assert db.conn.execute("SELECT cost FROM observation").fetchone()[0] == 123.45
    assert db.conn.execute("SELECT multiplier FROM pricing_provider WHERE provider_type='relay'").fetchone()[0] == 1.0
    assert db.conn.execute("SELECT count(*) FROM pricing_migration").fetchone()[0] == 1
