from datetime import datetime

from app.memory import accounts, metrics as metrics_store
from app.models import AccountMetrics


def _metric(job_id: str) -> AccountMetrics:
    return AccountMetrics(
        job_id=job_id, at=datetime.utcnow(), total_rows=10, auto_handled=8,
        needed_user=2, insight_density=0.8, override_rate=0.0, revocation_rate=0.0,
        trust_adjusted_density=0.8, llm_calls=0,
    )


def test_snapshot_and_series_preserve_order():
    acc = accounts.create_account()
    metrics_store.snapshot(acc.id, _metric("j1"))
    metrics_store.snapshot(acc.id, _metric("j2"))

    series = metrics_store.series(acc.id)
    assert [m.job_id for m in series] == ["j1", "j2"]


def test_series_respects_limit_and_keeps_most_recent():
    acc = accounts.create_account()
    for i in range(5):
        metrics_store.snapshot(acc.id, _metric(f"j{i}"))

    series = metrics_store.series(acc.id, limit=2)
    assert [m.job_id for m in series] == ["j3", "j4"]


def test_series_empty_account_returns_empty_list():
    acc = accounts.create_account()
    assert metrics_store.series(acc.id) == []


def test_deleting_account_cascades_to_metrics():
    from app.db.base import session_scope
    from app.db.models import AccountORM, MetricORM

    acc = accounts.create_account()
    metrics_store.snapshot(acc.id, _metric("j1"))

    with session_scope() as s:
        s.delete(s.get(AccountORM, acc.id))

    with session_scope() as s:
        assert s.query(MetricORM).filter(MetricORM.account_id == acc.id).count() == 0
