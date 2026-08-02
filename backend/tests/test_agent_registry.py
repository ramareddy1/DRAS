from __future__ import annotations

import pytest

from app.agent_runtime import registry
from app.agent_runtime.registry import Effect
from app.models import AutonomyLevel


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
    names = [t.__name__ for t in registry.tier1_tools()]
    assert names == sorted(names)


def test_serialization_is_byte_stable_across_calls():
    assert registry.serialize_tools() == registry.serialize_tools()


def test_every_registered_tool_declares_an_effect():
    for tool in registry.tier1_tools():
        assert registry.effect_of(tool.__name__) in set(Effect)


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


