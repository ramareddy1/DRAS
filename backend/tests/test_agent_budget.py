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


# Finding 1: Boundary equality tests for strict > comparisons
def test_tool_call_cap_exactly_at_limit_does_not_trip():
    """Exactly at cap should be allowed (uses > not >=)."""
    spend = Spend()
    for _ in range(30):
        spend.record_tool_call()
    assert exceeded(Budget(tool_call_cap=30), spend) is None


def test_tool_call_cap_one_over_limit_trips():
    """One over cap should trip."""
    spend = Spend()
    for _ in range(31):
        spend.record_tool_call()
    reason = exceeded(Budget(tool_call_cap=30), spend)
    assert reason is not None
    assert "tool call" in reason


def test_usd_cap_exactly_at_limit_does_not_trip():
    """Exactly at cap should be allowed (uses > not >=)."""
    spend = Spend()
    spend.record_llm(0.50)
    assert exceeded(Budget(usd_cap=0.50), spend) is None


def test_usd_cap_over_limit_trips():
    """Over cap should trip."""
    spend = Spend()
    spend.record_llm(0.51)
    reason = exceeded(Budget(usd_cap=0.50), spend)
    assert reason is not None
    assert "budget" in reason


def test_wall_clock_cap_safely_under_limit_does_not_trip():
    """Wall clock safely under cap should not trip (avoid race conditions)."""
    spend = Spend(started_at=time.monotonic() - 1)
    assert exceeded(Budget(wall_clock_s=60), spend) is None


def test_wall_clock_cap_comfortably_over_limit_trips():
    """Wall clock comfortably over cap should trip (avoid race conditions)."""
    spend = Spend(started_at=time.monotonic() - 65)
    reason = exceeded(Budget(wall_clock_s=60), spend)
    assert reason is not None
    assert "wall clock" in reason


# Finding 2: Unlimited caps (None means unlimited)
def test_usd_cap_none_means_unlimited():
    """usd_cap=None should allow arbitrary spending."""
    spend = Spend()
    for _ in range(1000):
        spend.record_llm(0.01)  # Spend $10 total
    assert exceeded(Budget(usd_cap=None), spend) is None


def test_wall_clock_cap_none_means_unlimited():
    """wall_clock_s=None should allow arbitrary elapsed time."""
    spend = Spend(started_at=time.monotonic() - 1000)
    assert exceeded(Budget(wall_clock_s=None), spend) is None


def test_from_dict_with_explicit_none_usd_cap_yields_unlimited():
    """Explicit None in dict should yield unlimited cap, not fall back to default."""
    b = Budget.from_dict({"usd_cap": None})
    # Verify the cap is actually None, not the default 0.40
    assert b.usd_cap is None

    # Verify it behaves as unlimited
    spend = Spend()
    spend.record_llm(10.0)  # Spend $10
    assert exceeded(b, spend) is None
