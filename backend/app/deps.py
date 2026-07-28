"""Shared FastAPI dependencies for account/workspace resolution.

Split out of main.py so other route modules (app/integrations/routes.py)
can depend on them without importing main.py itself, which would create a
circular import — main.py already imports and mounts those routers.
"""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException

from .auth.routes import require_user
from .auth import members as members_store
from .memory import accounts as accounts_memory
from .models import Account


def require_account(
    x_account_id: str = Header(default=""),
    user: dict = Depends(require_user),
) -> Account:
    """Membership-checked account resolution. The UUID header is now just a
    workspace selector — the session cookie is the credential."""
    if not x_account_id:
        raise HTTPException(status_code=400, detail="X-Account-Id header required.")
    if members_store.role_of(x_account_id, user["id"]) is None:
        raise HTTPException(status_code=403, detail="No access to this workspace.")
    acc = accounts_memory.load_account(x_account_id)
    if acc is None:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    return acc


def require_owner(
    account: Account = Depends(require_account),
    user: dict = Depends(require_user),
) -> Account:
    if members_store.role_of(account.id, user["id"]) != "owner":
        raise HTTPException(status_code=403, detail="Owner role required.")
    return account
