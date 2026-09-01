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

MEMORY_RESEARCH_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object", "additionalProperties": False,
    "required": ["operation", "query", "episode_id", "url", "source_reference"],
    "properties": {
        "operation": {"enum": ["search", "expand", "related", "web_search", "web_read", "markethub_quote", "archive_article", "complete"]},
        "query": {"type": ["string", "null"]},
        "episode_id": {"type": ["string", "null"]},
        "url": {"type": ["string", "null"]},
        "source_reference": {"type": ["object", "null"]},
    },
}

RESEARCH_PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["version", "operations"],
    "properties": {"version": {"type": "integer", "const": 1}, "operations": {
        "type": "array", "maxItems": 24, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["requirement_key", "backend", "operation", "arguments", "fallback_backends"],
            "properties": {
                "requirement_key": {"type": "string", "minLength": 1},
                "backend": {"type": "string", "enum": ["market", "gateway"]},
                "operation": {"type": "string", "enum": ["market_snapshot", "market_breadth", "sector_snapshot", "holding_snapshot", "web_search", "web_read", "web_browser"]},
                "arguments": {"type": "object", "additionalProperties": False,
                    "required": ["query", "categories", "url", "symbol", "render", "session_id", "actions"],
                    "properties": {
                        "query": {"type": ["string", "null"]}, "categories": {"type": ["string", "null"]},
                        "url": {"type": ["string", "null"]}, "symbol": {"type": ["string", "null"]},
                        "render": {"type": ["string", "null"]}, "session_id": {"type": ["string", "null"]},
                        "actions": {"type": ["array", "null"], "items": {"type": "object", "additionalProperties": False,
                            "required": ["type", "url", "ref", "element", "ms", "pixels"],
                            "properties": {"type": {"type": "string", "enum": ["navigate", "click", "wait", "scroll", "snapshot", "screenshot", "close"]},
                                "url": {"type": ["string", "null"]}, "ref": {"type": ["string", "null"]},
                                "element": {"type": ["string", "null"]}, "ms": {"type": ["integer", "null"]}, "pixels": {"type": ["integer", "null"]}}}},
                    }},
                "fallback_backends": {"type": "array", "items": {"type": "string", "enum": ["market", "gateway"]}},
            }}},
    },
}


def schema_hash(schema):
    encoded = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
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


def default_prompt(token_count):
    return (
        "synthetic evidence\n" + "e " * token_count
        + "\nReturn exactly one JSON object. Write a coherent reply_markdown of at least 800 characters "
        "with a summary, three independently reasoned findings, counterevidence, and next-session implications. "
        "Set needs_fresh_search=false and public_search_request=null. Produce 3 to 5 distinct propositions "
        "using external_claim or ai_inference, meaningful subject/predicate/object_json and calibrated confidence. "
        "Produce exactly one analysis.request action with a concrete subject, time_scope and goal. Every "
        "proposition and action must use source_span message_id=synthetic-message, start=0, end=18, "
        "quote=synthetic evidence. Do not use empty arrays or placeholder prose."
    )


def sized_prompt(char_count, byte_count, contract="cognition"):
    instruction = (
        "\nReturn exactly one JSON object matching the authoritative schema. Write a substantial "
        "reply_markdown, multiple meaningful propositions, and one concrete action. Every source_span "
        "must refer to message_id synthetic-message with a non-empty exact quote. Do not emit prose "
        "outside JSON, wrappers, placeholders, empty arrays, or undeclared properties."
    )
    if contract == "memory-research":
        instruction = (
            "\nChoose the next private-memory operation needed before replying. The synthetic frozen "
            "snapshot already contains sufficient evidence, so choose operation complete. Set query, "
            "episode_id, url, and source_reference to null. Return only the JSON object required by the "
            "authoritative schema, with every required property and no prose or undeclared property."
        )
    extra_bytes = byte_count - char_count
    if char_count < len(instruction) or extra_bytes < 0:
        raise ValueError("requested prompt shape is smaller than its instruction")
    three_byte_chars, two_byte_chars = divmod(extra_bytes, 2)
    ascii_chars = char_count - len(instruction) - three_byte_chars - two_byte_chars
    if ascii_chars < 0:
        raise ValueError("requested prompt byte/character shape is impossible")
    prompt = "证" * three_byte_chars + "é" * two_byte_chars + "e" * ascii_chars + instruction
    if len(prompt) != char_count or len(prompt.encode("utf-8")) != byte_count:
        raise AssertionError("constructed prompt shape does not match requested size")
    return prompt


def research_plan_prompt(char_count, byte_count):
    urls = {
        "current_market_state": "https://public.example.test/market-close",
        "events": "https://public.example.test/events",
    }
    packet = {
        "task_key": "daily.review.1520", "stage": "research_plan", "as_of": "2026-09-01T07:20:00Z",
        "evidence_contract": {"version": 3, "requirements": [
            {"key": key, "blocking": True, "window": {"mode": "exact", "start": "2026-09-01T07:00:00Z", "end": "2026-09-01T07:00:00Z"}}
            for key in urls
        ]},
        "market_time_context": {"requirements": [{"requirement_key": "current_market_state", "start_utc": "2026-09-01T07:00:00Z", "start_local": "2026-09-01T15:00:00+08:00", "is_local_market_close": True}]},
        "research_scope": {"standing_questions": ["synthetic public market close and event evidence"]},
        "coverage_gaps": list(urls), "repair_round": 0, "available_backends": ["gateway"],
        "research_discoveries": [{"requirement_key": key, "url": url, "source_kind": "public", "snippet": ""} for key, url in urls.items()],
        "instruction": "Return a version 1 plan that covers every blocking requirement. For each supplied discovery, use gateway web_read with its exact URL. Do not use market or web_search. Use null for unused arguments and an empty fallback_backends array.",
    }
    envelope = {"instruction": "Return only one JSON object matching output_schema. Do not add prose or Markdown fences.", "output_schema": RESEARCH_PLAN_SCHEMA, "input": packet}
    base = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    extra_bytes = byte_count - char_count
    cjk_chars = extra_bytes // 2
    if extra_bytes % 2 or cjk_chars < 0:
        raise ValueError("research-plan prompt byte difference must be even and non-negative")
    ascii_chars = char_count - len(base) - cjk_chars
    if ascii_chars < 0:
        raise ValueError("research-plan prompt target is too small")
    packet["research_discoveries"][0]["snippet"] = "证" * cjk_chars + "e" * ascii_chars
    prompt = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(prompt) != char_count or len(prompt.encode("utf-8")) != byte_count:
        raise AssertionError("constructed research-plan prompt shape does not match requested size")
    return prompt


