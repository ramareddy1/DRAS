"""Ambient run scope for tool execution.

`account_id` is deliberately NOT a tool parameter. If the model could supply
it, a hallucinated or injected value would cross account boundaries. The
runtime sets this context variable before the loop starts; tools read it and
the model cannot reach it.
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class RunContext:
    run_id: str
    account_id: str


_CURRENT: ContextVar[Optional[RunContext]] = ContextVar(
    "agent_run_context", default=None,
)


def set_run_context(ctx: Optional[RunContext]) -> Token:
    return _CURRENT.set(ctx)


def reset_run_context(token: Token) -> None:
    _CURRENT.reset(token)


def current_run() -> RunContext:
    ctx = _CURRENT.get()
    if ctx is None:
        raise RuntimeError("no active agent run context")
    return ctx
