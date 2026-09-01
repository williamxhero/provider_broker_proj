#!/usr/bin/env python3
"""Replay the production cognition shape without retaining sensitive content."""
import argparse
from collections import Counter
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

from jsonschema import Draft202012Validator


SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["reply_markdown", "needs_fresh_search", "public_search_request", "propositions", "actions"],
    "properties": {
        "reply_markdown": {"type": ["string", "null"]},
        "needs_fresh_search": {"type": "boolean"},
        "public_search_request": {"anyOf": [{"type": "null"}, {"$ref": "#/$defs/search_request"}]},
        "propositions": {"type": "array", "items": {"$ref": "#/$defs/proposition"}},
        "actions": {"type": "array", "items": {"oneOf": [
            {"$ref": "#/$defs/analysis_request"},
            {"$ref": "#/$defs/workflow_proposal"},
        ]}},
    },
    "$defs": {
        "search_request": {
            "type": "object", "additionalProperties": False,
            "required": ["topics", "questions"],
            "properties": {
                "topics": {"type": "array", "items": {"type": "string"}},
                "questions": {"type": "array", "items": {"type": "string"}},
            },
        },
        "source_span": {
            "type": "object", "additionalProperties": False,
            "required": ["message_id", "start", "end", "quote"],
            "properties": {
                "message_id": {"type": "string"}, "start": {"type": "integer", "minimum": 0},
                "end": {"type": "integer", "minimum": 0}, "quote": {"type": "string", "minLength": 1},
            },
        },
        "proposition": {
            "type": "object", "additionalProperties": False,
            "required": ["kind", "subject", "predicate", "object_json", "confidence", "source_span"],
            "properties": {
                "kind": {"type": "string", "enum": ["user_fact", "user_view", "external_claim", "ai_inference"]},
                "subject": {"type": "string"}, "predicate": {"type": "string"},
                "object_json": {"type": "string"},
                "confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
                "source_span": {"$ref": "#/$defs/source_span"},
            },
        },
        "analysis_request": {
            "type": "object", "additionalProperties": False,
            "required": ["action_type", "subject", "time_scope", "goal", "source_span"],
            "properties": {
                "action_type": {"type": "string", "const": "analysis.request"},
                "subject": {"type": "string", "minLength": 1},
                "time_scope": {"type": "string", "minLength": 1},
                "goal": {"type": "string", "minLength": 1},
                "source_span": {"$ref": "#/$defs/source_span"},
            },
        },
        "workflow_proposal": {
            "type": "object", "additionalProperties": False,
            "required": ["action_type", "category", "evidence", "source_span"],
            "properties": {
                "action_type": {"type": "string", "const": "workflow.propose"},
                "category": {"type": "string", "enum": ["workflow_efficiency", "search_coverage", "investment_method"]},
                "evidence": {"type": "array", "items": {"type": "string"}},
                "source_span": {"$ref": "#/$defs/source_span"},
            },
        },
    },
}


def schema_hash():
    encoded = json.dumps(SCHEMA, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def safe_attempts(value):
    attempts = value if isinstance(value, list) else []
    statuses = Counter(item.get("status", "unknown") for item in attempts if isinstance(item, dict))
    return {"count": len(attempts), "statuses": dict(sorted(statuses.items()))}


def run_once(base_url, token_count, deadline_ms):
    prompt = (
        "Synthetic evidence follows. " + "e " * token_count
        + " Return exactly one JSON object. Use null for reply_markdown and public_search_request, "
        "false for needs_fresh_search, and empty arrays for propositions and actions."
    )
    payload = {
        "prompt": prompt, "intellect": "smart", "effort": "medium",
        "deadline_ms": deadline_ms, "output_token_limit": 4096, "output_schema": SCHEMA,
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v1/generate/stream",
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    final = None
    try:
        with urllib.request.urlopen(request, timeout=deadline_ms / 1000 + 30) as response:
            if response.status != 200:
                raise RuntimeError(f"unexpected HTTP status {response.status}")
            event = None
            for raw in response:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:") and event == "final":
                    final = json.loads(line[5:].strip())
    except urllib.error.HTTPError as exc:
        try:
            failure = json.loads(exc.read().decode("utf-8"))
        except Exception:
            failure = {}
        return False, {
            "http_status": exc.code, "attempts": safe_attempts(failure.get("attempts")),
            "prompt_chars": len(prompt), "schema_hash": schema_hash(),
        }
    except Exception as exc:
        return False, {
            "http_status": "transport_error", "error_type": type(exc).__name__,
            "prompt_chars": len(prompt), "schema_hash": schema_hash(),
        }
    try:
        output = json.loads(final["output_text"])
        Draft202012Validator(SCHEMA).validate(output)
    except Exception as exc:
        return False, {
            "http_status": 200, "error_type": type(exc).__name__,
            "attempts": safe_attempts(final.get("attempts") if isinstance(final, dict) else None),
            "prompt_chars": len(prompt), "schema_hash": schema_hash(),
        }
    return True, {
        "http_status": 200, "status": final.get("status"),
        "actual_model": final.get("actual_model"),
        "fulfilled_intellect": final.get("fulfilled_intellect"), "ttft_ms": final.get("ttft_ms"),
        "attempts": safe_attempts(final.get("attempts")),
        "prompt_chars": len(prompt), "schema_hash": schema_hash(),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.getenv("BROKER_URL", "http://192.168.50.2:8817"))
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--token-count", type=int, default=72_000)
    parser.add_argument("--deadline-ms", type=int, default=300_000)
    args = parser.parse_args()
    if args.runs < 1 or args.token_count < 1 or args.deadline_ms < 1:
        parser.error("runs, token-count, and deadline-ms must be positive")
    failures = 0
    for run in range(1, args.runs + 1):
        passed, summary = run_once(args.url, args.token_count, args.deadline_ms)
        failures += not passed
        print(json.dumps({"run": run, "passed": passed, **summary}, ensure_ascii=False), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
