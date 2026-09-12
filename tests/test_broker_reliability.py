from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from provider_broker.db import Store
from provider_broker.health import run_probe
from provider_broker.source import expand_config
from provider_broker.upstream import AttemptFailure, classify_failure


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


def test_versioned_route_telemetry_reports_safe_cohorts_and_request_coverage(tmp_path):
    db = store(tmp_path)
    db.route_started(
        "route-telemetry", "smart", "request-telemetry", effort="high",
        body={
            "prompt": "sentinel prompt must never persist",
            "deadline_ms": 12_000,
            "output_token_limit": 600,
            "output_schema": {"type": "object", "properties": {"answer": {"type": "string"}}},
        },
        delivery_mode="validated_stream",
    )
    db.route_finished("route-telemetry", outcome="completed", completed_ms=345)

    route = db.route_detail("route-telemetry")
    quality = db.quality()

    assert route["telemetry_version"] == 1
    assert route["delivery_mode"] == "validated_stream"
    assert route["input_bucket"] == "1-256"
    assert route["output_budget_bucket"] == "257-1024"
    assert route["deadline_bucket"] == "5s-30s"
    assert route["schema_family"] == "object"
    assert "sentinel prompt" not in str(route)
    assert quality["request_outcomes"] == {
        "completed": 1, "failed": 0, "timed_out": 0, "client_cancelled": 0,
        "validation_rejected": 0, "in_progress": 0, "unknown": 0,
    }
    assert quality["request_success_numerator"] == 1
    assert quality["request_success_denominator"] == 1
    assert quality["request_coverage"] == 1
    assert quality["first_forwarded_delta"]["applicable_count"] == 0
    assert quality["first_forwarded_delta"]["p95_ms"] is None


def test_structured_capability_gate_is_scoped_to_one_key_and_model(tmp_path):
    db = store(tmp_path)
    db.replace_source_snapshot([
        {"name": "Claude A", "base_url": "https://claude-a.test", "api_key": "key-a",
         "provider_type": "claude", "models": ["claude-sonnet-5"], "inventory_status": "available"},
        {"name": "Claude B", "base_url": "https://claude-b.test", "api_key": "key-b",
         "provider_type": "claude", "models": ["claude-sonnet-5"], "inventory_status": "available"},
    ], datetime.now(UTC).isoformat())
    first = db.providers("smart")
    assert {item.api_key for item in first} == {"key-a", "key-b"}

    fingerprint = first[0].fingerprint
    db.record_capability(fingerprint, "claude-sonnet-5", "structured", "unsupported", "contract")

    assert [item.api_key for item in db.providers("smart", contract="structured")] == ["key-b"]
    assert {item.api_key for item in db.providers("smart", contract="plain")} == {"key-a", "key-b"}


def test_route_reconciliation_and_audit_detail_are_idempotent_and_private(tmp_path):
    db = store(tmp_path)
    db.route_started("route-open", "standard", "request-open", body={"prompt": "secret prompt"})
    with db.conn:
        db.conn.execute("UPDATE route_run SET started_at=? WHERE route_id='route-open'", ((datetime.now(UTC) - timedelta(minutes=5)).isoformat(),))
    db.record_candidate("route-open", fingerprint="key-1", model="luna", site_id="site-a", eligible=True, initial_rank=1)
    db.record_candidate("route-open", fingerprint="key-2", model="luna", site_id="site-b", eligible=False, exclusion_reason="health_open", initial_rank=2)
    db.record_attempt("route-open", attempt_number=1, fingerprint="key-1", model="luna", site_id="site-a", role="primary", status="cancelled")

    assert db.reconcile_open_routes(grace_seconds=30) == 1
    db.route_finished("route-open", outcome="completed")
    detail = db.route_detail("route-open")
    health = db.data_health()

    assert detail["outcome"] == "unknown"
    assert detail["terminal_reason"] == "reconciled_after_restart"
    assert detail["candidates"][1]["exclusion_reason"] == "health_open"
    assert detail["attempts"][0]["role"] == "primary"
    assert "secret prompt" not in str(detail)
    assert health["reconciled_unknown"] == 1
    assert health["in_progress"] == 0


def test_route_audit_subresources_are_bounded_ordered_and_include_milestones(tmp_path):
    db = store(tmp_path)
    db.route_started("route-api", "standard", "request-api")
    db.route_milestone("route-api", first_attempt_ms=3, first_text_ms=9, validation_completed_ms=12)
    for ordinal in range(3):
        db.record_candidate("route-api", fingerprint=f"key-{ordinal}", model="luna", site_id="site-a",
                            eligible=True, initial_rank=ordinal + 1)
    for attempt in range(1, 3):
        db.record_attempt("route-api", attempt_number=attempt, fingerprint=f"key-{attempt}", model="luna",
                          site_id="site-a", role="primary" if attempt == 1 else "retry", status="completed")

    detail = db.route_detail("route-api")
    assert detail["first_attempt_ms"] == 3
    assert detail["validation_completed_ms"] == 12
    candidates, next_cursor = db.route_audit_items("route-api", "candidates", limit=2)
    assert [item["ordinal"] for item in candidates] == [0, 1]
    assert next_cursor == 1
    attempts, next_attempt_cursor = db.route_audit_items("route-api", "attempts", limit=1, cursor=1)
    assert [item["attempt_number"] for item in attempts] == [2]
    assert next_attempt_cursor is None
    assert "prompt" not in str(detail)


