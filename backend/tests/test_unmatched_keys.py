from app.memory.triage import _first_key_value, signature_for_unmatched


def test_signature_uses_explicit_key_column():
    # Key column named outside the hardcoded guess list — the resolved
    # binding must anchor the signature, not dict order.
    row = {"weird_ref_col": "SUB-991", "amount": 5.0}
    row2 = {"amount": 9.0, "weird_ref_col": "SUB-442"}
    s1 = signature_for_unmatched("a", row, key_col="weird_ref_col")
    s2 = signature_for_unmatched("a", row2, key_col="weird_ref_col")
    assert s1 == s2  # same "SUB-" prefix -> same signature, any dict order


def test_signature_falls_back_to_guess_list():
    s1 = signature_for_unmatched("a", {"order_id": "#1001", "amount": 3.0})
    s2 = signature_for_unmatched("a", {"order_id": "#2002"})
    assert s1 == s2


def test_first_key_value_prefers_key_col():
    assert _first_key_value({"amount": 9, "ref": "R-1"}, key_col="ref") == "R-1"
    # Missing/None key_col value falls back to the legacy guess
    assert _first_key_value({"order_id": "#5", "ref": None}, key_col="ref") == "#5"
