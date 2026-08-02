"""Hard caps on a run, enforced by us.

Distinct from `output_config.task_budget`, which is model-aware and makes
the agent pace itself. These caps are the backstop: they stop a run that is
overreaching regardless of what the model intends (spec 2.7). The defaults
match the budget line shown in the prototype's plan block.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

DEFAULT_USD_CAP = 0.40
DEFAULT_TOOL_CALL_CAP = 30
DEFAULT_WALL_CLOCK_S = 120
DEFAULT_TASK_BUDGET_TOKENS = 64_000


@dataclass(frozen=True)
class Budget:
    usd_cap: Optional[float] = DEFAULT_USD_CAP
    tool_call_cap: Optional[int] = DEFAULT_TOOL_CALL_CAP
    wall_clock_s: Optional[int] = DEFAULT_WALL_CLOCK_S
    task_budget_tokens: Optional[int] = DEFAULT_TASK_BUDGET_TOKENS

    @staticmethod
    def default() -> "Budget":
        return Budget()

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Budget":
        base = Budget()
        return Budget(
            usd_cap=d.get("usd_cap", base.usd_cap),
            tool_call_cap=d.get("tool_call_cap", base.tool_call_cap),
            wall_clock_s=d.get("wall_clock_s", base.wall_clock_s),
            task_budget_tokens=d.get("task_budget_tokens", base.task_budget_tokens),
        )


@dataclass
class Spend:
    tool_calls: int = 0
    usd: float = 0.0
    started_at: float = field(default_factory=time.monotonic)

    def record_tool_call(self) -> None:
        self.tool_calls += 1

    def record_llm(self, usd: float) -> None:
        self.usd += usd

    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool_calls": self.tool_calls,
            "usd": round(self.usd, 6),
            "elapsed_s": round(self.elapsed_s(), 3),
        }


def exceeded(budget: Budget, spend: Spend) -> Optional[str]:
    """Return a human-readable reason, or None when still within budget."""
    if budget.tool_call_cap is not None and spend.tool_calls > budget.tool_call_cap:
        return (
            f"tool call cap reached: {spend.tool_calls} of "
            f"{budget.tool_call_cap}"
        )
    if budget.usd_cap is not None and spend.usd > budget.usd_cap:
        return f"budget reached: ${spend.usd:.2f} of ${budget.usd_cap:.2f}"
    if budget.wall_clock_s is not None and spend.elapsed_s() > budget.wall_clock_s:
        return (
            f"wall clock cap reached: {spend.elapsed_s():.0f}s of "
            f"{budget.wall_clock_s}s"
        )
    return None
