import json
import sqlite3
from pathlib import Path

import pytest

from provider_broker.catalog import normalize_hostname
from provider_broker.db import Store


def make_store(tmp_path: Path) -> Store:
    return Store(tmp_path / "mapping.sqlite", b"0" * 32)


def test_key_model_mapping_is_unique_and_is_the_only_runtime_multiplier(tmp_path):
    db = make_store(tmp_path)
    db.replace_source_snapshot([{
        "name": "direct", "base_url": "https://direct.example/v1", "api_key": "secret",
        "provider_type": "openai", "models": ["gpt-5.6-luna"],
        "source": {"provider_type": "direct", "inventory_status": "available"},
    }], "2026-09-14T00:00:00Z")
    fingerprint = db.conn.execute("SELECT fingerprint FROM source_provider").fetchone()[0]
    official_id = db.conn.execute(
        "SELECT id FROM pricing_provider WHERE provider_key='official-catalog'"
    ).fetchone()[0]
    mapping_id = db.upsert_key_model_mapping(
        fingerprint, "gpt-5.6", target_provider_id=official_id,
        target_model_id="gpt-5.6-sol", multiplier=1.25,
    )
    db.conn.execute("UPDATE policy SET multiplier=9.0 WHERE fingerprint=?", (fingerprint,))
    db.conn.execute("UPDATE pricing_provider SET multiplier=8.0 WHERE id=?", (official_id,))
    db.conn.commit()

    resolved = db.effective_key_pricing(fingerprint, "gpt-5.6")
    assert resolved["mapping_id"] == mapping_id
    assert resolved["multiplier"] == 1.25
    assert resolved["input_price"] == 5.0
    with pytest.raises(sqlite3.IntegrityError):
        db.create_key_model_mapping(
            fingerprint, "gpt-5.6-sol", target_provider_id=official_id,
            target_model_id="gpt-5.6-sol",
        )


def test_relay_mapping_targets_official_row_and_missing_mapping_is_unknown(tmp_path):
    db = make_store(tmp_path)
    db.replace_source_snapshot([{
        "name": "relay", "base_url": "https://relay.example/v1", "api_key": "relay-secret",
        "provider_type": "relay", "models": ["gpt-5.6-sol"],
        "source": {"provider_type": "relay", "inventory_status": "available"},
    }], "2026-09-14T00:00:00Z")
    fingerprint, = db.conn.execute("SELECT fingerprint FROM source_provider").fetchone()
    mapping = db.key_model_mappings(fingerprint=fingerprint)[0]
    assert mapping["target_provider_key"] == "official-openai"
    assert db.effective_key_pricing(fingerprint, "gpt-5.6-sol")["priced"] is True
    assert db.effective_key_pricing(fingerprint, "gpt-5.6-luna")["reason"] == "key-model mapping is missing or disabled"


def test_hostname_normalization_and_currency_buckets_preserve_history(tmp_path):
    assert Store.fingerprint("HTTPS://API-TOP.COM:443/v1/", "secret") == Store.fingerprint("https://api-top.com/v1", "secret")
    assert normalize_hostname("HTTPS://code28.ccwu.cc:443/path") == "code28.ccwu.cc"
    assert normalize_hostname("https://api-top.com/v1") == "api-top.com"
    assert normalize_hostname("https://foo.bar.maas.aliyuncs.com:8443/v1") == "maas.aliyuncs.com"

    db = make_store(tmp_path)
    db.observe(
        fingerprint="key", requested_model="gpt-5.6-luna", actual_model="gpt-5.6-luna",
        tier="standard", success=1, input_tokens=10, output_tokens=5, cost=1.25,
        currency="USD", status="completed", request_id="history-1",
    )
    db.observe(
        fingerprint="key", requested_model="gpt-5.6-luna", actual_model="gpt-5.6-luna",
        tier="standard", success=1, input_tokens=20, output_tokens=5, cost=2.5,
        currency="CNY", status="completed", request_id="history-2",
    )
    before = db.conn.execute("SELECT cost FROM observation ORDER BY id").fetchall()
    accounting = db.accounting(window="24h")
    assert accounting["total_tokens"] == 40
    assert accounting["fees_by_currency"] == {"CNY": 2.5, "USD": 1.25}
    assert db.conn.execute("SELECT cost FROM observation ORDER BY id").fetchall() == before


def test_mapping_migration_is_retryable_after_failure(tmp_path):
    db = make_store(tmp_path)
    db.conn.execute(
        "INSERT INTO source_provider(fingerprint,name,base_url,api_key,provider_type,models_json,source_json,synced_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("legacy", "Legacy", "https://legacy.example/v1", db._encrypt("secret"), "openai",
         json.dumps(["gpt-5.6-luna"]), json.dumps({"provider_type": "direct"}), "2026-09-14T00:00:00Z"),
    )
    db.conn.execute("INSERT INTO policy(fingerprint,multiplier) VALUES(?,?)", ("legacy", 1.5))
    db.conn.execute(
        "CREATE TRIGGER fail_mapping BEFORE INSERT ON key_model_mapping "
        "WHEN NEW.fingerprint='legacy' BEGIN SELECT RAISE(ABORT, 'forced mapping failure'); END"
    )
    db.conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="forced mapping failure"):
        db.migrate_pricing()
    assert db.pricing_health()["migration"]["status"] == "failed"
    db.conn.execute("DROP TRIGGER fail_mapping")
    db.conn.commit()
    assert db.migrate_pricing()["migrated"] is True
    assert db.pricing_health()["startup_ready"] is True
