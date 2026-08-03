"""Tool registration, effect classification, and canonical serialization.

Two invariants this module exists to enforce:

1. Sorted, byte-stable serialization. Prompt caching is a byte-exact prefix
   match and `tools` renders before `system`, so unstable ordering silently
   destroys the cache for every account at once — no error, just
   `cache_creation_input_tokens: 0` forever.

2. Every tool declares an effect, which is what makes the autonomy dial
   enforceable rather than decorative (spec 2.3).

Unknown tools fail closed: `requires_gate` returns True for anything it has
not seen, so a tool that skips registration cannot run unsupervised.
"""
from __future__ import annotations

import json
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from anthropic import beta_tool

from ..models import AutonomyLevel


class Effect(str, Enum):
    read = "read"
    external = "external"
    write = "write"


_TOOLS: Dict[str, Any] = {}
_EFFECTS: Dict[str, Effect] = {}


def record_effect(tool_name: str, effect: Effect) -> None:
    """Declare an effect for a tool defined outside this registry.

    Connection tools (`provider.verb`) and pack tools are tier-2 and reach
    the model through tool search, but the gate still needs their effect.
    """
    _EFFECTS[tool_name] = effect


def register(effect: Effect) -> Callable:
    """Decorate a plain function as a tier-1 agent tool."""

    def wrap(fn: Callable):
        tool = beta_tool(fn)
        name = fn.__name__
        _TOOLS[name] = tool
        _EFFECTS[name] = effect
        return tool

    return wrap


def tier1_tools() -> List[Any]:
    """Registered tools, sorted by name. The sort is load-bearing."""
    return [_TOOLS[name] for name in sorted(_TOOLS)]


def effect_of(tool_name: str) -> Effect:
    return _EFFECTS.get(tool_name, Effect.write)


def callable_for(tool_name: str) -> Optional[Any]:
    """The registered callable for a tool name, or None.

    Dispatch goes through the registry rather than a module attribute
    lookup, so a name is executable exactly when it is registered — and
    registration is also what assigns its effect. The two cannot drift.
    """
    return _TOOLS.get(tool_name)


def serialize_tools() -> str:
    """Canonical JSON of the tier-1 registry — the cache-stability check."""
    return json.dumps(
        [t.to_dict() for t in tier1_tools()],
        sort_keys=True,
        separators=(",", ":"),
    )


def requires_gate(tool_name: str, autonomy: AutonomyLevel) -> bool:
    """True when this call must pause for the user before executing."""
    if autonomy is AutonomyLevel.observe:
        return True

    effect = _EFFECTS.get(tool_name)
    if effect is None:
        return True  # fail closed

    if effect is Effect.read:
        return False
    if effect is Effect.external:
        return autonomy is not AutonomyLevel.auto
    return True  # write always gates
