from __future__ import annotations

import pytest

from app.agent_runtime import critic


@pytest.fixture(autouse=True)
def _isolate_critic_checks():
    """Snapshot and restore module-global critic checks.
    
    _CHECKS is a Dict[str, List[Check]], and tests call register_check()
    which mutates the module-level dict. Without cleanup, state persists
    across tests. This fixture snapshots before each test and restores
    afterwards by mutating in place (not rebinding), so other modules
    holding references to the original dict see the reset.
    """
    # Snapshot before test, copying both the outer dict and inner lists
    saved_checks = {k: list(v) for k, v in critic._CHECKS.items()}
    
    yield
    
    # Restore after test by mutating in place
    critic._CHECKS.clear()
    critic._CHECKS.update(saved_checks)


def test_unknown_tool_passes_vacuously():
    result = critic.check("some_tool_with_no_checks", {"anything": 1})
    assert result.passed is True
    assert result.failures == []


def test_match_conservation_passes_on_balanced_counts():
    result = critic.check("match_datasets", {
        "rows_in_a": 4, "rows_in_b": 3,
        "matched": 2, "unmatched_a": 2, "unmatched_b": 1,
        "fuzzy_count": 0,
    })
    assert result.passed is True


def test_match_conservation_fails_when_rows_vanish():
    """The whole point: a tool cannot lose rows without the run aborting."""
    result = critic.check("match_datasets", {
        "rows_in_a": 4, "rows_in_b": 3,
        "matched": 2, "unmatched_a": 1, "unmatched_b": 1,
        "fuzzy_count": 0,
    })
    assert result.passed is False
    assert any("side A" in f for f in result.failures)


def test_bind_counts_must_not_exceed_total():
    result = critic.check("bind_columns", {
        "dataset_id": "d", "total_count": 2, "bound_count": 5,
        "mappings": [], "unbound": [],
    })
    assert result.passed is False


def test_custom_checks_register_and_run():
    """Pack invariants use this path in Phase E."""
    critic.register_check(
        "pack_tool",
        lambda out: None if out.get("ok") else "pack invariant violated",
    )
    assert critic.check("pack_tool", {"ok": True}).passed is True

    failed = critic.check("pack_tool", {"ok": False})
    assert failed.passed is False
    assert failed.failures == ["pack invariant violated"]


def test_a_raising_check_is_reported_not_propagated():
    """A broken check must fail the run, not crash the loop."""
    critic.register_check("boom", lambda out: 1 / 0)
    result = critic.check("boom", {})
    assert result.passed is False
    assert any("ZeroDivisionError" in f for f in result.failures)
