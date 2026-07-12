import pandas as pd
import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RECONOPS_STUB_LLM", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-stub")
    import importlib
    from app.memory import accounts, rules_store
    importlib.reload(accounts); importlib.reload(rules_store)
    return accounts, rules_store


def _run(accounts, rules_store, revoke_fees=False):
    from app.agent import run_job
    from app.models import BindingSet, ReconcileConfig
    from app.tools.binding import bind_columns

    acc = accounts.create_account()
    rules_store.seed_defaults(acc.id)
    if revoke_fees:
        for r in rules_store.load_rules(acc.id):
            if r.kind == "fee_pattern":
                rules_store.revoke_rule(acc.id, r.id)
    da = pd.DataFrame({"order_id": ["#1"], "order_total": [100.00]})
    db = pd.DataFrame({"order_reference": ["#1"], "amount": [96.80]})  # Stripe shape
    cfg = ReconcileConfig(
        source_a=BindingSet(bindings=bind_columns(da)),
        source_b=BindingSet(bindings=bind_columns(db)),
    )
    return run_job(account=acc, df_a=da, df_b=db, cfg=cfg, job_id="t")


def test_active_fee_rule_classifies_fee_offset(env):
    out = _run(*env)
    assert out.matched[0]["status"] == "fee_offset"


def test_revoked_fee_rule_changes_the_verdict(env):
    # The whole point of consolidation: turning a fee rule OFF must change
    # classifications. Fails while FEE_PATTERNS re-derives it in amounts.py.
    out = _run(*env, revoke_fees=True)
    assert out.matched[0]["status"] != "fee_offset"
