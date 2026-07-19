# Authentication & Organizations (Phase 2.1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** No endpoint is reachable without a session; export links expire; decision-log rows carry user identity. A client signs in with their email, teammates join the same workspace with roles, and the localStorage-UUID-as-password era ends.

**Architecture:** Self-rolled **email OTP (6-digit code) + server-side sessions** in an httpOnly cookie — no auth SaaS, so the stack stays self-contained on JSON-on-disk (Postgres is Phase 2.2) and fully testable offline via a dev mode that returns the code in the response. **Org = Account, 1:1**: membership records (`members.json` per account + a global reverse index) wrap the existing tenant, so no memory-store code changes. All secrets (codes, session tokens) are stored **sha256-hashed**. Export downloads switch from `?account_id=` to **HMAC-signed 5-minute tokens**. The single rewritten `require_account` dependency (20 call sites) enforces membership everywhere at once.

**Tech Stack:** FastAPI cookie deps, stdlib `secrets`/`hmac`/`hashlib`, `smtplib` for mail, existing fsutil atomic-write/lock patterns, React for the login gate.

**Non-goals:** SSO/OAuth, password auth, per-row permissions, audit-log UI, email templates beyond plain text, multi-workspace switching UI polish (the selector is minimal), CSRF tokens (SameSite=Lax cookies + JSON-only endpoints cover the pilot threat model — revisit at Postgres time).

**Conventions:** backend commands from `backend/` with the venv (`./.venv/Scripts/python.exe`); commit per task; every task keeps `python -m pytest -q` and `python -m app.eval` green. **Note:** after Task 3 the old frontend flow is broken until Task 7 lands — do Tasks 3–7 in one working session.

---

## File structure

- Create: `backend/app/auth/__init__.py` (empty)
- Create: `backend/app/auth/store.py` — users, login codes, sessions (hashed at rest)
- Create: `backend/app/auth/members.py` — per-account membership + global reverse index
- Create: `backend/app/auth/tokens.py` — HMAC export tokens + persisted server secret
- Create: `backend/app/auth/emailer.py` — SMTP sender, env-gated
- Create: `backend/app/auth/routes.py` — request-code / verify / me / logout, wired into main
- Create: `frontend/src/auth.jsx` — AuthGate + LoginPage
- Create: `backend/tests/test_auth_store.py`, `test_auth_endpoints.py`, `test_membership.py`, `test_roles.py`, `test_export_tokens.py`
- Modify: `backend/app/memory/fsutil.py` (+`named_lock`), `backend/app/main.py` (deps, lockdown, attribution, export), `backend/app/models.py` (DecisionLogEntry.user_id/user_email, Rule.created_by), `frontend/src/account.js`, `frontend/src/api/client.js`, `frontend/src/main.jsx`, `frontend/src/components/Layout.jsx`, `frontend/src/pages/ResultsPage.jsx`, `backend/.env.example`, `deploy/env.example`, `docker-compose.prod.yml`, `docs/DEPLOY.md`

Auth data layout (all under `data/`): `auth/users.json` (email → user), `auth/codes.json` (email → hashed pending code + rate counters), `auth/sessions.json` (token-hash → session), `auth/memberships.json` (user_id → [account_ids]), `auth/secret` (HMAC key), `accounts/{id}/members.json` (user_id → role record). Auth modules call `data_dir()` **at call time** (not module import) so tests need no reload dance.

---

### Task 1: `named_lock` + auth store (users, codes, sessions)

**Files:**
- Modify: `backend/app/memory/fsutil.py`
- Create: `backend/app/auth/__init__.py`, `backend/app/auth/store.py`
- Test: `backend/tests/test_auth_store.py`

- [x] **Step 1: Write the failing tests**

