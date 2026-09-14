from provider_broker.db import Store
from provider_broker.upstream import estimate_cost_details


ALLOWED = ("openai", "anthropic", "deepseek", "qwen", "doubao", "deepinfra")


def entry(provider_type, model, host):
    return {
        "name": provider_type,
        "base_url": f"https://{host}/v1",
        "api_key": f"{provider_type}-secret",
        "provider_type": "openai_chat",
        "canonical_provider_type": provider_type,
        "models": [model],
        "inventory_status": "available",
    }


def test_cpa_provider_allowlist_creates_dynamic_cny_combinations(tmp_path):
    db = Store(tmp_path / "pricing.sqlite", b"0" * 32)
    entries = [entry(provider, f"{provider}-model-new", f"{provider}.example") for provider in ALLOWED]
    entries.append(entry("unsupported", "must-not-route", "unsupported.example") | {
        "canonical_provider_type": "unsupported",
    })

    db.replace_source_snapshot(entries, "2026-09-14T00:00:00Z")

    providers = db.conn.execute("SELECT provider_type FROM source_provider ORDER BY id").fetchall()
    assert [row[0] for row in providers] == list(ALLOWED)
    rows = db.provider_model_prices()
    dynamic_rows = [row for row in rows if row["provider_key"] in ALLOWED]
    assert {(row["provider_type"], row["model_id"]) for row in dynamic_rows} == {
        (provider, f"{provider}-model-new") for provider in ALLOWED
    }
    assert all(
        row["output_price_cny"] == 20
        and row["currency"] == "CNY"
        and row["input_price"] == row["cache_price"] == row["output_price"] == 20
        for row in dynamic_rows
    )


def test_sync_preserves_existing_price_and_mapping_multiplier(tmp_path):
    db = Store(tmp_path / "pricing.sqlite", b"0" * 32)
    first = entry("openai", "kept-model", "kept.example")
    db.replace_source_snapshot([first], "2026-09-14T00:00:00Z")
    fingerprint = db.conn.execute("SELECT fingerprint FROM source_provider").fetchone()[0]
    provider_id = db.conn.execute("SELECT pricing_provider_id FROM source_provider").fetchone()[0]
    db.upsert_provider_model_price(
        provider_id=provider_id, model_id="kept-model", source_kind="openai", output_price_cny=31,
    )
    db.upsert_key_model_mapping(
        fingerprint, "kept-model", target_provider_id=provider_id, target_model_id="kept-model", multiplier=1.5,
    )

    db.replace_source_snapshot([first | {"models": ["kept-model", "new-model"]}], "2026-09-14T01:00:00Z")

    kept = db.effective_key_pricing(fingerprint, "kept-model")
    new = db.effective_key_pricing(fingerprint, "new-model")
    assert kept["output_price_cny"] == 46.5
    assert kept["input_price"] == kept["cache_price"] == kept["output_price"] == 46.5
    assert new["output_price_cny"] == 20
    assert db.conn.execute(
        "SELECT count(*) FROM provider_model_price WHERE provider_id=? AND model_id=? AND active=1",
        (provider_id, "kept-model"),
    ).fetchone()[0] == 1


def test_cost_uses_one_output_price_for_all_token_classes():
    assert estimate_cost_details(
        "dynamic-model",
        {"input_tokens": 900, "output_tokens": 100, "input_tokens_details": {"cached_tokens": 400}},
        pricing={"output_price_cny": 20, "priced": True},
    ) == {"cost": 0.02, "reason": None}
