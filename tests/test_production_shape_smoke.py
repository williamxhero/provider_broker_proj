from scripts.production_shape_smoke import sized_prompt
from scripts.transport_matrix import broker_cell, structured_gate_passes


def test_sized_prompt_matches_real_character_and_utf8_shape_without_original_text():
    prompt = sized_prompt(6024, 6766)

    assert len(prompt) == 6024
    assert len(prompt.encode("utf-8")) == 6766
    assert "Return exactly one JSON object" in prompt


def test_structured_canary_requires_three_successes_on_the_same_target(monkeypatch):
    calls = []

    def fake_request(url, *, payload=None, headers=None, timeout=70):
        if url.endswith("/providers?window=24h"):
            return 200, {"providers": [{"enabled": True, "calibrated": True, "models": ["gpt-5.6-luna"], "fingerprint": "fp"}]}
        calls.append(payload)
        return 200, {"items": [{"state": "succeeded"}]}

    monkeypatch.setattr("scripts.transport_matrix.request", fake_request)
    result = broker_cell("http://broker", "gpt-5.6-luna", "structured", runs=3)

    assert result["state"] == "succeeded"
    assert result["runs"] == result["passed_runs"] == 3
    assert len(calls) == 3
    assert {call["fingerprint"] for call in calls} == {"fp"}


def test_structured_canary_fails_when_no_enabled_target_exists(monkeypatch):
    def fake_request(url, *, payload=None, headers=None, timeout=70):
        if url.endswith("/providers?window=24h"):
            return 200, {"providers": []}
        raise AssertionError("probe must not run without a target")

    monkeypatch.setattr("scripts.transport_matrix.request", fake_request)
    result = broker_cell("http://broker", "gpt-5.6-luna", "structured", runs=3)

    assert result["state"] == "failed"
    assert result["reason"] == "no_enabled_target"


def test_release_gate_accepts_one_real_target_when_other_models_are_unavailable():
    assert structured_gate_passes([
        {"path": "broker_direct", "contract": "structured", "state": "failed"},
        {"path": "broker_direct", "contract": "structured", "state": "succeeded", "runs": 3, "passed_runs": 3},
    ])
    assert not structured_gate_passes([
        {"path": "broker_direct", "contract": "structured", "state": "failed"},
    ])