`backend/tests/test_auth_store.py`:
```python
import json

import pytest


@pytest.fixture()
def auth_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    from app.auth import store
    return store, tmp_path


def test_user_created_once_per_email(auth_env):
    store, _ = auth_env
    u1 = store.get_or_create_user("Alice@Example.COM ")
    u2 = store.get_or_create_user("alice@example.com")
    assert u1["id"] == u2["id"]
    assert u1["email"] == "alice@example.com"


def test_code_roundtrip_and_secrets_hashed(auth_env):
    store, tmp = auth_env
    code = store.issue_code("a@b.co")
    assert len(code) == 6 and code.isdigit()
    raw = (tmp / "auth" / "codes.json").read_text(encoding="utf-8")
    assert code not in raw                      # hashed at rest
    assert store.verify_code("a@b.co", "000000") is False
    assert store.verify_code("a@b.co", code) is True
    assert store.verify_code("a@b.co", code) is False   # single-use


def test_code_rate_limit(auth_env):
    store, _ = auth_env
    for _ in range(store.MAX_CODES_PER_HOUR):
        store.issue_code("spam@x.co")
    with pytest.raises(store.RateLimited):
        store.issue_code("spam@x.co")


def test_verify_attempts_capped(auth_env):
    store, _ = auth_env
    code = store.issue_code("brute@x.co")
    for _ in range(store.MAX_VERIFY_ATTEMPTS):
        assert store.verify_code("brute@x.co", "999999") is False
    assert store.verify_code("brute@x.co", code) is False  # burned by attempts


def test_session_lifecycle(auth_env):
    store, tmp = auth_env
    u = store.get_or_create_user("s@x.co")
    token = store.create_session(u["id"])
    raw = (tmp / "auth" / "sessions.json").read_text(encoding="utf-8")
    assert token not in raw                     # hashed at rest
    assert store.session_user_id(token) == u["id"]
    store.delete_session(token)
    assert store.session_user_id(token) is None
    assert store.session_user_id("garbage") is None
```

- [x] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_auth_store.py -q` → collection error (`app.auth` doesn't exist).

- [x] **Step 3: Add `named_lock` to fsutil**

Append to `backend/app/memory/fsutil.py`:
```python
def named_lock(name: str, timeout: float = 10.0) -> FileLock:
    """Advisory lock for non-account-scoped stores (auth, global indexes)."""
    from ..config import data_dir
    lock_dir = data_dir() / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    return FileLock(str(lock_dir / f"{name}.lock"), timeout=timeout)
