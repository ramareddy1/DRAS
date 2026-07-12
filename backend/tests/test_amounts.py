import pandas as pd

from app.tools.amounts import classify_amount_diff, coerce_amount


def test_coerce_amount_currency_symbols_and_parens():
    s = pd.Series(["$1,234.56", "(100)", "€50.00", "abc"])
    out = coerce_amount(s)
    assert out.iloc[0] == 1234.56
    assert out.iloc[1] == -100.0
    assert out.iloc[2] == 50.0
    assert pd.isna(out.iloc[3])


def test_within_tolerance_is_match():
    status, conf, ev, alts = classify_amount_diff(0.005, 0.00005, 100.0, 99.995, 0.01, 0.005)
    assert status == "match"


def test_stripe_fee_shape_detected():
    a = 100.00
    b = round(a - (a * 0.029 + 0.30), 2)  # 96.80
    status, conf, ev, alts = classify_amount_diff(a - b, (a - b) / a, a, b, 0.01, 0.005)
    assert status == "fee_offset"


def test_major_threshold():
    status, conf, ev, alts = classify_amount_diff(150.0, 0.15, 1000.0, 850.0, 0.01, 0.005)
    assert status == "major"
