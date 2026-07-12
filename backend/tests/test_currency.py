import pandas as pd
import pytest

from app.tools.amounts import detect_currency_tokens


def test_symbols_normalize_to_iso():
    assert detect_currency_tokens(pd.Series(["$10.00", "$20.00"])) == {"USD"}
    assert detect_currency_tokens(pd.Series(["€10,00"])) == {"EUR"}
    assert detect_currency_tokens(pd.Series(["R$ 99,33"])) == {"BRL"}
    assert detect_currency_tokens(pd.Series(["10.00 GBP"])) == {"GBP"}
    assert detect_currency_tokens(pd.Series(["100 usd"])) == {"USD"}


def test_bare_numbers_yield_empty_set():
    assert detect_currency_tokens(pd.Series([10.0, 20.5])) == set()
    assert detect_currency_tokens(pd.Series(["10.00", "20.50"])) == set()


def test_mixed_series_reports_all_tokens():
    assert detect_currency_tokens(pd.Series(["$5.00", "€5,00"])) == {"USD", "EUR"}


def test_run_job_refuses_mixed_currency(monkeypatch, tmp_path):
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RECONOPS_STUB_LLM", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-stub")

    import importlib
    from app.memory import accounts, rules_store
    importlib.reload(accounts); importlib.reload(rules_store)
    from app.agent import run_job
    from app.models import BindingSet, ReconcileConfig
    from app.tools.binding import bind_columns

    da = pd.DataFrame({"order_id": ["#1", "#2"], "order_total": ["$10.00", "$20.00"]})
    db = pd.DataFrame({"order_reference": ["#1", "#2"], "amount": ["R$ 10,00", "R$ 20,00"]})
    acc = accounts.create_account()
    rules_store.seed_defaults(acc.id)
    cfg = ReconcileConfig(
        source_a=BindingSet(bindings=bind_columns(da)),
        source_b=BindingSet(bindings=bind_columns(db)),
    )
    with pytest.raises(ValueError, match="Cross-currency"):
        run_job(account=acc, df_a=da, df_b=db, cfg=cfg, job_id="cur")

    # Explicit override lets it through
    cfg_override = ReconcileConfig(
        source_a=cfg.source_a, source_b=cfg.source_b, allow_mixed_currency=True,
    )
    out = run_job(account=acc, df_a=da, df_b=db, cfg=cfg_override, job_id="cur2")
    assert out.summary.matched == 2
