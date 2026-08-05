from __future__ import annotations

import pytest

from app.agent_runtime import registry
from app.agent_runtime.registry import Effect
from app.models import AutonomyLevel

# Imported for its import-time side effect: every @register(...) decorated
# tool in tools_core registers itself into the module-global registry on
# import. Without this import, running this file in isolation (e.g.
# `pytest tests/test_agent_registry.py`) leaves the registry empty and the
# name/effect assertions below would loop zero times and pass vacuously.
# Do not remove even though nothing here calls it directly.
from app.agent_runtime import tools_core  # noqa: F401


@pytest.fixture(autouse=True)
def _isolate_registry_state():
    """Snapshot and restore module-global registry state.
    
    Protects against state leakage: tests call registry.record_effect() to
    populate _EFFECTS directly, and without cleanup, state persists across tests.
    This fixture ensures each test runs against a clean registry, preventing
    test-execution-order dependencies that become critical when Task 6 adds
    real tools with @register(...) decorators.
    """
    # Snapshot before test
    saved_tools = registry._TOOLS.copy()
    saved_effects = registry._EFFECTS.copy()
    
    yield
    
    # Restore after test by mutating in place (not rebinding), so other
    # modules holding references to the original dicts see the reset
    registry._TOOLS.clear()
    registry._TOOLS.update(saved_tools)
    registry._EFFECTS.clear()
    registry._EFFECTS.update(saved_effects)


def test_registry_serializes_sorted_by_name():
    """Prompt caching is a byte-exact prefix match (spec 2.5)."""
    tools = registry.tier1_tools()
    assert len(tools) > 0, "registry is empty — did the tools_core import get removed?"
    names = [t.name for t in tools]
    assert names == sorted(names)


def test_serialization_is_byte_stable_across_calls():
    assert registry.serialize_tools() == registry.serialize_tools()


def test_every_registered_tool_declares_an_effect():
    tools = registry.tier1_tools()
    assert len(tools) > 0, "registry is empty — did the tools_core import get removed?"
    for tool in tools:
        assert registry.effect_of(tool.name) in set(Effect)


def test_read_tools_run_freely_at_assist():
    registry.record_effect("profile_schema", Effect.read)
    assert registry.requires_gate("profile_schema", AutonomyLevel.assist) is False


def test_external_tools_pause_at_assist_but_not_auto():
    registry.record_effect("shopify.pull_orders", Effect.external)
    assert registry.requires_gate("shopify.pull_orders", AutonomyLevel.assist) is True
    assert registry.requires_gate("shopify.pull_orders", AutonomyLevel.auto) is False


def test_write_tools_gate_at_every_level():
    registry.record_effect("create_rule", Effect.write)
    for level in AutonomyLevel:
        assert registry.requires_gate("create_rule", level) is True


def test_observe_gates_everything_including_reads():
    registry.record_effect("profile_schema", Effect.read)
    assert registry.requires_gate("profile_schema", AutonomyLevel.observe) is True


def test_unknown_tool_is_gated_rather_than_allowed():
    """Fail closed: an unregistered tool must never run unsupervised."""
    assert registry.requires_gate("not_a_real_tool", AutonomyLevel.auto) is True


def test_macro_tool_is_dispatchable():
    """The gate is the only reason this has not broken yet.

    `run_reconciliation` lives in tools_macro, not tools_core, so the old
    attribute-lookup dispatch could not find it. It always gated, so the
    call never reached dispatch — until resume executes an approved one.
    """
    from app.agent_runtime import runtime, tools_macro  # noqa: F401

    assert registry.callable_for("run_reconciliation") is not None


def test_callable_for_returns_none_for_an_unregistered_name():
    assert registry.callable_for("not_a_real_tool") is None
