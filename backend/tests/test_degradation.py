import pandas as pd


def test_job_completes_without_api_key(monkeypatch, tmp_path):
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("RECONOPS_STUB_LLM", raising=False)

    import importlib
    from app.memory import accounts, rules_store
    importlib.reload(accounts); importlib.reload(rules_store)
    from app.agent import run_job
    from app.models import BindingSet, ReconcileConfig
    from app.tools.binding import bind_columns

    da = pd.DataFrame({"order_id": ["#1", "#2"], "order_total": [10.0, 20.0]})
    db = pd.DataFrame({"order_reference": ["#1", "#2"], "amount": [10.0, 15.0]})
    acc = accounts.create_account()
    rules_store.seed_defaults(acc.id)
    cfg = ReconcileConfig(
        source_a=BindingSet(bindings=bind_columns(da)),
        source_b=BindingSet(bindings=bind_columns(db)),
    )
    out = run_job(account=acc, df_a=da, df_b=db, cfg=cfg, job_id="t")
    assert out.summary.matched == 2
    assert out.insights_status == "unavailable"
    assert out.insights == ""


def test_low_confidence_join_proceeds_with_warning(monkeypatch, tmp_path):
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RECONOPS_STUB_LLM", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-stub")

    import importlib
    from app.memory import accounts, rules_store
    importlib.reload(accounts); importlib.reload(rules_store)
    from app.agent import run_job
    from app.models import BindingSet, ReconcileConfig, SemanticBinding

    # 3 A-keys, 1 shared with B -> overlap 1/3 (< 0.5) with 0.3-confidence
    # bindings on both sides: exactly the case that used to raise AskUser.
    da = pd.DataFrame({"ref": ["x1", "x2", "x3"], "total": [10.0, 20.0, 30.0]})
    db = pd.DataFrame({"memo": ["x1", "zz"], "amount": [10.0, 5.0]})
    cfg = ReconcileConfig(
        source_a=BindingSet(bindings=[SemanticBinding(
            column_name="ref", concept_id="order.id", confidence=0.3,
            provenance="inferred", evidence=[], alternatives=[])]),
        source_b=BindingSet(bindings=[SemanticBinding(
            column_name="memo", concept_id="payment.order_reference", confidence=0.3,
            provenance="inferred", evidence=[], alternatives=[])]),
    )
    acc = accounts.create_account()
    rules_store.seed_defaults(acc.id)
    out = run_job(account=acc, df_a=da, df_b=db, cfg=cfg, job_id="t2")
    assert out.binding_warning is not None
    assert "ref" in out.binding_warning["message"]
    assert out.summary.matched >= 1  # it proceeded anyway
