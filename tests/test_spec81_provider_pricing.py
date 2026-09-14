from pathlib import Path

import pytest

from provider_broker.db import Store
from provider_broker.pricing import PROVIDER_ALLOWLIST
from provider_broker.source import expand_config
from provider_broker.upstream import estimate_cost_details


def make_store(tmp_path: Path) -> Store:
    return Store(tmp_path / "spec81.sqlite", b"0" * 32)


def test_pricing_provider_allowlist_is_exactly_six_vendors(tmp_path):
    db = make_store(tmp_path)
    for provider_type in sorted(PROVIDER_ALLOWLIST):
        provider_id = db.create_pricing_provider(provider_type, provider_type=provider_type)
        assert db.conn.execute("SELECT provider_type FROM pricing_provider WHERE id=?", (provider_id,)).fetchone()[0] == provider_type
    with pytest.raises(ValueError, match="one of"):
        db.create_pricing_provider("unsupported", provider_type="glm")
    assert {row["provider_type"] for row in db.pricing_providers()} == set(PROVIDER_ALLOWLIST)


def test_cpa_entries_separate_transport_from_supported_pricing_identity():
    entries = expand_config({"providers": [
        {"type": "openai", "base_url": "https://openai.example", "keys": [{"key": "a", "models": ["unknown-openai-model"]}]},
        {"type": "anthropic", "base_url": "https://anthropic.example", "keys": [{"key": "b", "models": ["claude-new"]}]},
        {"type": "deepseek", "base_url": "https://deepseek.example", "keys": [{"key": "c", "models": ["deepseek-new"]}]},
        {"type": "qwen", "base_url": "https://qwen.example", "keys": [{"key": "d", "models": ["qwen-new"]}]},
        {"type": "doubao", "base_url": "https://doubao.example", "keys": [{"key": "e", "models": ["doubao-new"]}]},
        {"type": "deepinfra", "base_url": "https://deepinfra.example", "keys": [{"key": "f", "models": ["infra-new"]}]},
    ]})
    assert {entry["pricing_provider_type"] for entry in entries} == set(PROVIDER_ALLOWLIST)
    assert any(entry["provider_type"] == "openai_chat" for entry in entries)
    with pytest.raises(ValueError, match="unsupported provider type"):
        expand_config({"providers": [{
            "type": "glm", "base_url": "https://glm.example",
            "keys": [{"key": "g", "models": ["glm-new"]}],
        }]})


def test_sync_derives_dynamic_pairs_defaults_new_prices_and_preserves_mapping(tmp_path):
    db = make_store(tmp_path)
    entries = [{
        "name": "CPA OpenAI", "base_url": "https://key.example/v1", "api_key": "secret",
        "provider_type": "openai", "pricing_provider_type": "openai",
        "models": ["model-a"], "source": {"provider_type": "openai", "inventory_status": "available"},
    }]
    db.replace_source_snapshot(entries, "2026-09-14T00:00:00Z")
    fingerprint = db.conn.execute("SELECT fingerprint FROM source_provider").fetchone()[0]
    provider_id = db.conn.execute("SELECT id FROM pricing_provider WHERE provider_key='openai'").fetchone()[0]
    mapping_id = db.key_model_mappings(fingerprint=fingerprint)[0]["id"]
    db.update_key_model_mapping(mapping_id, multiplier=1.75)
    db.upsert_provider_model_price(provider_id=provider_id, model_id="model-a", output_price_cny=8)

    expanded = entries[0] | {"models": ["model-a", "model-b"]}
    db.replace_source_snapshot([expanded], "2026-09-15T00:00:00Z")

    prices = {(row["model_id"], row["output_price_cny"], row["currency"]): row for row in db.provider_model_prices()}
    assert ("model-a", 8.0, "CNY") in prices
    assert ("model-b", 20.0, "CNY") in prices
    assert db.key_model_mappings(fingerprint=fingerprint, model="model-a")[0]["multiplier"] == 1.75
    resolved = db.effective_key_pricing(fingerprint, "model-b")
    assert resolved["output_price_cny"] == 20.0
    assert resolved["input_price"] == resolved["cache_price"] == resolved["output_price"] == 20.0


def test_unknown_price_and_cost_use_one_output_rate():
    pricing = {"priced": True, "output_price_cny": 20}
    assert estimate_cost_details("model", {"input_tokens": 100, "output_tokens": 50, "input_tokens_details": {"cached_tokens": 100}}, pricing=pricing) == {
        "cost": 0.003,
        "reason": None,
    }
    db = Store(Path(":memory:"), b"0" * 32)
    unknown = db.effective_pricing("openai", "never-synced")
    assert unknown["status"] == "UNKNOWN" and unknown["output_price_cny"] is None
    db.conn.close()


def test_sync_preserves_legacy_key_source_price_during_provider_identity_migration(tmp_path):
    db = make_store(tmp_path)
    old_provider = db.conn.execute(
        "INSERT INTO pricing_provider(provider_key,name,provider_type,multiplier,active) VALUES(?,?,?,?,1)",
        ("key-source:legacy-openai", "legacy", "direct", 1.0),
    ).lastrowid
    db.insert_provider_model_price(
        provider_id=old_provider,
        model_id="legacy-model",
        output_price_cny=7,
        source_name="legacy-price",
    )
    db.conn.commit()

    db.replace_source_snapshot([{
        "name": "CPA OpenAI", "base_url": "https://key.example/v1", "api_key": "secret",
        "provider_type": "openai", "pricing_provider_type": "openai",
        "models": ["legacy-model"], "source": {"provider_type": "openai"},
    }], "2026-09-14T00:00:00Z")

    canonical = db.conn.execute(
        """SELECT p.output_price_cny
           FROM provider_model_price p
           JOIN pricing_provider provider ON provider.id=p.provider_id
           WHERE provider.provider_key='openai' AND p.model_id='legacy-model' AND p.active=1"""
    ).fetchone()
    assert canonical["output_price_cny"] == 7.0


def test_empty_cpa_snapshot_deactivates_old_dynamic_pairs(tmp_path):
    db = make_store(tmp_path)
    db.replace_source_snapshot([{
        "name": "CPA OpenAI", "base_url": "https://key.example/v1", "api_key": "secret",
        "provider_type": "openai", "pricing_provider_type": "openai", "models": ["dynamic-model"],
        "source": {"provider_type": "openai"},
    }], "2026-09-14T00:00:00Z")
    db.replace_source_snapshot([], "2026-09-14T00:01:00Z")

    active = db.conn.execute(
        """SELECT count(*) FROM provider_model_price p
           JOIN pricing_provider provider ON provider.id=p.provider_id
           WHERE provider.provider_key='openai' AND p.model_id='dynamic-model' AND p.active=1"""
    ).fetchone()[0]
    assert active == 0


def test_cpa_sync_does_not_inject_static_models_into_dynamic_pairs(tmp_path):
    db = make_store(tmp_path)
    db.replace_source_snapshot([{
        "name": "CPA DeepSeek", "base_url": "https://api.deepseek.com/v1", "api_key": "secret",
        "provider_type": "deepseek", "pricing_provider_type": "deepseek", "models": ["actual-model"],
        "source": {"provider_type": "deepseek"},
    }], "2026-09-14T00:00:00Z")

    models = [row[0] for row in db.conn.execute(
        """SELECT p.model_id FROM provider_model_price p
           JOIN pricing_provider provider ON provider.id=p.provider_id
           WHERE provider.provider_key='deepseek' AND p.active=1"""
    )]
    assert models == ["actual-model"]
