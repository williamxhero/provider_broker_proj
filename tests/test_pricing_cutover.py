import json

import pytest

from provider_broker.db import Store


def make_store(tmp_path):
    return Store(tmp_path / "cutover.sqlite", b"0" * 32)


def test_key_model_multiplier_is_the_runtime_rate_key(tmp_path):
    db = make_store(tmp_path)
    provider_id = db.create_pricing_provider("openai-cutover", provider_type="openai", multiplier=9.0)
    db.create_canonical_model("cutover-model", stage="smart", family="Cutover")
    db.insert_provider_model_price(
        provider_id=provider_id, model_id="cutover-model", output_price_cny=4.0, multiplier=1.25,
    )
    db.conn.execute(
        "INSERT INTO source_provider(fingerprint,name,base_url,api_key,provider_type,models_json,source_json,synced_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("cutover-key", "Key", "https://cutover.example/v1", db._encrypt("secret"), "openai",
         json.dumps(["cutover-model"]), json.dumps({"provider_type": "openai"}), "2026-09-14T00:00:00Z"),
    )
    db.conn.execute("INSERT INTO policy(fingerprint,multiplier) VALUES(?,?)", ("cutover-key", 7.0))
    db.create_key_model_mapping("cutover-key", "cutover-model", target_provider_id=provider_id, multiplier=1.25)

    db.conn.execute("UPDATE pricing_provider SET multiplier=8.0 WHERE id=?", (provider_id,))
    db.conn.commit()

    resolved = db.effective_key_pricing("cutover-key", "cutover-model")

    assert resolved["multiplier"] == 1.25
    assert (resolved["input_price"], resolved["cache_price"], resolved["output_price"]) == (5.0, 5.0, 5.0)


def test_legacy_catalog_write_is_rejected_when_provider_prices_are_ambiguous(tmp_path):
    db = make_store(tmp_path)
    db.create_canonical_model("shared-model", stage="smart", family="Shared")
    first = db.create_pricing_provider("openai-one", provider_type="openai")
    second = db.create_pricing_provider("anthropic-two", provider_type="anthropic")
    for provider_id, price in ((first, 1.0), (second, 2.0)):
        db.insert_provider_model_price(
            provider_id=provider_id, model_id="shared-model", output_price_cny=price * 2, multiplier=1.0,
        )

    assert not hasattr(db, "update_catalog")


def test_pricing_startup_health_reports_migration_and_integrity_gates(tmp_path):
    db = make_store(tmp_path)
    health = db.pricing_health()

    assert health["migration"]["status"] == "completed"
    assert health["migration"]["version"] == 4
    assert health["duplicate_active"] == 0
    assert health["dangling_mappings"] == 0
    assert "unpriced_active" in health
    assert health["startup_ready"] is True


def test_migration_carries_legacy_multiplier_into_each_key_model_mapping(tmp_path):
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
        "WHERE pp.provider_key='openai' AND p.model_id='gpt-5.6-luna'"
    ).fetchone()
    assert row["multiplier"] == 1.0
    mapping = db.key_model_mappings(fingerprint="legacy-cutover", model="gpt-5.6-luna")[0]
    assert mapping["multiplier"] == 1.0
