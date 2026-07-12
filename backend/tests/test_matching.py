import pandas as pd

from app.tools.matching import match_by_key, norm_key


def test_norm_key_strips_prefix_and_leading_zeros():
    assert norm_key("#1001") == "1001"
    assert norm_key("ORD-0042") == "42"
    assert norm_key("pi_ABC123") == "abc123"


def test_norm_key_all_zeros_survives():
    # lstrip("0") on "000" yields "", falls back to the lowercased original
    assert norm_key("000") == "000"


def test_exact_match_preferred_over_fuzzy():
    a = pd.DataFrame({"k": ["#1001"]})
    b = pd.DataFrame({"k": ["#1001", "1001"]})
    res = match_by_key(a, b, "k", "k")
    assert len(res.matches) == 1
    assert res.matches[0].match_type == "exact"
    assert res.matches[0].key_b == "#1001"


def test_duplicate_keys_first_wins_rest_unmatched():
    # Documents current 1:1 behavior — Task 10 changes this via aggregation
    a = pd.DataFrame({"k": ["1", "1"], "v": [10, 20]})
    b = pd.DataFrame({"k": ["1"], "v": [10]})
    res = match_by_key(a, b, "k", "k")
    assert len(res.matches) == 1
    assert res.unmatched_a_idx == [1]


def test_no_cross_matching_of_unrelated_keys():
    a = pd.DataFrame({"k": ["A1", "B2"]})
    b = pd.DataFrame({"k": ["C3", "D4"]})
    res = match_by_key(a, b, "k", "k")
    assert res.matches == []
    assert len(res.unmatched_a_idx) == 2
    assert len(res.unmatched_b_idx) == 2
