import pandas as pd

from app.tools.binding import bind_columns


def _top_concept(df, col):
    for b in bind_columns(df):
        if b.column_name == col:
            return b.concept_id
    return None


def test_olist_payment_columns_bind():
    df = pd.DataFrame({
        "order_id": ["4244733e06e7ecb4970a6e2683c13e61"],
        "payment_value": [99.33],
        "payment_type": ["credit_card"],
    })
    assert _top_concept(df, "payment_value") == "payment.amount"
    assert _top_concept(df, "payment_type") == "payment.method"


def test_olist_order_columns_bind():
    df = pd.DataFrame({
        "order_id": ["e481f51cbdc54678b7cc49136f2d6af7"],
        "order_purchase_timestamp": ["2017-10-02 10:56:33"],
        "order_status": ["delivered"],
    })
    assert _top_concept(df, "order_purchase_timestamp") == "date.event"
    assert _top_concept(df, "order_status") == "status.value"


def test_stripe_export_headers_bind():
    df = pd.DataFrame({
        "Created (UTC)": ["2026-07-01 10:00:00"],
        "Amount": [100.0],
        "Fee": [3.2],
        "Converted Amount": [96.8],
    })
    assert _top_concept(df, "Created (UTC)") == "date.event"
    assert _top_concept(df, "Fee") == "payment.fee"
    assert _top_concept(df, "Amount") == "payment.amount"
    assert _top_concept(df, "Converted Amount") == "payment.amount"
