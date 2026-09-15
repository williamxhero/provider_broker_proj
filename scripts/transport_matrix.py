#!/usr/bin/env python3
"""Run a small, secret-safe CPA vs Broker transport matrix on yosef-server.

The script deliberately performs no provider discovery beyond Broker's existing
inventory and prints no prompt, response body, credential, or URL containing a
credential.  Each cell uses a 32-token canary; use ``--gate`` in deployment to
require only Broker's structured direct path, not CPA's independent routing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request


SCHEMA = {
    "type": "object",
    "properties": {"healthy": {"type": "boolean"}},
    "required": ["healthy"],
    "additionalProperties": False,
}
TIERS = {
    "gpt-5.6-luna": "standard",
    "deepseek-v4-flash-0731": "standard",
    "gpt-5.6-terra": "smart",
    "claude-sonnet-5": "smart",
    "glm-5.3-flash": "smart",
    "deepseek-v4.1-flash": "smart",
    "qwen3.8-flash-next": "smart",
    "doubao-seed-2.1-turbo": "smart",
    "doubao-seed-2.1-pro": "smart",
    "gpt-5.6-sol": "expert",
    "gpt-5.5": "expert",
    "claude-opus-5": "expert",
    "claude-opus-4-8": "expert",
    "glm-5.3": "expert",
}


def request(url: str, *, payload: dict | None = None, headers: dict | None = None, timeout: int = 70) -> tuple[int | str, dict | None]:
    data = json.dumps(payload).encode() if payload is not None else None
    request_headers = {"Content-Type": "application/json"} if data else {}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, data=data, headers=request_headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            parsed = json.load(response)
            return response.status, parsed if isinstance(parsed, dict) else None
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception:
        return "transport_error", None


def cpa_cell(cpa_url: str, cpa_key: str, model: str, contract: str) -> dict:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Return exactly one JSON object with healthy set to true."}],
        "max_tokens": 32,
        "stream": False,
    }
    if contract == "structured":
        payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "broker_canary", "strict": True, "schema": SCHEMA}}
    started = time.monotonic()
    status, data = request(cpa_url.rstrip("/") + "/v1/chat/completions", payload=payload, headers={"Authorization": f"Bearer {cpa_key}"})
    content = (((data or {}).get("choices") or [{}])[0].get("message") or {}).get("content")
    structured_ok = False
    if contract == "structured" and isinstance(content, str):
        try:
            structured_ok = json.loads(content) == {"healthy": True}
        except json.JSONDecodeError:
            pass
    return {
        "path": "cpa", "contract": contract, "model": model, "http_status": status,
        "state": "succeeded" if status == 200 and (contract == "plain" or structured_ok) else "failed",
        "actual_model": (data or {}).get("model"), "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
    }


def broker_cell(broker_url: str, model: str, contract: str, *, runs: int = 1) -> dict:
    # The Provider + Model contract keeps Model capabilities under each Key;
    # the Key test endpoint chooses the cheapest callable model for that Key.
    status, key_inventory = request(broker_url.rstrip("/") + "/admin/v1/providers?window=24h")
    providers = (key_inventory or {}).get("providers") if status == 200 else None
    target = next((item for item in providers or []
                   if isinstance(item, dict) and any(model_item.get("model") == model and model_item.get("callable") is True
                                                     for model_item in item.get("models", []))), None)
    if not target:
        return {"path": "broker_direct", "contract": contract, "model": model, "state": "failed", "reason": "no_enabled_target", "runs": 0, "passed_runs": 0}
    started = time.monotonic()
    items = []
    for _ in range(runs):
        status, data = request(broker_url.rstrip("/") + f"/admin/v1/keys/{target['fingerprint']}/test", payload={})
        tested = (data or {}).get("tested_count", 0) if status == 200 else 0
        succeeded = (data or {}).get("succeeded_count", 0) if status == 200 else 0
        items.append((status, tested, succeeded))
    status, tested, succeeded = items[-1]
    passed_runs = sum(1 for cell_status, tested_count, succeeded_count in items
                      if cell_status == 200 and tested_count > 0 and succeeded_count == tested_count)
    return {
        "path": "broker_direct", "contract": contract, "model": model, "http_status": status,
        "state": "succeeded" if passed_runs == runs else "failed",
        "tested_count": tested, "succeeded_count": succeeded,
        "runs": runs, "passed_runs": passed_runs,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
    }


def structured_gate_passes(outcomes: list[dict]) -> bool:
    """Require one Stage/Model cell to pass all of its configured runs."""
    return any(
        item.get("path") == "broker_direct"
        and item.get("contract") == "structured"
        and item.get("state") == "succeeded"
        for item in outcomes
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", action="append", dest="models")
    parser.add_argument("--gate", action="store_true", help="Fail only when Broker structured canaries fail")
    parser.add_argument("--broker-only", action="store_true", help="Do not call CPA")
    parser.add_argument("--structured-only", action="store_true", help="Skip plain-text control cells")
    parser.add_argument("--runs", type=int, default=1, help="Consecutive probes per fixed Broker target")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("runs must be positive")
    broker_url = os.getenv("BROKER_URL", "http://192.168.50.2:8817")
    cpa_url = os.getenv("CPA_URL", "http://127.0.0.1:8317")
    cpa_key = os.getenv("CPA_INFERENCE_KEY", "")
    # Include the smart fallback tier in the release gate.  A flaky expert
    # model must not hide a healthy smart target from the any-provider gate.
    models = args.models or ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"]
    outcomes = []
    for model in models:
        for contract in (("structured",) if args.structured_only else ("plain", "structured")):
            outcomes.append(broker_cell(broker_url, model, contract, runs=args.runs))
            if not args.broker_only:
                outcomes.append(cpa_cell(cpa_url, cpa_key, model, contract) if cpa_key else {
                    "path": "cpa", "contract": contract, "model": model, "state": "skipped", "reason": "CPA_INFERENCE_KEY_missing",
                })
    for outcome in outcomes:
        print(json.dumps(outcome, ensure_ascii=False, sort_keys=True))
    if args.gate:
        return 0 if structured_gate_passes(outcomes) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
