"""Deterministic post-conditions on tool output.

The agent orchestrates; tools decide. The critic is what makes that
enforceable — every tool's output is checked against invariants that do not
involve the model, and a failure aborts rather than being swallowed
(spec 2.4).

Checks are keyed by tool name so pack invariants can register into the same
table in Phase E without touching core.
"""
from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

Check = Callable[[Dict[str, Any]], Optional[str]]

_CHECKS: Dict[str, List[Check]] = {}


@dataclass
class CriticResult:
    passed: bool
    failures: List[str] = field(default_factory=list)


def register_check(tool_name: str, fn: Check) -> None:
    _CHECKS.setdefault(tool_name, []).append(fn)


def check(tool_name: str, output: Dict[str, Any]) -> CriticResult:
    failures: List[str] = []
    for fn in _CHECKS.get(tool_name, []):
        try:
            problem = fn(output)
        except Exception as exc:  # a broken check fails the run, not the loop
            problem = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        if problem:
            failures.append(problem)
    return CriticResult(passed=not failures, failures=failures)


# --- built-in checks -------------------------------------------------------

def _match_conserves_rows(out: Dict[str, Any]) -> Optional[str]:
    a_in = out.get("rows_in_a", 0)
    b_in = out.get("rows_in_b", 0)
    matched = out.get("matched", 0)
    if matched + out.get("unmatched_a", 0) != a_in:
        return (
            f"row conservation failed on side A: matched {matched} + "
            f"unmatched {out.get('unmatched_a', 0)} != {a_in} rows in"
        )
    if matched + out.get("unmatched_b", 0) != b_in:
        return (
            f"row conservation failed on side B: matched {matched} + "
            f"unmatched {out.get('unmatched_b', 0)} != {b_in} rows in"
        )
    return None


def _bind_counts_are_sane(out: Dict[str, Any]) -> Optional[str]:
    total = out.get("total_count", 0)
    bound = out.get("bound_count", 0)
    if bound > total:
        return f"bound_count {bound} exceeds total_count {total}"
    return None


register_check("match_datasets", _match_conserves_rows)
register_check("bind_columns", _bind_counts_are_sane)
