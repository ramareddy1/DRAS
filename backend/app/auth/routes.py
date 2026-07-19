"""Auth endpoints: request-code -> verify (cookie session) -> me / logout."""
from __future__ import annotations

import os
import re

from fastapi import APIRouter, Cookie, HTTPException, Request, Response

from . import emailer, members, store

router = APIRouter(prefix="/api/auth")

SESSION_COOKIE = "reconops_session"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def require_user(reconops_session: str = Cookie(default="")) -> dict:
    uid = store.session_user_id(reconops_session)
    user = store.user_by_id(uid) if uid else None
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in required.")
    return user


@router.post("/request-code")
def request_code(payload: dict):
    email = (payload or {}).get("email", "")
    if not _EMAIL_RE.match((email or "").strip()):
        raise HTTPException(status_code=400, detail="Enter a valid email address.")
    try:
        code = store.issue_code(email)
    except store.RateLimited:
        raise HTTPException(status_code=429, detail="Too many codes requested; try again in an hour.")
    if os.getenv("RECONOPS_AUTH_DEV") == "1":
        return {"ok": True, "dev_code": code}
    if not emailer.smtp_configured():
        raise HTTPException(status_code=503, detail="Email sign-in is not configured on this server.")
    emailer.send_code(email.strip(), code)
    return {"ok": True}


@router.post("/verify")
def verify(payload: dict, request: Request, response: Response):
    email = (payload or {}).get("email", "")
    code = (payload or {}).get("code", "")
    if not store.verify_code(email, code):
        raise HTTPException(status_code=401, detail="Invalid or expired code.")
    user = store.get_or_create_user(email)
    token = store.create_session(user["id"])
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax",
        secure=(request.url.scheme == "https"),
        max_age=store.SESSION_TTL_S, path="/",
    )
    return {"ok": True, "user": {"id": user["id"], "email": user["email"]}}


@router.get("/me")
def me(reconops_session: str = Cookie(default="")):
    user = require_user(reconops_session)
    return {"user": {"id": user["id"], "email": user["email"]},
            "accounts": members.accounts_for_user(user["id"])}


@router.post("/logout")
def logout(response: Response, reconops_session: str = Cookie(default="")):
    if reconops_session:
        store.delete_session(reconops_session)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}
