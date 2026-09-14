import pytest

from scripts.stock_planner_canary import _requirement_rows, _runtime_source


def test_runtime_source_discovers_current_companion_layout(tmp_path):
    runtime = tmp_path / "runtime" / "ai_trading_companion"
    runtime.mkdir(parents=True)

    assert _runtime_source(tmp_path) == runtime.parent


def test_runtime_source_fails_closed_when_companion_is_not_installed(tmp_path):
    with pytest.raises(RuntimeError, match="needs_repair: installed companion runtime package"):
        _runtime_source(tmp_path)


def test_requirement_rows_accepts_current_chat_research_packet():
    rows = [{"key": "market_close", "blocking": True, "description": "closing state"}]

    assert _requirement_rows({"evidence_requirements": rows}) == rows


def test_requirement_rows_keeps_legacy_m0_research_contract_compatibility():
    rows = [{"key": "portfolio_close"}]

    assert _requirement_rows({"evidence_contract": {"requirements": rows}}) == rows