```

- [x] **Step 4: Implement the store**

`backend/app/auth/store.py`:
```python
"""Users, login codes, sessions — JSON-on-disk, secrets sha256-hashed at rest.

A leaked data directory must not let anyone mint a session or replay a
login code. Codes are single-use, expire in 10 minutes, rate-limited per
email; sessions are opaque 256-bit tokens with a 30-day TTL.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import data_dir
from ..memory.fsutil import atomic_write_json, named_lock

CODE_TTL_S = 10 * 60
SESSION_TTL_S = 30 * 24 * 3600
MAX_CODES_PER_HOUR = 5
MAX_VERIFY_ATTEMPTS = 5


class RateLimited(Exception):
    pass


def _auth_dir() -> Path:
    return data_dir() / "auth"


def _load(p: Path) -> Dict[str, Any]:
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _norm_email(e: str) -> str:
    return (e or "").strip().lower()


# --- users -----------------------------------------------------------------

def get_or_create_user(email: str) -> Dict[str, Any]:
    email = _norm_email(email)
    with named_lock("auth"):
        p = _auth_dir() / "users.json"
        users = _load(p)
        u = users.get(email)
        if u is None:
            u = {"id": str(uuid.uuid4()), "email": email, "created_at": time.time()}
            users[email] = u
            atomic_write_json(p, users, indent=2)
        return u


def user_by_id(user_id: str) -> Optional[Dict[str, Any]]:
    for u in _load(_auth_dir() / "users.json").values():
        if u.get("id") == user_id:
            return u
    return None


# --- login codes -----------------------------------------------------------

def issue_code(email: str) -> str:
    email = _norm_email(email)
    code = f"{secrets.randbelow(1_000_000):06d}"
    now = time.time()
    with named_lock("auth"):
        p = _auth_dir() / "codes.json"
        codes = _load(p)
        entry = codes.get(email) or {}
        issued = [t for t in entry.get("issued", []) if now - t < 3600]
        if len(issued) >= MAX_CODES_PER_HOUR:
            raise RateLimited(f"Too many codes for {email}; try again later.")
        issued.append(now)
        codes[email] = {"hash": _sha(code), "exp": now + CODE_TTL_S,
                        "attempts": 0, "issued": issued}
        atomic_write_json(p, codes, indent=2)
    return code


def verify_code(email: str, code: str) -> bool:
    email = _norm_email(email)
    now = time.time()
    with named_lock("auth"):
        p = _auth_dir() / "codes.json"
        codes = _load(p)
        entry = codes.get(email)
        if not entry or not entry.get("hash") or now > entry.get("exp", 0):
            return False
        if entry.get("attempts", 0) >= MAX_VERIFY_ATTEMPTS:
            return False
        if _sha(code or "") != entry["hash"]:
            entry["attempts"] = entry.get("attempts", 0) + 1
            codes[email] = entry
            atomic_write_json(p, codes, indent=2)
            return False
        entry.pop("hash", None)   # single-use; keep rate counters
        entry.pop("exp", None)
        codes[email] = entry
        atomic_write_json(p, codes, indent=2)
        return True


# --- sessions --------------------------------------------------------------

def create_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with named_lock("auth"):
        p = _auth_dir() / "sessions.json"
        sessions = {h: s for h, s in _load(p).items() if s.get("exp", 0) > now}
        sessions[_sha(token)] = {"user_id": user_id, "exp": now + SESSION_TTL_S,
                                 "created_at": now}
        atomic_write_json(p, sessions, indent=2)
    return token


def session_user_id(token: str) -> Optional[str]:
    if not token:
        return None
    s = _load(_auth_dir() / "sessions.json").get(_sha(token))
    if not s or time.time() > s.get("exp", 0):
        return None
    return s.get("user_id")


def delete_session(token: str) -> None:
    with named_lock("auth"):
        p = _auth_dir() / "sessions.json"
        sessions = _load(p)
        sessions.pop(_sha(token), None)
        atomic_write_json(p, sessions, indent=2)
```
Also create empty `backend/app/auth/__init__.py`.

- [x] **Step 5: Verify + commit**

Run: `python -m pytest tests/test_auth_store.py -q` → all pass; `python -m pytest -q` → all pass.
```bash
git add backend/app/auth/ backend/app/memory/fsutil.py backend/tests/test_auth_store.py
git commit -m "feat: auth store — users, hashed login codes, hashed sessions"
```

---

### Task 2: Auth endpoints + cookie sessions

**Files:**
- Create: `backend/app/auth/routes.py`, `backend/app/auth/emailer.py`
- Modify: `backend/app/main.py` (include router)
- Test: `backend/tests/test_auth_endpoints.py`

- [x] **Step 1: Write the failing tests**

`backend/tests/test_auth_endpoints.py`:
```python
import importlib

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RECONOPS_AUTH_DEV", "1")
    from fastapi.testclient import TestClient
    from app import main
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c


def _login(client, email="me@x.co"):
    code = client.post("/api/auth/request-code", json={"email": email}).json()["dev_code"]
    r = client.post("/api/auth/verify", json={"email": email, "code": code})
    assert r.status_code == 200
    return r


def test_request_code_dev_mode_returns_code(client):
    r = client.post("/api/auth/request-code", json={"email": "a@b.co"})
    assert r.status_code == 200
    assert len(r.json()["dev_code"]) == 6


def test_request_code_rejects_bad_email(client):
    assert client.post("/api/auth/request-code", json={"email": "nope"}).status_code == 400


def test_verify_sets_cookie_and_me_works(client):
    _login(client)
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["email"] == "me@x.co"


def test_wrong_code_401(client):
    client.post("/api/auth/request-code", json={"email": "w@x.co"})
    r = client.post("/api/auth/verify", json={"email": "w@x.co", "code": "000000"})
    assert r.status_code == 401


def test_logout_kills_session(client):
    _login(client)
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401


def test_rate_limit_429(client):
    for _ in range(5):
        client.post("/api/auth/request-code", json={"email": "r@x.co"})
    assert client.post("/api/auth/request-code", json={"email": "r@x.co"}).status_code == 429
```

- [x] **Step 2: Run to verify it fails** → 404s (routes don't exist).

- [x] **Step 3: Implement emailer + routes**

`backend/app/auth/emailer.py`:
```python
"""SMTP sender for login codes. Env-gated; absent config -> 503 at the route."""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage


def smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST", "").strip())


def send_code(to_email: str, code: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = f"Your ReconOps sign-in code: {code}"
    msg["From"] = os.getenv("SMTP_FROM", os.getenv("SMTP_USER", "reconops@localhost"))
    msg["To"] = to_email
    msg.set_content(
        f"Your ReconOps sign-in code is: {code}\n\n"
        "It expires in 10 minutes. If you didn't request it, ignore this email."
    )
    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=15) as s:
        s.starttls()
        user, pw = os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASS", "")
        if user:
            s.login(user, pw)
        s.send_message(msg)
```

`backend/app/auth/routes.py`:
```python
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
```
(`members.accounts_for_user` lands in Task 3 — for this task create `backend/app/auth/members.py` with just:
```python
from typing import List


def accounts_for_user(user_id: str) -> List[dict]:
    return []
```
Task 3 replaces it.)

In `main.py`, after the app is created: `from .auth.routes import router as auth_router` and `app.include_router(auth_router)`.

- [x] **Step 4: Verify + commit**

Run: `python -m pytest -q` → all pass (TestClient carries cookies across requests).
```bash
git add backend/app/auth/ backend/app/main.py backend/tests/test_auth_endpoints.py
git commit -m "feat: email-OTP auth endpoints with httpOnly cookie sessions"
```

---

### Task 3: Membership + global lockdown + legacy claim

**Files:**
- Rewrite: `backend/app/auth/members.py`
- Modify: `backend/app/main.py` (`require_account` rewrite, account create/claim, lock down `/api/preview`, `/api/bind`, `/api/concepts`)
- Test: `backend/tests/test_membership.py`

- [x] **Step 1: Write the failing tests**

`backend/tests/test_membership.py` (reuses the fixture/_login pattern from `test_auth_endpoints.py` — copy both helpers in):
```python
def test_everything_401s_without_session(client):
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/rules", headers={"X-Account-Id": "x"}).status_code == 401
    assert client.post("/api/accounts", json={}).status_code == 401
    assert client.get("/api/concepts").status_code == 401
    assert client.get("/api/health").status_code == 200   # only health + auth stay open


def test_create_account_grants_ownership(client):
    _login(client)
    acc = client.post("/api/accounts", json={}).json()
    me = client.get("/api/auth/me").json()
    assert me["accounts"][0]["account_id"] == acc["id"]
    assert me["accounts"][0]["role"] == "owner"
    r = client.get("/api/rules", headers={"X-Account-Id": acc["id"]})
    assert r.status_code == 200


def test_membership_enforced_cross_account(client):
    _login(client, "a@x.co")
    acc_a = client.post("/api/accounts", json={}).json()
    client.post("/api/auth/logout")
    _login(client, "b@x.co")
    r = client.get("/api/rules", headers={"X-Account-Id": acc_a["id"]})
    assert r.status_code == 403


def test_legacy_account_claim_once(client, tmp_path, monkeypatch):
    # A pre-auth account (no members) can be claimed by presenting its UUID
    from app.memory import accounts as accounts_memory, rules_store
    import importlib
    importlib.reload(accounts_memory); importlib.reload(rules_store)
    legacy = accounts_memory.create_account()
    rules_store.seed_defaults(legacy.id)

    _login(client, "owner@x.co")
    r = client.post("/api/accounts/claim", json={"account_id": legacy.id})
    assert r.status_code == 200
    assert client.get("/api/rules", headers={"X-Account-Id": legacy.id}).status_code == 200
    # Second claim by someone else -> 409
    client.post("/api/auth/logout")
    _login(client, "intruder@x.co")
    assert client.post("/api/accounts/claim",
                       json={"account_id": legacy.id}).status_code == 409
```

- [x] **Step 2: Run to verify it fails** → old `require_account` accepts the bare header; endpoints return 200/401 in the wrong places.

- [x] **Step 3: Implement members.py**

```python
"""Account membership: per-account members.json + global reverse index.

