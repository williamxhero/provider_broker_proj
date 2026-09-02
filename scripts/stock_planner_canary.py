#!/usr/bin/env python3
"""Run the installed stock_advisor planner and its real verifier on sanitized production topology."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import time


def _sanitize(value, key=""):
    if isinstance(value, dict):
        return {name: _sanitize(item, name) for name, item in value.items() if name not in {"sha256", "cycle_id"}}
    if isinstance(value, list):
        return [_sanitize(item, key) for item in value]
    if isinstance(value, str):
        if key in {
            "key", "stage", "task_key", "as_of", "scheduled_for", "evidence_class", "mode", "start", "end",
            "allowed_research_backends", "allowed_coverage",
            "url", "source_kind",
        }:
            return value
        return "synthetic-public-value"
    return value


def _latest_failed_packet(database: Path) -> tuple[dict, list[str]]:
    uri = database.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """SELECT input_packet_json,verifier_json FROM llm_attempt
               WHERE error='Broker output did not pass local verification'
                 AND json_extract(verifier_json,'$.business.problems[0]') LIKE 'research_plan_%'
               ORDER BY started_at DESC LIMIT 1"""
        ).fetchone()
    if row is None:
        raise RuntimeError("no failed production research-plan attempt is available")
    packet = json.loads(row["input_packet_json"] or "{}")
    verifier = json.loads(row["verifier_json"] or "{}")
    problems = list((verifier.get("business") or {}).get("problems") or [])
    return _sanitize(packet), problems


def _cycle_packet(database: Path, cycle: str) -> tuple[dict, list[str]]:
    uri = database.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        row = connection.execute(
            "SELECT input_packet_json FROM llm_attempt WHERE cycle_id=? AND stage='m0_research' ORDER BY started_at DESC LIMIT 1",
            (cycle,),
        ).fetchone()
    if row is None:
        raise RuntimeError("cycle has no production m0_research packet")
    return _sanitize(json.loads(row[0] or "{}")), []


def _safe_attempts(items):
    return [
        {"status": row.get("status"), "requested_model": row.get("requested_model")}
        for row in items if isinstance(row, dict)
    ]


def main() -> int:
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "AITradingCompanion"
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, default=local)
    parser.add_argument("--url", default="http://192.168.50.2:8817")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--deadline-ms", type=int, default=300_000)
    parser.add_argument("--cycle")
    parser.add_argument("--verification-read-repair")
    parser.add_argument("--discovery-url", action="append", default=[])
    args = parser.parse_args()
    runtime_source = args.home / "app" / "runtime"
    sys.path.insert(0, str(runtime_source))
    from ai_trading_companion.broker_client import BrokerError, ProviderBrokerClient
    import ai_trading_companion.local_research as local_research
    BrokerResearchPlanner = local_research.BrokerResearchPlanner

    database = args.home / "data" / "trading-companion.sqlite3"
    packet, prior_problems = _cycle_packet(database, args.cycle) if args.cycle else _latest_failed_packet(database)
    requirement_keys = [
        str(row.get("key")) for row in (packet.get("evidence_contract") or {}).get("requirements") or []
        if isinstance(row, dict) and row.get("key")
    ]
    if not requirement_keys:
        raise RuntimeError("production packet has no evidence requirements")
    gaps = requirement_keys
    if args.verification_read_repair:
        if args.verification_read_repair not in requirement_keys or not args.discovery_url:
            raise RuntimeError("verification-read repair requires a contract key and at least one discovery URL")
        packet["research_discoveries"] = [
            {"requirement_key": args.verification_read_repair, "url": url, "source_kind": "production_canary"}
            for url in args.discovery_url[:4]
        ]
        gaps = [f"research_plan_missing_verification_read:{args.verification_read_repair}"]
        # Exercise Broker's repair instruction even when a newer caller has a
        # deterministic local short-circuit. This patch is process-local only.
        local_research._discovery_read_repair_plan = lambda *_args, **_kwargs: None
    failed = False
    for run in range(1, args.runs + 1):
        planner = BrokerResearchPlanner(
            ProviderBrokerClient(args.url), intellect="smart", effort="medium",
            deadline=lambda: time.monotonic() + args.deadline_ms / 1000,
            market_tool_available=True,
        )
        try:
            plan = planner(packet, gaps, 1 if args.verification_read_repair else 0)
            outcome = planner.outcomes[-1]
            result = {
                "run": run, "passed": True, "request_id": outcome.request_id,
                "requirements": len(requirement_keys), "operations": len(plan.get("operations") or []),
                "attempts": _safe_attempts(outcome.attempts), "problems": [],
            }
        except BrokerError as exc:
            failed = True
            business = (exc.verifier or {}).get("business") or {}
            result = {
                "run": run, "passed": False, "request_id": exc.request_id,
                "requirements": len(requirement_keys), "attempts": _safe_attempts(exc.attempts),
                "problems": list(business.get("problems") or []),
            }
        print(json.dumps(result, ensure_ascii=False), flush=True)
    print(json.dumps({"source_problems": prior_problems, "packet_keys": sorted(packet), "requirement_keys": requirement_keys}, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
