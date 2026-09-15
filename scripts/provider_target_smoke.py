#!/usr/bin/env python3
"""Secret-safe live smoke for every Broker-owned vendor model target.

Run on the Broker host with its normal environment.  This bypasses routing and
circuit state but uses the exact stored endpoint, credential, transport
adapter, wire-model mapping, and upstream invocation code used in production.
"""
from __future__ import annotations

import argparse
import asyncio
import json

from provider_broker.catalog import BROKER_PROVIDER_MODELS
from provider_broker.db import Store
from provider_broker.settings import Settings
from provider_broker.health import structured_probe_prompt
from provider_broker.upstream import AttemptFailure, invoke_stream


TARGET_PROVIDERS = ("deepinfra", "deepseek", "qwen", "doubao")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout-ms", type=int, default=120_000)
    parser.add_argument("--provider", action="append", choices=TARGET_PROVIDERS)
    parser.add_argument("--structured", action="store_true")
    args = parser.parse_args()
    settings = Settings.from_env()
    store = Store(settings.database_path, settings.key_bytes(), settings.parallel_cap)
    selected = set(args.provider or TARGET_PROVIDERS)
    catalog = store.canonical_models()
    failures = 0
    rows = store.conn.execute(
        """SELECT s.*,p.enabled,p.calibrated,p.tiers_json,p.max_parallel
             FROM source_provider s JOIN policy p USING(fingerprint)
            ORDER BY s.id"""
    ).fetchall()
    for row in rows:
        provider_type = store._broker_provider_type_for_row(row)
        if provider_type not in selected:
            continue
        for model in BROKER_PROVIDER_MODELS[provider_type]:
            provider = store._provider_from_row(row, model, catalog)
            result = {
                "provider": provider_type,
                "model": model,
                "wire_model": provider.wire_model,
                "transport": provider.provider_type,
                "state": "failed",
            }
            try:
                output = await invoke_stream(provider, {
                    "prompt": structured_probe_prompt() if args.structured else "Reply with exactly: provider-broker-ok",
                    "output_token_limit": 1024 if args.structured else 64,
                    "deadline_ms": args.timeout_ms,
                    "effort": "low",
                    "_first_event_timeout_ms": min(args.timeout_ms, 60_000),
                    "_attempt_timeout_ms": args.timeout_ms,
                    "_preserve_prompt_envelope": args.structured,
                })
                result.update({
                    "state": "succeeded",
                    "actual_model": output.get("actual_model"),
                    "ttft_ms": output.get("latency_ms"),
                })
            except AttemptFailure as exc:
                failures += 1
                result.update({"error": exc.status, "diagnostic": exc.diagnostic})
            except Exception as exc:  # Keep output secret-safe and stable.
                failures += 1
                result.update({"error": type(exc).__name__})
            print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