Org == Account (1:1). Roles: "owner" (settings, rules accept/revoke,
member management) and "analyst" (everything else).
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from ..config import data_dir
from ..memory.fsutil import atomic_write_json, named_lock


def _members_path(account_id: str):
    return data_dir() / "accounts" / account_id / "members.json"


def _index_path():
    return data_dir() / "auth" / "memberships.json"


def _load(p) -> Dict[str, Any]:
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def add_member(account_id: str, user_id: str, email: str, role: str) -> None:
    with named_lock("auth"):
        mp = _members_path(account_id)
        members = _load(mp)
        members[user_id] = {"role": role, "email": email, "added_at": time.time()}
        atomic_write_json(mp, members, indent=2)
        ip = _index_path()
        index = _load(ip)
        ids = set(index.get(user_id, []))
        ids.add(account_id)
        index[user_id] = sorted(ids)
        atomic_write_json(ip, index, indent=2)


def role_of(account_id: str, user_id: str) -> Optional[str]:
    return (_load(_members_path(account_id)).get(user_id) or {}).get("role")


def has_members(account_id: str) -> bool:
    return bool(_load(_members_path(account_id)))


def members_of(account_id: str) -> Dict[str, Any]:
    return _load(_members_path(account_id))


def accounts_for_user(user_id: str) -> List[dict]:
    out = []
    for aid in _load(_index_path()).get(user_id, []):
        role = role_of(aid, user_id)
        if role:
            out.append({"account_id": aid, "role": role})
    return out
```

- [x] **Step 4: Rewrite the dependencies + endpoints in main.py**

Replace `require_account` (and add `require_owner`):
```python
from .auth import members as members_store
from .auth.routes import require_user


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
```

- `create_account` endpoint: add `user: dict = Depends(require_user)`; after `rules_store.seed_defaults`, call `members_store.add_member(acc.id, user["id"], user["email"], "owner")`.
- New claim endpoint:
```python
@app.post("/api/accounts/claim")
def claim_account(payload: dict, user: dict = Depends(require_user)):
    """One-time migration path: a pre-auth account (no members) is claimed by
    whoever presents its UUID — the old bearer secret — while signed in."""
    account_id = (payload or {}).get("account_id", "")
    if accounts_memory.load_account(account_id) is None:
        raise HTTPException(status_code=404, detail="Account not found.")
    if members_store.has_members(account_id):
        raise HTTPException(status_code=409, detail="Workspace already claimed.")
    members_store.add_member(account_id, user["id"], user["email"], "owner")
    return {"ok": True, "account_id": account_id, "role": "owner"}
