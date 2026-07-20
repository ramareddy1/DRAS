import pytest

from app import models
from app.memory import accounts


def test_create_and_load_account_round_trips():
    acc = accounts.create_account(display_name="Acme Co")
    loaded = accounts.load_account(acc.id)
    assert loaded is not None
    assert loaded.id == acc.id
    assert loaded.display_name == "Acme Co"
    assert isinstance(loaded.profile, models.AccountProfile)


def test_load_account_missing_returns_none():
    assert accounts.load_account("00000000-0000-4000-8000-000000000000") is None


def test_load_account_rejects_malformed_id():
    assert accounts.load_account("not-a-uuid") is None


def test_update_profile_merges_partial_and_ignores_none():
    acc = accounts.create_account()
    updated = accounts.update_profile(acc.id, {"time_zone": "America/New_York", "materiality_abs": None})
    assert updated.profile.time_zone == "America/New_York"
    assert updated.profile.materiality_abs == 100.0  # unchanged (None ignored)


def test_update_profile_missing_account_raises():
    with pytest.raises(ValueError):
        accounts.update_profile("00000000-0000-4000-8000-000000000000", {"time_zone": "UTC"})


def test_account_exists():
    acc = accounts.create_account()
    assert accounts.account_exists(acc.id) is True
    assert accounts.account_exists("00000000-0000-4000-8000-000000000000") is False
