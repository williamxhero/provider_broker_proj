#!/usr/bin/env python3
"""Replay Broker's safe research-plan audit projections through stock_advisor's installed verifier."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


def main() -> int:
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "AITradingCompanion"
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, default=local)
    parser.add_argument("--cycle", required=True)
    parser.add_argument("--minimum-id", type=int, default=0)
    parser.add_argument("--ssh-host")
    args = parser.parse_args()
    sys.path.insert(0, str(args.home / "app" / "runtime"))
    from ai_trading_companion.local_research import _verify_research_plan

    uri = (args.home / "data" / "trading-companion.sqlite3").resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        row = connection.execute(
            "SELECT input_packet_json FROM llm_attempt WHERE cycle_id=? AND stage='m0_research' ORDER BY started_at DESC LIMIT 1",
            (args.cycle,),
        ).fetchone()
    if row is None:
        raise RuntimeError("cycle has no m0_research packet")
    packet = json.loads(row[0] or "{}")
    requirement_keys = {
        str(item.get("key")) for item in (packet.get("evidence_contract") or {}).get("requirements") or []
        if isinstance(item, dict) and item.get("key")
    }
    if args.ssh_host:
        raw = subprocess.check_output([
            "ssh", args.ssh_host,
            "set -a; . /data/provider-broker/secrets/broker.env; set +a; "
            "curl -fsS -H \"Authorization: Bearer $BROKER_ADMIN_TOKEN\" "
            "'http://192.168.50.2:8817/admin/v1/calls?window=1h&limit=100'",
        ], text=True)
        calls = json.loads(raw).get("items") or []
    else:
        calls = json.load(sys.stdin).get("items") or []
    replayed = []
    frequency: Counter[str] = Counter()
    for call in sorted(calls, key=lambda item: int(item.get("id") or 0)):
        if int(call.get("id") or 0) < args.minimum_id or call.get("status") != "completed":
            continue
        plan = (call.get("diagnostic") or {}).get("research_plan_output")
        context = (call.get("diagnostic") or {}).get("research_plan_context")
        if not isinstance(plan, dict) or not isinstance(context, dict):
            continue
        planned = {
            str(item.get("requirement_key") or "") for item in plan.get("operations") or []
            if isinstance(item, dict)
        }
        if not planned or not planned.issubset(requirement_keys):
            continue
        replay_packet = packet | {
            key: context.get(key) for key in (
                "available_backends", "coverage_gaps", "market_time_context", "research_discoveries",
            )
        }
        verdict = _verify_research_plan(replay_packet, plan)
        problems = list(verdict.get("problems") or [])
        frequency.update(problems)
        replayed.append({
            "id": call.get("id"), "time": call.get("time"), "route_id": call.get("route_id"),
            "request_id": call.get("request_id"), "passed": bool(verdict.get("passed")),
            "problems": problems, "operations": plan.get("operations") or [],
        })
    print(json.dumps({
        "cycle": args.cycle, "requirements": sorted(requirement_keys), "replayed": replayed,
        "problem_frequency": dict(sorted(frequency.items())),
    }, ensure_ascii=False))
    return 0 if replayed else 2


if __name__ == "__main__":
    raise SystemExit(main())