```
- Lock down the strays: add `user: dict = Depends(require_user)` to `preview_file`, `bind_file`, and `concepts`. In `preview_file`/`bind_file` the opportunistic learned-alias lookup keeps using the header as before (membership isn't required just to preview, but a session is).
- The export GET keeps its query-param auth **until Task 6 replaces it entirely** — leave it as is here.

- [x] **Step 5: Verify + commit**

Run: `python -m pytest -q && python -m app.eval` → all pass (eval calls `run_job` directly — untouched by HTTP auth).
```bash
git add backend/app/auth/members.py backend/app/main.py backend/tests/test_membership.py
git commit -m "feat: membership-checked workspaces, global endpoint lockdown, legacy claim"
```

---

### Task 4: Roles — owner-only surfaces + member management

**Files:**
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_roles.py`

- [x] **Step 1: Write the failing tests**

`backend/tests/test_roles.py` (same fixture/_login helpers):
```python
def _owner_with_analyst(client):
    _login(client, "owner@x.co")
    acc = client.post("/api/accounts", json={}).json()
    client.post("/api/accounts/me/members", json={"email": "analyst@x.co"},
                headers={"X-Account-Id": acc["id"]})
    client.post("/api/auth/logout")
    _login(client, "analyst@x.co")
    return acc


def test_analyst_can_work_but_not_administer(client):
    acc = _owner_with_analyst(client)
    h = {"X-Account-Id": acc["id"]}
    assert client.get("/api/rules", headers=h).status_code == 200
    assert client.get("/api/inbox", headers=h).status_code == 200
    # Owner-only surfaces:
    assert client.patch("/api/accounts/me/profile", json={"materiality_abs": 5},
                        headers=h).status_code == 403
    assert client.post("/api/rules/nonexistent/accept", headers=h).status_code == 403
    assert client.post("/api/rules/nonexistent/revoke", headers=h).status_code == 403
    assert client.post("/api/accounts/me/members", json={"email": "c@x.co"},
                       headers=h).status_code == 403


def test_owner_sees_member_list(client):
    acc = _owner_with_analyst(client)
    client.post("/api/auth/logout")
    _login(client, "owner@x.co")
    r = client.get("/api/accounts/me/members", headers={"X-Account-Id": acc["id"]})
    emails = sorted(m["email"] for m in r.json()["members"].values())
    assert emails == ["analyst@x.co", "owner@x.co"]
```

- [x] **Step 2: Run to verify it fails** → member endpoints 404; analyst gets 200 on owner surfaces.

- [x] **Step 3: Implement**

- Switch these endpoints from `Depends(require_account)` to `Depends(require_owner)`: `patch_profile`, `accept_rule`, `revoke_rule_endpoint`.
- Member management:
```python
@app.get("/api/accounts/me/members")
def list_members(account: Account = Depends(require_account)):
    return {"members": members_store.members_of(account.id)}


@app.post("/api/accounts/me/members")
def add_member_endpoint(payload: dict, account: Account = Depends(require_owner)):
    """Owner adds a teammate by email as analyst. The teammate signs in with
    the same email and lands in the workspace — no invite email in the pilot."""
    email = ((payload or {}).get("email") or "").strip()
    from .auth.routes import _EMAIL_RE
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Enter a valid email address.")
    from .auth import store as auth_store
    target = auth_store.get_or_create_user(email)
    members_store.add_member(account.id, target["id"], target["email"], "analyst")
    return {"ok": True, "user_id": target["id"], "role": "analyst"}
```

- [x] **Step 4: Verify + commit**

Run: `python -m pytest -q` → all pass.
```bash
git add backend/app/main.py backend/tests/test_roles.py
git commit -m "feat: owner/analyst roles — settings and rule lifecycle are owner-only"
```

---

### Task 5: Decision-log user attribution + Rule.created_by

**Files:**
- Modify: `backend/app/models.py` (DecisionLogEntry + Rule), `backend/app/main.py`
- Test: append to `backend/tests/test_roles.py`

- [x] **Step 1: Write the failing test**

