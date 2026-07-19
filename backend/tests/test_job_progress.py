import pandas as pd


def test_progress_callback_invoked_once_per_matched_row(monkeypatch, tmp_path):
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("RECONOPS_STUB_LLM", raising=False)

    import importlib
    from app.memory import accounts, rules_store
    importlib.reload(accounts); importlib.reload(rules_store)
    from app.agent import run_job
    from app.models import BindingSet, ReconcileConfig
    from app.tools.binding import bind_columns

    da = pd.DataFrame({"order_id": ["#1", "#2", "#3"], "order_total": [10.0, 20.0, 30.0]})
    db = pd.DataFrame({"order_reference": ["#1", "#2", "#3"], "amount": [10.0, 20.0, 30.0]})
    acc = accounts.create_account()
    rules_store.seed_defaults(acc.id)
    cfg = ReconcileConfig(
        source_a=BindingSet(bindings=bind_columns(da)),
        source_b=BindingSet(bindings=bind_columns(db)),
    )

    calls = []
    out = run_job(account=acc, df_a=da, df_b=db, cfg=cfg, job_id="t",
                  progress_cb=lambda done, total: calls.append((done, total)))

    assert calls == [(1, 3), (2, 3), (3, 3)]
    assert out.summary.matched == 3


def test_progress_cb_is_optional(monkeypatch, tmp_path):
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("RECONOPS_STUB_LLM", raising=False)

    import importlib
    from app.memory import accounts, rules_store
    importlib.reload(accounts); importlib.reload(rules_store)
    from app.agent import run_job
    from app.models import BindingSet, ReconcileConfig
    from app.tools.binding import bind_columns

    da = pd.DataFrame({"order_id": ["#1"], "order_total": [10.0]})
    db = pd.DataFrame({"order_reference": ["#1"], "amount": [10.0]})
    acc = accounts.create_account()
    rules_store.seed_defaults(acc.id)
    cfg = ReconcileConfig(
        source_a=BindingSet(bindings=bind_columns(da)),
        source_b=BindingSet(bindings=bind_columns(db)),
    )
    out = run_job(account=acc, df_a=da, df_b=db, cfg=cfg, job_id="t2")  # no progress_cb
    assert out.summary.matched == 1
