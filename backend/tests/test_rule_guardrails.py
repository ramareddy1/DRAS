from app.memory.rules_store import apply_force_status_rules
from app.models import Rule


def _rule(ceiling):
    return Rule(
        account_id="a", kind="force_status", description="test",
        when={"signature_prefix": "abc123", "max_abs_diff": ceiling},
        then={"status": "match"}, origin="user_rule",
        confidence=0.9, state="active",
    )


def test_rule_fires_under_ceiling():
    r = apply_force_status_rules([_rule(50.0)], "abc123def", "k1", diff_abs=10.0)
    assert r is not None and r.status == "match"


def test_rule_skipped_over_ceiling():
    # A rule taught on small diffs must never swallow a big discrepancy
    r = apply_force_status_rules([_rule(50.0)], "abc123def", "k1", diff_abs=5000.0)
    assert r is None


def test_negative_diff_uses_absolute_value():
    r = apply_force_status_rules([_rule(50.0)], "abc123def", "k1", diff_abs=-5000.0)
    assert r is None


def test_legacy_rule_without_ceiling_still_fires():
    rule = _rule(None)
    del rule.when["max_abs_diff"]
    r = apply_force_status_rules([rule], "abc123def", "k1", diff_abs=5000.0)
    assert r is not None
