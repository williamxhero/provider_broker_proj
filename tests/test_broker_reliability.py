from pathlib import Path

from provider_broker.db import Store
from provider_broker.upstream import classify_failure


def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "broker.sqlite3", b"0123456789abcdef")


def test_route_metrics_measure_request_outcome_separately_from_attempts(tmp_path):
    db = store(tmp_path)
    db.route_started("route-1", "standard", "request-1")
    db.observe(
        fingerprint="key-1", requested_model="gpt-5.6-luna", actual_model=None,
        tier="standard", effort=None, success=0, latency_ms=None, error="cancelled",
        status="cancelled", input_tokens=None, output_tokens=None, cost=None, request_id="attempt-1",
        route_id="route-1", attempt_number=1,
    )
    db.route_finished("route-1", outcome="completed", first_delta_ms=120, completed_ms=410)

    quality = db.quality()

    assert quality["calls"] == 1
    assert quality["technical_success_rate"] == 0
    assert quality["request_success_rate"] == 1
    assert quality["client_first_delta_p50_ms"] == 120
    assert quality["cancellation_neutral_attempts"] == 1


def test_source_snapshot_keeps_last_known_working_inventory_on_discovery_failure(tmp_path):
    db = store(tmp_path)
    original = {
        "name": "Terra", "site_name": "terra", "base_url": "https://terra.example/v1", "api_key": "key",
        "models": ["gpt-5.6-luna"], "inventory_status": "available", "source": {},
    }
    db.replace_source_snapshot([original], "2026-09-08T00:00:00+00:00")
    failed = original | {"models": ["unavailable"], "inventory_status": "unavailable"}
    db.replace_source_snapshot([failed], "2026-09-08T01:00:00+00:00")

    provider = db.inventory()[0]
    assert provider["models"] == ["gpt-5.6-luna"]
    assert provider["inventory_status"] == "stale"


def test_upgrade_backfills_site_domain_for_existing_inventory(tmp_path):
    path = tmp_path / "broker.sqlite3"
    db = Store(path, b"0123456789abcdef")
    db.replace_source_snapshot([{
        "name": "Terra", "base_url": "https://terra.example/v1", "api_key": "key",
        "models": ["gpt-5.6-luna"], "inventory_status": "available", "source": {},
    }], "2026-09-08T00:00:00+00:00")
    with db.conn:
        db.conn.execute("UPDATE source_provider SET site_id='default'")
        db.conn.execute("DELETE FROM site_policy")
    db.conn.close()

    upgraded = Store(path, b"0123456789abcdef")

    assert upgraded.sites()[0]["site_id"] == "terra.example"


def test_fault_classification_does_not_open_circuit_for_contract_rejection():
    assert classify_failure("structured_output_invalid", {}) == "contract"
    assert classify_failure("cancelled", {}) == "neutral"
    assert classify_failure("unavailable", {"http_status": 429}) == "provider_overload"
    assert classify_failure("transport_failed", {}) == "transport"
