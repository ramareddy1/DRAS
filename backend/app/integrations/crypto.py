"""Fernet encryption for connection credentials at rest.

Mirrors app/auth/emailer.py's smtp_configured() guard: if the key isn't
set, callers get a clear error instead of a silent no-op or a crash deep
in cryptography internals.
"""
from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken


def encryption_configured() -> bool:
    return bool(os.getenv("RECONOPS_ENCRYPTION_KEY", "").strip())


def _fernet() -> Fernet:
    key = os.getenv("RECONOPS_ENCRYPTION_KEY", "").strip()
    if not key:
        raise RuntimeError("RECONOPS_ENCRYPTION_KEY is not set")
    return Fernet(key.encode())


def encrypt_token(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise ValueError("Could not decrypt stored token") from e
