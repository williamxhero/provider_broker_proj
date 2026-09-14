from provider_broker.catalog import APPROVED_STAGE_MODELS, CATALOG, canonicalize
from provider_broker.db import Store
from provider_broker.upstream import model_fulfills


APPROVED_STAGES = {stage: set(models) for stage, models in APPROVED_STAGE_MODELS.items()}


def test_approved_catalog_entries_have_explicit_stage_and_output_price():
    approved = set().union(*APPROVED_STAGES.values())

    assert approved <= set(CATALOG)
    for stage, models in APPROVED_STAGES.items():
        for model in models:
            entry = CATALOG[model]
            assert entry["intellect"] == stage
            assert entry["family"]
            assert isinstance(entry["official_output_price_cny"], (int, float))


def test_approved_aliases_canonicalize_deterministically():
    aliases = {
        "DeepSeek_V4_Flash": "deepseek-v4-flash-0731",
        "deepseek-v4-1-flash": "deepseek-v4.1-flash",
        "deepseek-reasoner": "deepseek-v4.1-flash",
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
    assert "unknown-provider-model" in store.inventory()[0]["models"]
    assert all("unknown-provider-model" not in provider.models for provider in store.providers("smart"))


def test_legacy_catalog_writes_are_removed_and_fixed_directory_is_stable(tmp_path):
    path = tmp_path / "broker.sqlite3"
    first = Store(path, b"0123456789abcdef")
    first.conn.close()

    reopened = Store(path, b"0123456789abcdef")

    assert reopened.canonical_models()["glm-5.3"] == {
        "id": "glm-5.3", "family": CATALOG["glm-5.3"]["family"],
        "stage": CATALOG["glm-5.3"]["intellect"], "active": True,
    }
    assert "operator-model" not in reopened.canonical_models()
    assert reopened.canonical_models()["gpt-5.6-luna"]["family"] == "OpenAI GPT-5.6"


def test_model_fulfillment_requires_known_model_and_never_downgrades():
    assert model_fulfills("deepseek-v4-flash-0731", "glm-5.3-flash")
    assert model_fulfills("deepseek-v4.1-flash", "glm-5.3")
    assert model_fulfills("glm-5.3", "glm-5.3")
    assert not model_fulfills("glm-5.3", "glm-5.3-flash")
    assert not model_fulfills("deepseek-v4-flash-0731", "unrecognized-model")
