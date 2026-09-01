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
        "reply_markdown": {"type": "string", "minLength": 800},
        "needs_fresh_search": {"type": "boolean"},
        "public_search_request": {"type": "null"},
        "propositions": {"type": "array", "minItems": 3, "maxItems": 5, "items": {"$ref": "#/$defs/proposition"}},
        "actions": {"type": "array", "minItems": 1, "maxItems": 1, "items": {"oneOf": [
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
                "message_id": {"type": "string", "const": "synthetic-message"},
                "start": {"type": "integer", "const": 0},
                "end": {"type": "integer", "const": 18},
                "quote": {"type": "string", "const": "synthetic evidence"},
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
    diagnostics = [item.get("diagnostic") for item in attempts if isinstance(item, dict) and isinstance(item.get("diagnostic"), dict)]
    return {
        "count": len(attempts),
        "statuses": dict(sorted(statuses.items())),
        "max_event_gap_ms": max((item.get("max_event_gap_ms") or 0 for item in diagnostics), default=0),
        "max_output_chars": max((item.get("output_chars") or 0 for item in diagnostics), default=0),
        "max_progress_events": max((item.get("progress_event_count") or 0 for item in diagnostics), default=0),
    }


def run_once(base_url, token_count, deadline_ms, output_token_limit):
    prompt = (
        "synthetic evidence\n" + "e " * token_count
        + "\nReturn exactly one JSON object. Write a coherent reply_markdown of at least 800 characters "
        "with a summary, three independently reasoned findings, counterevidence, and next-session implications. "
        "Set needs_fresh_search=false and public_search_request=null. Produce 3 to 5 distinct propositions "
        "using external_claim or ai_inference, meaningful subject/predicate/object_json and calibrated confidence. "
        "Produce exactly one analysis.request action with a concrete subject, time_scope and goal. Every "
        "proposition and action must use source_span message_id=synthetic-message, start=0, end=18, "
        "quote=synthetic evidence. Do not use empty arrays or placeholder prose."
    )
    payload = {
        "prompt": prompt, "intellect": "smart", "effort": "medium",
        "deadline_ms": deadline_ms, "output_token_limit": output_token_limit, "output_schema": SCHEMA,
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
        "output_chars": len(final["output_text"]),
        "reply_chars": len(output["reply_markdown"]),
        "propositions": len(output["propositions"]),
        "actions": len(output["actions"]),
        "prompt_chars": len(prompt), "schema_hash": schema_hash(),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.getenv("BROKER_URL", "http://192.168.50.2:8817"))
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--token-count", type=int, default=72_000)
    parser.add_argument("--deadline-ms", type=int, default=260_000)
    parser.add_argument("--output-token-limit", type=int, default=2_000)
    args = parser.parse_args()
    if args.runs < 1 or args.token_count < 1 or args.deadline_ms < 1 or args.output_token_limit < 1:
        parser.error("runs, token-count, deadline-ms, and output-token-limit must be positive")
    failures = 0
    for run in range(1, args.runs + 1):
        passed, summary = run_once(args.url, args.token_count, args.deadline_ms, args.output_token_limit)
        failures += not passed
        print(json.dumps({"run": run, "passed": passed, **summary}, ensure_ascii=False), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