```python
def test_decisions_carry_user_identity(client, tmp_path):
    _login(client, "who@x.co")
    acc = client.post("/api/accounts", json={}).json()
    client.post("/api/decisions", json={"signature": "sig123", "user_status": "expected"},
                headers={"X-Account-Id": acc["id"]})
    from app.memory import decision_log
    entries = list(decision_log.replay(acc["id"]))
    assert entries[-1].user_email == "who@x.co"
    assert entries[-1].user_id
```

- [x] **Step 2: Run to verify it fails** → `DecisionLogEntry` has no `user_email`.

- [x] **Step 3: Implement**

`models.py`: `DecisionLogEntry` gains `user_id: Optional[str] = None` and `user_email: Optional[str] = None`; `Rule` gains `created_by: Optional[str] = None` (email).

`main.py`: every `decision_log.append(...)` call site (`resolve_triage`, `record_decision`, `observation_feedback`) gains `user: dict = Depends(require_user)` on its endpoint (where not already present via require_account — note `require_account` doesn't expose the user, so add the explicit dependency) and passes `user_id=user["id"], user_email=user["email"]` into the entry. Rule creations in `resolve_triage` set `created_by=user["email"]`; `rule_proposer` rules keep `created_by=None` (system-proposed).

- [x] **Step 4: Verify + commit**

Run: `python -m pytest -q && python -m app.eval` → pass (fields default to None everywhere else).
```bash
git add backend/app/models.py backend/app/main.py backend/tests/test_roles.py
git commit -m "feat: decision log and user-taught rules carry user identity"
```

---

### Task 6: Signed export tokens (kill `?account_id=`)

**Files:**
- Create: `backend/app/auth/tokens.py`
- Modify: `backend/app/main.py` (export endpoints)
- Test: `backend/tests/test_export_tokens.py`

- [x] **Step 1: Write the failing tests**

`backend/tests/test_export_tokens.py`:
```python
import time


def test_token_roundtrip_and_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    from app.auth import tokens
    t = tokens.make_export_token("job-1", "acc-1", ttl=300)
    assert tokens.check_export_token(t, "job-1") == "acc-1"
    assert tokens.check_export_token(t, "job-2") is None       # wrong job
    assert tokens.check_export_token(t + "x", "job-1") is None  # tampered


def test_token_expires(tmp_path, monkeypatch):
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    from app.auth import tokens
    t = tokens.make_export_token("job-1", "acc-1", ttl=-1)
    assert tokens.check_export_token(t, "job-1") is None
```

- [x] **Step 2: Run to verify it fails** → module doesn't exist.

- [x] **Step 3: Implement tokens.py**

```python
"""HMAC-signed, short-lived export tokens.

<a href> downloads can't carry headers or need long-lived credentials in
the URL. Instead the frontend asks for a 5-minute token scoped to one job,
and the export endpoint accepts only that.
"""
from __future__ import annotations

import base64
import hmac
import hashlib
import os
import secrets
import time
from typing import Optional

from ..config import data_dir
from ..memory.fsutil import named_lock


def _secret() -> bytes:
    env = os.getenv("RECONOPS_SECRET", "").strip()
    if env:
        return env.encode("utf-8")
    p = data_dir() / "auth" / "secret"
    if not p.exists():
        with named_lock("auth"):
            if not p.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(secrets.token_hex(32), encoding="utf-8")
    return p.read_text(encoding="utf-8").strip().encode("utf-8")


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def make_export_token(job_id: str, account_id: str, ttl: int = 300) -> str:
    payload = f"{job_id}|{account_id}|{int(time.time()) + ttl}"
    b64 = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{b64}.{_sign(payload)}"


def check_export_token(token: str, job_id: str) -> Optional[str]:
    try:
        b64, sig = token.rsplit(".", 1)
        payload = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-8")
        t_job, t_account, t_exp = payload.split("|")
    except Exception:
        return None
    if not hmac.compare_digest(sig, _sign(payload)):
        return None
    if t_job != job_id or time.time() > int(t_exp):
        return None
    return t_account
```

- [x] **Step 4: Rewire the export endpoints in main.py**

```python
@app.post("/api/results/{job_id}/export-token")
def export_token(job_id: str, account: Account = Depends(require_account)):
    _load_job_for_account(job_id, account)   # 404 unless it's theirs
    from .auth.tokens import make_export_token
    return {"token": make_export_token(job_id, account.id)}
```
Rewrite `export`: signature becomes `def export(job_id: str, token: str = "")`; delete the `x_account_id`/`account_id` params and the account lookup; instead:
```python
    from .auth.tokens import check_export_token
    account_id = check_export_token(token, job_id)
    if account_id is None:
        raise HTTPException(status_code=401, detail="Export link invalid or expired.")
    acc = accounts_memory.load_account(account_id)
    if acc is None:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    job = _load_job_for_account(job_id, acc)
```
(rest unchanged). Add an endpoint test to `test_export_tokens.py` using the client fixture: login → create account → export-token for a missing job → 404; and `GET /api/results/x/export?token=garbage` → 401.

- [x] **Step 5: Verify + commit**

Run: `python -m pytest -q` → all pass; `grep -n "account_id: str = \"\"" backend/app/main.py` → gone.
```bash
git add backend/app/auth/tokens.py backend/app/main.py backend/tests/test_export_tokens.py
git commit -m "feat: HMAC-signed 5-minute export tokens replace account_id query param"
```

---

### Task 7: Frontend — login gate, workspace selection, claim, export flow

**Files:**
- Create: `frontend/src/auth.jsx`
- Rewrite: `frontend/src/account.js`
- Modify: `frontend/src/api/client.js`, `frontend/src/main.jsx`, `frontend/src/components/Layout.jsx`, `frontend/src/pages/ResultsPage.jsx`

- [x] **Step 1: client.js — auth calls + 401 broadcast + export token**

Add:
```javascript
export async function requestCode(email) {
  return handle(await fetch(`${BASE}/api/auth/request-code`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email }),
  }));
}
export async function verifyCode(email, code) {
  return handle(await fetch(`${BASE}/api/auth/verify`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, code }),
  }));
}
export async function getMe() {
  return handle(await fetch(`${BASE}/api/auth/me`));
}
export async function logout() {
  return handle(await fetch(`${BASE}/api/auth/logout`, { method: "POST" }));
}
export async function claimAccount(accountId) {
  return handle(await fetch(`${BASE}/api/accounts/claim`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ account_id: accountId }),
  }));
}
export async function getExportToken(jobId) {
  return post(`/api/results/${jobId}/export-token`);
}
```
In `handle`, broadcast auth failures so the gate can react:
```javascript
async function handle(res) {
  if (res.status === 401) {
    window.dispatchEvent(new Event("reconops:unauthenticated"));
  }
  if (!res.ok) { /* existing error path */ }
  return res.json();
}
```
Delete `exportUrl` (replaced by the token flow in ResultsPage).

- [x] **Step 2: account.js — cookie era**

Rewrite: drop `?account=`/`?reset` adoption and the unauthenticated auto-create. `ensureAccount()` now: `getMe()` → if the stored `reconops_account_id` is among memberships, use it; else if memberships exist, use the first (and store it); else if a stored legacy id exists, try `claimAccount(storedId)` (on 409/404, clear it); else `POST /api/accounts` and store the new id. `accountFetch` keeps adding `X-Account-Id`. Keep `currentAccountId()`; `resetAccount()` becomes `logout()+reload`.

- [x] **Step 3: auth.jsx — AuthGate + LoginPage**

```jsx
import { useEffect, useState } from "react";
import { getMe, requestCode, verifyCode } from "./api/client.js";

export function AuthGate({ children }) {
  const [state, setState] = useState("loading"); // loading | in | out
  useEffect(() => {
    getMe().then(() => setState("in")).catch(() => setState("out"));
    const onOut = () => setState("out");
    window.addEventListener("reconops:unauthenticated", onOut);
    return () => window.removeEventListener("reconops:unauthenticated", onOut);
  }, []);
  if (state === "loading") return <div className="text-center py-16 text-slate-400">Loading…</div>;
  if (state === "out") return <LoginPage onSignedIn={() => setState("in")} />;
  return children;
}

function LoginPage({ onSignedIn }) {
  const [email, setEmail] = useState("");
  const [phase, setPhase] = useState("email"); // email | code
  const [code, setCode] = useState("");
  const [devCode, setDevCode] = useState("");
  const [err, setErr] = useState("");
  const submitEmail = async (e) => {
    e.preventDefault(); setErr("");
    try {
      const r = await requestCode(email);
      setDevCode(r.dev_code || "");
      setPhase("code");
    } catch (ex) { setErr(ex.message); }
  };
  const submitCode = async (e) => {
    e.preventDefault(); setErr("");
    try { await verifyCode(email, code); onSignedIn(); }
    catch (ex) { setErr(ex.message); }
  };
  return (
    <div className="max-w-sm mx-auto mt-24 bg-white border border-slate-200 rounded-lg p-6">
      <h1 className="text-xl font-semibold text-navy mb-1">Sign in to ReconOps</h1>
      <p className="text-sm text-slate-500 mb-4">We'll email you a 6-digit code.</p>
      {phase === "email" ? (
        <form onSubmit={submitEmail} className="space-y-3">
          <input autoFocus type="email" required value={email} placeholder="you@company.com"
                 onChange={(e) => setEmail(e.target.value)}
                 className="w-full border border-slate-300 rounded px-3 py-2 text-sm" />
          <button className="w-full bg-navy text-white rounded px-3 py-2 text-sm font-medium hover:bg-brand">
            Email me a code</button>
        </form>
      ) : (
        <form onSubmit={submitCode} className="space-y-3">
          <p className="text-xs text-slate-500">Code sent to {email}.</p>
          {devCode && <p className="text-xs text-amber-700">Dev mode — your code: <b>{devCode}</b></p>}
          <input autoFocus inputMode="numeric" pattern="[0-9]*" maxLength={6} required value={code}
                 onChange={(e) => setCode(e.target.value)} placeholder="123456"
                 className="w-full border border-slate-300 rounded px-3 py-2 text-sm tracking-widest" />
          <button className="w-full bg-navy text-white rounded px-3 py-2 text-sm font-medium hover:bg-brand">
            Sign in</button>
          <button type="button" onClick={() => setPhase("email")}
                  className="w-full text-xs text-slate-500 hover:underline">Different email</button>
        </form>
      )}
      {err && <p className="mt-3 text-xs text-bad">{err}</p>}
    </div>
  );
}
```
Wrap the app in `main.jsx`: `<AuthGate><App/></AuthGate>` (inside providers, outside routes). Add the signed-in email + a "Sign out" button to `Layout.jsx` header (calls `logout()` then `window.location.reload()`).

- [x] **Step 4: ResultsPage export via token**

Replace the `<a href={exportUrl(...)}>` with a button:
```jsx
onClick={async () => {
  const { token } = await getExportToken(data.job_id);
  window.location.href = `/api/results/${data.job_id}/export?token=${encodeURIComponent(token)}`;
}}
```

- [x] **Step 5: Verify**

`npm run build` → clean. Manual dev-mode walkthrough: backend with `RECONOPS_AUTH_DEV=1` + stub LLM, frontend dev server; sign in with the on-screen dev code, auto-create workspace, upload the bundled samples end-to-end, download the export (token flow), sign out → gate returns. Confirm `?account=<uuid>` no longer adopts.

- [x] **Step 6: Commit**

```bash
git add frontend/src/
git commit -m "feat: login gate, cookie sessions, workspace claim, tokenized exports in the UI"
```

---

### Task 8: SMTP config + env/docs

**Files:**
- Modify: `backend/.env.example`, `deploy/env.example`, `docker-compose.prod.yml`, `docs/DEPLOY.md`

- [x] **Step 1: Env plumbing**

`backend/.env.example` and `deploy/env.example` gain:
```
# --- auth ---
# Dev only: return the sign-in code in the API response instead of emailing.
# NEVER set in production.
RECONOPS_AUTH_DEV=
# SMTP for sign-in codes (any transactional provider: Resend, Postmark, SES...)
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASS=
SMTP_FROM=
# Optional: pin the HMAC secret for export tokens (else auto-generated on disk)
RECONOPS_SECRET=
```
`docker-compose.prod.yml` backend environment gains the passthroughs:
```yaml
      - SMTP_HOST=${SMTP_HOST:-}
      - SMTP_PORT=${SMTP_PORT:-587}
      - SMTP_USER=${SMTP_USER:-}
      - SMTP_PASS=${SMTP_PASS:-}
      - SMTP_FROM=${SMTP_FROM:-}
      - RECONOPS_SECRET=${RECONOPS_SECRET:-}
```

- [x] **Step 2: DEPLOY.md**

Add an "## Auth setup" section after Bring-up: SMTP env vars are required for sign-in emails (any transactional SMTP provider works; the free tiers of Resend/Postmark suffice for a pilot); without them, `/api/auth/request-code` returns 503. Document the one-time legacy migration: existing pilot users sign in, and the app claims the workspace from their browser's stored UUID automatically. Note that the backup now also carries `data/auth/` (sessions/users) — already covered by the volume backup.

- [x] **Step 3: Verify + commit**

Run: `python -m pytest -q && python -m app.eval` → green; `npm run build` → clean.
```bash
git add backend/.env.example deploy/env.example docker-compose.prod.yml docs/DEPLOY.md
git commit -m "docs+env: SMTP auth configuration for production"
```

---

## Definition of done (from master plan §2.1)

- **No endpoint reachable without a session** — Task 3's lockdown test asserts it endpoint-by-endpoint (health + auth routes are the only exceptions). ✓
- **Export links expire** — 5-minute HMAC tokens, tamper/expiry/scope tested. ✓
- **Decision log rows carry user identity** — `user_id`/`user_email` on every append; user-taught rules carry `created_by`. ✓
- Legacy pilot accounts migrate via one-time claim; `?account=<uuid>` adoption is gone.
