#!/usr/bin/env python3
"""Independent live broker smoke. Never prints credentials or response bodies on failure."""
import json
import os
import sys
import urllib.error
import urllib.request

base = os.getenv("BROKER_URL", "http://192.168.50.2:8817").rstrip("/")


def request(payload):
    req = urllib.request.Request(
        base + "/v1/generate", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception:
        return "transport_error", None


failures = 0
for intellect, effort in (("standard", "high"), ("smart", "medium"), ("expert", "low")):
    status, data = request({"prompt": "Reply with exactly: provider-broker-ok", "intellect": intellect, "effort": effort, "output_token_limit": 40})
    if status != 200 or not isinstance(data, dict) or data.get("status") != "completed":
        failures += 1
        print(json.dumps({"intellect": intellect, "effort": effort, "http_status": status, "technical_status": "failed"}))
        continue
    print(json.dumps({key: data.get(key) for key in ("status", "provider", "intellect", "fulfilled_intellect", "actual_model", "ttft_ms", "usage", "request_id")}, ensure_ascii=False))

sys.exit(1 if failures else 0)
