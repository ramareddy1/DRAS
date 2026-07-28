import pytest
from cryptography.fernet import Fernet

from app.integrations import crypto


@pytest.mark.no_db
def test_encrypt_decrypt_round_trip(monkeypatch):
    monkeypatch.setenv("RECONOPS_ENCRYPTION_KEY", Fernet.generate_key().decode())
    token = "shpat_abc123"
    encrypted = crypto.encrypt_token(token)
    assert encrypted != token
    assert crypto.decrypt_token(encrypted) == token


@pytest.mark.no_db
def test_encryption_configured_reflects_env(monkeypatch):
    monkeypatch.delenv("RECONOPS_ENCRYPTION_KEY", raising=False)
    assert crypto.encryption_configured() is False
    monkeypatch.setenv("RECONOPS_ENCRYPTION_KEY", Fernet.generate_key().decode())
    assert crypto.encryption_configured() is True


@pytest.mark.no_db
def test_encrypt_without_key_raises(monkeypatch):
    monkeypatch.delenv("RECONOPS_ENCRYPTION_KEY", raising=False)
    with pytest.raises(RuntimeError):
        crypto.encrypt_token("x")


@pytest.mark.no_db
def test_decrypt_garbage_raises_value_error(monkeypatch):
    monkeypatch.setenv("RECONOPS_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with pytest.raises(ValueError):
        crypto.decrypt_token("not-a-real-token")