def run_once(base_url, prompt, schema, deadline_ms, output_token_limit, intellect="smart"):
    payload = {
        "prompt": prompt, "intellect": intellect, "effort": "medium",
        "deadline_ms": deadline_ms, "output_token_limit": output_token_limit, "output_schema": schema,
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
            "prompt_chars": len(prompt), "prompt_bytes": len(prompt.encode("utf-8")), "schema_hash": schema_hash(schema),
        }
    except Exception as exc:
        return False, {
            "http_status": "transport_error", "error_type": type(exc).__name__,
            "prompt_chars": len(prompt), "prompt_bytes": len(prompt.encode("utf-8")), "schema_hash": schema_hash(schema),
        }
    try:
        output = json.loads(final["output_text"])
        Draft202012Validator(schema).validate(output)
    except Exception as exc:
        return False, {
            "http_status": 200, "error_type": type(exc).__name__,
            "attempts": safe_attempts(final.get("attempts") if isinstance(final, dict) else None),
            "prompt_chars": len(prompt), "prompt_bytes": len(prompt.encode("utf-8")), "schema_hash": schema_hash(schema),
        }
    if schema_hash(schema) == schema_hash(RESEARCH_PLAN_SCHEMA):
        operations = output.get("operations") if isinstance(output, dict) else []
        required = {"current_market_state", "events"}
        reads = {row.get("requirement_key") for row in operations if isinstance(row, dict) and row.get("backend") == "gateway" and row.get("operation") == "web_read" and str((row.get("arguments") or {}).get("url") or "").startswith("https://public.example.test/")}
        if not isinstance(operations, list) or not required.issubset(reads):
            return False, {"http_status": 200, "error_type": "BusinessValidationError", "schema_hash": schema_hash(schema), "planned_requirements": sorted(reads)}
        contract_summary = {"operations": len(operations), "planned_requirements": sorted(reads)}
    elif schema_hash(schema) == schema_hash(MEMORY_RESEARCH_SCHEMA):
        contract_summary = {"operation": output.get("operation"), "source_reference_kind": type(output.get("source_reference")).__name__}
    else:
        contract_summary = {
            "reply_chars": len(output["reply_markdown"]), "propositions": len(output["propositions"]), "actions": len(output["actions"]),
        }
    return True, {
        "http_status": 200, "status": final.get("status"),
        "request_id": final.get("request_id"),
        "actual_model": final.get("actual_model"),
        "fulfilled_intellect": final.get("fulfilled_intellect"), "ttft_ms": final.get("ttft_ms"),
        "attempts": safe_attempts(final.get("attempts")),
        "output_chars": len(final["output_text"]),
        **contract_summary,
        "prompt_chars": len(prompt), "prompt_bytes": len(prompt.encode("utf-8")), "schema_hash": schema_hash(schema),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.getenv("BROKER_URL", "http://192.168.50.2:8817"))
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--token-count", type=int, default=72_000)
    parser.add_argument("--deadline-ms", type=int, default=260_000)
    parser.add_argument("--output-token-limit", type=int, default=6_000)
    parser.add_argument("--intellect", choices=("smart", "expert"), default="smart")
    parser.add_argument("--schema-file")
    parser.add_argument("--contract", choices=("cognition", "memory-research", "research-plan"), default="cognition")
    parser.add_argument("--prompt-chars", type=int)
    parser.add_argument("--prompt-bytes", type=int)
    args = parser.parse_args()
    if args.runs < 1 or args.token_count < 1 or args.deadline_ms < 1 or args.output_token_limit < 1:
        parser.error("runs, token-count, deadline-ms, and output-token-limit must be positive")
    if (args.prompt_chars is None) != (args.prompt_bytes is None):
        parser.error("prompt-chars and prompt-bytes must be provided together")
    schema = {"memory-research": MEMORY_RESEARCH_SCHEMA, "research-plan": RESEARCH_PLAN_SCHEMA}.get(args.contract, SCHEMA)
    if args.schema_file:
        with open(args.schema_file, "r", encoding="utf-8") as handle:
            schema = json.load(handle)
    Draft202012Validator.check_schema(schema)
    prompt = (research_plan_prompt(args.prompt_chars, args.prompt_bytes) if args.contract == "research-plan" else sized_prompt(args.prompt_chars, args.prompt_bytes, args.contract)) if args.prompt_chars is not None else default_prompt(args.token_count)
    failures = 0
    for run in range(1, args.runs + 1):
        passed, summary = run_once(args.url, prompt, schema, args.deadline_ms, args.output_token_limit, args.intellect)
        failures += not passed
        print(json.dumps({"run": run, "passed": passed, **summary}, ensure_ascii=False), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
