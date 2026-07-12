import pandas as pd
import pytest

from app.tools.matching import aggregate_duplicate_keys


def test_duplicate_keys_sum_amounts():
    df = pd.DataFrame({
        "order_id": ["o1", "o1", "o2"],
        "_amt": [60.0, 39.33, 20.0],
    })
    out, info = aggregate_duplicate_keys(df, "order_id")
    assert len(out) == 2
    assert out.loc[out["order_id"] == "o1", "_amt"].iloc[0] == pytest.approx(99.33)
    assert out.loc[out["order_id"] == "o1", "_agg_count"].iloc[0] == 2
    assert out.loc[out["order_id"] == "o2", "_amt"].iloc[0] == pytest.approx(20.0)
    assert info.groups == 1
    assert info.rows_collapsed == 1


def test_no_duplicates_is_passthrough():
    df = pd.DataFrame({"order_id": ["o1", "o2"], "_amt": [1.0, 2.0]})
    out, info = aggregate_duplicate_keys(df, "order_id")
    assert info is None
    assert len(out) == 2
    assert "_agg_count" not in out.columns


def test_normalized_keys_group_together():
    # "#1001" and "1001" share a normalized key -> one group
    df = pd.DataFrame({"k": ["#1001", "1001"], "_amt": [10.0, 5.0]})
    out, info = aggregate_duplicate_keys(df, "k")
    assert len(out) == 1
    assert out["_amt"].iloc[0] == pytest.approx(15.0)
    assert info.rows_collapsed == 1


def test_singleton_nan_amount_survives():
    # A lone row with a missing amount must stay NaN (amounts_missing path),
    # not become 0.0.
    df = pd.DataFrame({"k": ["a", "b", "b"], "_amt": [float("nan"), 1.0, 2.0]})
    out, info = aggregate_duplicate_keys(df, "k")
    assert pd.isna(out.loc[out["k"] == "a", "_amt"].iloc[0])
    assert out.loc[out["k"] == "b", "_amt"].iloc[0] == pytest.approx(3.0)