def test_delivery_latency_uses_applicable_modes_and_never_turns_unknown_into_zero(tmp_path):
    db = store(tmp_path)
    db.route_started("route-stream", "standard", "request-stream", delivery_mode="plain_stream")
    db.route_milestone("route-stream", first_attempt_ms=8, capacity_wait_ms=3, first_forwarded_delta_ms=120)
    db.route_finished("route-stream", outcome="completed", first_delta_ms=120, completed_ms=400)
    db.route_started("route-structured", "standard", "request-structured", delivery_mode="validated_stream")
    db.route_milestone("route-structured", validation_completed_ms=600)
    db.route_finished("route-structured", outcome="completed", completed_ms=600)

    metrics = db.quality()["delivery_latency"]

    assert metrics["plain_stream"]["first_forwarded_delta"]["applicable_count"] == 1
    assert metrics["plain_stream"]["first_forwarded_delta"]["p95_ms"] == 120
    assert metrics["validated_stream"]["first_forwarded_delta"]["applicable_count"] == 0
    assert metrics["validated_stream"]["valid_completion"]["p95_ms"] == 600


def test_analytics_groups_only_allowlisted_route_dimensions(tmp_path):
    db = store(tmp_path)
    db.route_started("route-a", "smart", "request-a", delivery_mode="non_stream")
    db.route_finished("route-a", outcome="completed", selected_site_id="site-a", completed_ms=50)
    report = db.analytics(group_by="site", filters={"intellect": "smart"})

    assert report["groups"][0]["group"] == "site-a"
    assert report["groups"][0]["success_numerator"] == 1
    assert report["groups"][0]["insufficient"] is True


def test_client_first_delta_telemetry_is_idempotent_and_only_accepts_plain_routes(tmp_path):
    db = store(tmp_path)
    db.route_started("plain-route", "standard", "plain-request", delivery_mode="plain_stream")
    assert db.record_client_telemetry(route_id="plain-route", metric_type="client_first_delta", elapsed_ms=130, client_family="desktop", client_version="1", telemetry_version=1)
    assert not db.record_client_telemetry(route_id="plain-route", metric_type="client_first_delta", elapsed_ms=130, client_family="desktop", client_version="1", telemetry_version=1)
    db.route_started("json-route", "standard", "json-request", delivery_mode="validated_stream")
    try:
        db.record_client_telemetry(route_id="json-route", metric_type="client_first_delta", elapsed_ms=130, client_family="desktop", client_version="1", telemetry_version=1)
    except LookupError:
        pass
    else:
        raise AssertionError("structured route must reject client first delta telemetry")


def test_rollups_are_idempotent_and_retention_requires_a_watermark(tmp_path):
    db = store(tmp_path)
    db.route_started("old-route", "standard", "old-request")
    db.route_finished("old-route", outcome="completed")
    with db.conn:
        db.conn.execute("UPDATE route_run SET started_at=? WHERE route_id='old-route'", ((datetime.now(UTC) - timedelta(days=100)).isoformat(),))

    first = db.run_rollups()
    second = db.run_rollups()

    assert first["built"] == second["built"] == 1
    assert db.apply_retention(raw_days=90) == 1
    assert db.rollup_status()["hourly_count"] == 1


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


def test_model_not_found_isolated_from_provider_health_and_detected_from_404():
    assert classify_failure("unavailable", {"http_status": 404}) == "model_not_found"
    assert classify_failure("unavailable", {"upstream_error_code": "MODEL_NOT_FOUND"}) == "model_not_found"
    assert classify_failure("unavailable", {"http_status": 401}) == "credential"


@pytest.mark.asyncio
async def test_structured_probe_contract_failure_records_capability_without_opening_circuit(tmp_path):
    db = store(tmp_path)
    db.replace_source_snapshot([{
        "name": "Claude", "base_url": "https://claude.test", "api_key": "key",
        "provider_type": "claude", "models": ["claude-sonnet-5"], "inventory_status": "available",
    }], datetime.now(UTC).isoformat())
    target = db.providers("smart")[0]

    async def reject_contract(_provider, _body):
        raise AttemptFailure("unavailable", diagnostic={"http_status": 400, "schema_hash": "schema"})

    with patch("provider_broker.health._invoke_stream", reject_contract):
        for _ in range(3):
            result = await run_probe(db, tier="smart", mode="all", fingerprint=target.fingerprint,
                                     model=target.models[0], contract="structured")
            assert result[0]["state"] == "failed"

    assert db.health(target.fingerprint, target.models[0])["state"] != "open"
    assert not db.capability_allows_route(target.fingerprint, target.models[0], "structured")
    assert db.capability_allows_route(target.fingerprint, target.models[0], "plain")


def test_cpa_aliases_are_normalized_for_discovered_model_repair():
    entries = expand_config({
        "codex-api-key": [{
            "base_url": "https://provider.example/v1", "api_key": "secret",
            "models": [{"name": "gpt-5.6-terra", "alias": " GPT-5.6-LUNA "}],
        }],
    })
    assert entries[0]["aliases"] == {"gpt-5.6-luna": "gpt-5.6-terra"}
