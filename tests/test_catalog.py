from provider_broker.catalog import CATALOG, blended_price, canonicalize
from provider_broker.db import Store
from provider_broker.upstream import model_fulfills


APPROVED_STAGES = {
    "standard": {"deepseek-v4-flash-0731", "doubao-seed-2.0-lite"},
    "smart": {
        "glm-5.3-flash", "deepseek-v4.1-flash", "qwen3.8-flash-next",
        "doubao-seed-2.1-turbo", "doubao-seed-2.1-pro",
    },
    "expert": {"glm-5.3"},
}


def test_approved_catalog_entries_have_explicit_stage_and_valid_blended_price():
    approved = set().union(*APPROVED_STAGES.values())

    assert approved <= set(CATALOG)
    for stage, models in APPROVED_STAGES.items():
        for model in models:
            entry = CATALOG[model]
            assert entry["intellect"] == stage
            assert entry["family"]
            assert all(isinstance(entry[field], (int, float)) for field in (
                "official_input_price", "official_cache_price", "official_output_price",
            ))
            assert blended_price(entry) >= 0


def test_approved_aliases_canonicalize_deterministically():
    aliases = {
        "DeepSeek_V4_Flash": "deepseek-v4-flash-0731",
        "deepseek-v4-1-flash": "deepseek-v4.1-flash",
        "doubao-seed-2-0-lite": "doubao-seed-2.0-lite",
        "doubao-seed-2-1-turbo": "doubao-seed-2.1-turbo",
        "doubao-seed-2-1-pro": "doubao-seed-2.1-pro",
        "glm-5-3-flash": "glm-5.3-flash",
        "glm-5-3": "glm-5.3",
        "qwen3-8-flash-next": "qwen3.8-flash-next",
    }

    assert {alias: canonicalize(alias) for alias in aliases} == aliases


def test_discovery_intersects_with_catalog_and_stage_routing(tmp_path):
    store = Store(tmp_path / "broker.sqlite3", b"0123456789abcdef")
    store.replace_source_snapshot([{
        "name": "approved-provider", "base_url": "https://provider.test",
        "api_key": "secret", "provider_type": "openai_chat",
        "models": ["deepseek-v4-flash", "glm-5.3", "unknown-provider-model"],
        "inventory_status": "available",
    }], "2026-09-12T00:00:00+00:00")

    assert {provider.models[0] for provider in store.providers("standard")} == {"deepseek-v4-flash-0731"}
    assert {provider.models[0] for provider in store.providers("smart")} == set()
    assert {provider.models[0] for provider in store.providers("expert")} == {"glm-5.3"}
    assert "unknown-provider-model" not in store.inventory()[0]["models"]


def test_catalog_migration_adds_missing_seeds_without_overwriting_existing_entries(tmp_path):
    path = tmp_path / "broker.sqlite3"
    first = Store(path, b"0123456789abcdef")
    custom = {
        "family": "Operator managed", "intellect": "smart",
        "official_input_price": 1.0, "official_cache_price": 0.1, "official_output_price": 3.0,
    }
    assert first.create_catalog("operator-model", custom)
    with first.conn:
        first.conn.execute("UPDATE broker_setting SET value='1' WHERE name='catalog_seed_version'")
        first.conn.execute("DELETE FROM model_catalog WHERE model='glm-5.3'")
        first.conn.execute("UPDATE model_catalog SET family='Operator override' WHERE model='gpt-5.6-luna'")
    first.conn.close()

    reopened = Store(path, b"0123456789abcdef")

    assert reopened.catalog()["glm-5.3"] == CATALOG["glm-5.3"]
    assert reopened.catalog()["operator-model"] == custom
    assert reopened.catalog()["gpt-5.6-luna"]["family"] == "Operator override"


def test_model_fulfillment_requires_known_model_and_never_downgrades():
    assert model_fulfills("deepseek-v4-flash-0731", "glm-5.3-flash")
    assert model_fulfills("deepseek-v4.1-flash", "glm-5.3")
    assert model_fulfills("glm-5.3", "glm-5.3")
    assert not model_fulfills("glm-5.3", "glm-5.3-flash")
    assert not model_fulfills("deepseek-v4-flash-0731", "unrecognized-model")
