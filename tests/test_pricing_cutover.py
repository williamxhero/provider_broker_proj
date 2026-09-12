import json

import pytest

from provider_broker.db import Store


def make_store(tmp_path):
    return Store(tmp_path / "cutover.sqlite", b"0" * 32)


def test_provider_model_multiplier_is_the_runtime_rate_key(tmp_path):
    db = make_store(tmp_path)
    provider_id = db.create_pricing_provider("direct-cutover", provider_type="direct", multiplier=9.0)
    db.create_canonical_model("cutover-model", stage="smart", family="Cutover")
    db.insert_provider_model_price(
        provider_id=provider_id, model_id="cutover-model", source_kind="direct",
        input_price=1.0, cache_price=0.2, output_price=4.0, currency="USD", multiplier=1.25,
    )

    db.conn.execute("UPDATE policy SET multiplier=7.0")
    db.conn.commit()

    resolved = db.effective_pricing(provider_id, "cutover-model")

    assert resolved["multiplier"] == 1.25
    assert (resolved["input_price"], resolved["cache_price"], resolved["output_price"]) == (1.25, 0.25, 5.0)


def test_legacy_catalog_write_is_rejected_when_provider_prices_are_ambiguous(tmp_path):
    db = make_store(tmp_path)
    db.create_canonical_model("shared-model", stage="smart", family="Shared")
    first = db.create_pricing_provider("direct-one", provider_type="direct")
    second = db.create_pricing_provider("direct-two", provider_type="direct")
    for provider_id, price in ((first, 1.0), (second, 2.0)):
        db.insert_provider_model_price(
            provider_id=provider_id, model_id="shared-model", source_kind="direct",
            input_price=price, cache_price=0.1, output_price=price * 2, currency="USD", multiplier=1.0,
        )

    with pytest.raises(ValueError, match="ambiguous"):
        db.update_catalog("shared-model", {
            "family": "Shared", "intellect": "smart",
            "official_input_price": 3.0, "official_cache_price": 0.3, "official_output_price": 6.0,
        })
    assert db.legacy_catalog_projection("shared-model") == {
        "status": "conflict", "provider_count": 2,
    }


def test_pricing_startup_health_reports_migration_and_integrity_gates(tmp_path):
    db = make_store(tmp_path)
    health = db.pricing_health()

    assert health["migration"]["status"] == "completed"
    assert health["migration"]["version"] == 1
    assert health["duplicate_active"] == 0
    assert health["dangling_bindings"] == 0
    assert "unpriced_active" in health
    assert health["startup_ready"] is True


def test_migration_carries_legacy_multiplier_into_each_provider_model_row(tmp_path):
    db = make_store(tmp_path)
    db.conn.execute(
        "INSERT INTO source_provider(fingerprint,name,base_url,api_key,provider_type,models_json,source_json,synced_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("legacy-cutover", "Legacy", "https://legacy.example/v1", db._encrypt("secret"), "openai",
         json.dumps(["gpt-5.6-luna"]), json.dumps({"provider_type": "openai"}), "2026-09-12T00:00:00Z"),
    )
    db.conn.execute("INSERT INTO policy(fingerprint,multiplier) VALUES(?,?)", ("legacy-cutover", 1.7))
    db.conn.commit()

    db.migrate_pricing()

    row = db.conn.execute(
        "SELECT p.multiplier FROM provider_model_price p JOIN pricing_provider pp ON pp.id=p.provider_id "
        "WHERE pp.provider_key='legacy:direct:legacy.example' AND p.model_id='gpt-5.6-luna'"
    ).fetchone()
    assert row["multiplier"] == 1.7
