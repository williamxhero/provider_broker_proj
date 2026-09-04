from scripts.stock_planner_canary import _requirement_rows


def test_requirement_rows_accepts_current_chat_research_packet():
    rows = [{"key": "market_close", "blocking": True, "description": "closing state"}]

    assert _requirement_rows({"evidence_requirements": rows}) == rows


def test_requirement_rows_keeps_legacy_m0_research_contract_compatibility():
    rows = [{"key": "portfolio_close"}]

    assert _requirement_rows({"evidence_contract": {"requirements": rows}}) == rows
