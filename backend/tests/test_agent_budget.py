from __future__ import annotations

import time

from app.agent_runtime.budget import Budget, Spend, exceeded


def test_default_budget_matches_the_prototype_plan_block():
    b = Budget.default()
    assert b.usd_cap == 0.40
    assert b.tool_call_cap == 30
    assert b.wall_clock_s == 120


def test_from_dict_falls_back_to_defaults_for_missing_keys():
    b = Budget.from_dict({"tool_call_cap": 5})
    assert b.tool_call_cap == 5
    assert b.usd_cap == 0.40


def test_within_budget_returns_none():
    assert exceeded(Budget.default(), Spend()) is None


def test_tool_call_cap_trips():
    spend = Spend()
    for _ in range(6):
        spend.record_tool_call()
    reason = exceeded(Budget.from_dict({"tool_call_cap": 5}), spend)
    assert reason is not None
    assert "tool call" in reason


def test_usd_cap_trips():
    spend = Spend()
    spend.record_llm(0.51)
    reason = exceeded(Budget.from_dict({"usd_cap": 0.50}), spend)
    assert reason is not None
    assert "budget" in reason


def test_wall_clock_cap_trips():
    spend = Spend(started_at=time.monotonic() - 10)
    reason = exceeded(Budget.from_dict({"wall_clock_s": 5}), spend)
    assert reason is not None
    assert "wall clock" in reason


def test_none_cap_means_unlimited():
    spend = Spend()
    for _ in range(1000):
        spend.record_tool_call()
    assert exceeded(Budget(tool_call_cap=None), spend) is None


def test_spend_serializes_for_persistence():
    spend = Spend()
    spend.record_tool_call()
    spend.record_llm(0.02)
    d = spend.to_dict()
    assert d["tool_calls"] == 1
    assert d["usd"] == 0.02
