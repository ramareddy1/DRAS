# Production Deployment (Phase 2.4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A client can be onboarded on an HTTPS URL you'd put in an email; an exception in production shows up in Sentry tagged with the account and job ID.

**Architecture:** Provider-agnostic Docker Compose stack: `backend` (uvicorn) + `edge` (Caddy serving the built frontend and reverse-proxying `/api`, with automatic HTTPS from Let's Encrypt when a domain is set). JSON-on-disk data lives on a named volume (Postgres is Phase 2.2). Observability is stdlib JSON logging with request IDs plus Sentry via env-gated DSN. Runs identically on the dev machine (Docker 29 verified present) and any Linux VPS.

**Tech Stack:** Docker Compose, Caddy 2, uvicorn, sentry-sdk, GitHub Actions.

**Non-goals (explicitly out of scope):** frontend Sentry (backend coverage satisfies the done-criterion), multi-worker/queue scaling (Phase 2.3), Postgres (Phase 2.2), auth (Phase 2.1), CDN.

**Conventions:** backend commands run from `backend/` with the venv (`./.venv/Scripts/python.exe` on this machine); commit after every task.

---

## File structure

- Create: `backend/app/obs.py` — JSON log formatter, request-ID middleware, Sentry init (one observability module)
- Create: `deploy/Dockerfile.edge` — multi-stage: node build → Caddy image with `dist/` baked in
- Create: `deploy/Caddyfile` — static serving + `/api` reverse proxy + auto-HTTPS
- Create: `deploy/env.example` — documented server-side env vars
- Create: `docker-compose.prod.yml` — production stack (repo root, next to the dev compose file)
- Create: `docs/DEPLOY.md` — VPS runbook (bring-up, update, backup, smoke tests)
- Create: `backend/tests/test_obs.py`
- Modify: `backend/app/main.py` — env-driven CORS, middleware wiring, lifespan retention scheduler
- Modify: `backend/requirements.txt` (+`sentry-sdk[fastapi]`), `backend/requirements-dev.txt` (+`httpx` for TestClient)
- Modify: `.github/workflows/ci.yml` — build both prod images on every push

---

### Task 1: Env-driven CORS + health metadata

**Files:**
- Modify: `backend/app/main.py:44-51` (CORS), `main.py:93-95` (health)
- Test: `backend/tests/test_obs.py` (new file, first test)

- [x] **Step 1: Write the failing test**

`backend/tests/test_obs.py`:
```python
import importlib


def test_cors_origins_come_from_env(monkeypatch):
    monkeypatch.setenv("RECONOPS_CORS_ORIGINS", "https://app.example.com, https://staging.example.com")
    from app import main
    importlib.reload(main)
    cors = next(m for m in main.app.user_middleware
                if m.cls.__name__ == "CORSMiddleware")
    assert cors.kwargs["allow_origins"] == [
        "https://app.example.com", "https://staging.example.com"]


def test_health_reports_version(monkeypatch):
    monkeypatch.delenv("RECONOPS_CORS_ORIGINS", raising=False)
    from app import main
    importlib.reload(main)
    body = main.health()
    assert body["ok"] is True
    assert body["version"] == main.app.version
```

Note: `requirements-dev.txt` gains `httpx==0.27.2` now (used by TestClient in Task 2): add the line and `pip install -r requirements-dev.txt`.

- [x] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_obs.py -v`
Expected: FAIL — origins are hardcoded; health has no `version` key.

- [x] **Step 3: Implement**

In `main.py`, replace the CORS block:
```python
_cors_origins = [
    o.strip() for o in os.getenv(
        "RECONOPS_CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",") if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Account-Id", "X-Request-ID"],
)
```
(Behind the same-origin proxy CORS barely matters; the env var exists for split-origin setups.)

Health:
```python
@app.get("/api/health")
def health():
    return {"ok": True, "llm_configured": is_configured(), "version": app.version}
```

- [x] **Step 4: Verify + commit**

Run: `python -m pytest -q` → all pass.
```bash
git add backend/app/main.py backend/tests/test_obs.py backend/requirements-dev.txt
git commit -m "feat: env-driven CORS origins + version in health"
```

---

### Task 2: Structured JSON logging + request IDs

**Files:**
- Create: `backend/app/obs.py`
- Modify: `backend/app/main.py` (wire middleware + setup)
- Test: `backend/tests/test_obs.py` (append)

- [x] **Step 1: Write the failing test**

Append to `backend/tests/test_obs.py`:
```python
def test_request_id_header_and_access_log(monkeypatch, capsys):
    from fastapi.testclient import TestClient
    from app import main
    importlib.reload(main)
    with TestClient(main.app) as client:
        r = client.get("/api/health", headers={"X-Account-Id": "acc-123"})
    assert r.status_code == 200
    rid = r.headers.get("X-Request-ID")
    assert rid and len(rid) >= 8
    logged = capsys.readouterr().out
    assert '"path": "/api/health"' in logged
    assert f'"request_id": "{rid}"' in logged
    assert '"account_id": "acc-123"' in logged
```

- [x] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_obs.py -v` → FAIL (no X-Request-ID header, no JSON log line).

- [x] **Step 3: Implement obs.py**

`backend/app/obs.py`:
```python
"""Observability: JSON logs, request IDs, Sentry.

One line of JSON per request on stdout — grep-able on a VPS, parseable by
any log shipper later. Request IDs round-trip via X-Request-ID so a client
report ("it failed, id abc123") finds the exact log line and Sentry event.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware

_EXTRA_FIELDS = ("request_id", "account_id", "job_id", "method", "path",
                 "status", "duration_ms")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for k in _EXTRA_FIELDS:
            v = getattr(record, k, None)
            if v is not None:
                out[k] = v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(os.getenv("RECONOPS_LOG_LEVEL", "INFO").upper())


access_log = logging.getLogger("reconops.access")


class RequestLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        started = time.time()
        extra = {
            "request_id": rid,
            "account_id": request.headers.get("X-Account-Id"),
            "method": request.method,
            "path": request.url.path,
        }
        try:
            response = await call_next(request)
        except Exception:
            access_log.error("unhandled error", extra={
                **extra, "duration_ms": int((time.time() - started) * 1000)})
            raise
        response.headers["X-Request-ID"] = rid
        access_log.info("request", extra={
            **extra, "status": response.status_code,
            "duration_ms": int((time.time() - started) * 1000)})
        return response


def setup_sentry() -> bool:
    """Env-gated: no SENTRY_DSN -> no-op. Returns whether Sentry is active."""
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return False
    import sentry_sdk
    sentry_sdk.init(
        dsn=dsn,
        environment=os.getenv("RECONOPS_ENV", "production"),
        traces_sample_rate=0.0,
    )
    return True
```

- [x] **Step 4: Wire into main.py**

Right after `app = FastAPI(...)`:
```python
from .obs import RequestLogMiddleware, setup_logging, setup_sentry

setup_logging()
setup_sentry()
app.add_middleware(RequestLogMiddleware)
```
(Middleware order: added after CORSMiddleware is fine — CORS wraps outermost either way for our purposes.)

- [x] **Step 5: Verify + commit**

Run: `python -m pytest -q` → all pass. Also boot `uvicorn app.main:app --port 8023`, hit `/api/health`, and eyeball one JSON line on stdout; kill the server.

```bash
git add backend/app/obs.py backend/app/main.py backend/tests/test_obs.py
git commit -m "feat: structured JSON access logs with round-tripped request IDs"
```

---

### Task 3: Sentry (backend) with account/job tags

**Files:**
- Modify: `backend/requirements.txt` (+ `sentry-sdk[fastapi]==2.19.2`)
- Modify: `backend/app/main.py` (tags in upload endpoint)
- Modify: `backend/.env.example` (document `SENTRY_DSN`)

- [x] **Step 1: Add the dependency**

Append `sentry-sdk[fastapi]==2.19.2` to `backend/requirements.txt`; run `pip install -r requirements.txt`.

- [x] **Step 2: Tag events with account + job**

In `upload_and_reconcile`, immediately after `job_id = str(uuid.uuid4())`:
```python
    # Tag Sentry events so a production exception carries the context needed
    # to find the job. No-ops harmlessly when Sentry isn't initialized.
    import sentry_sdk
    sentry_sdk.set_tag("account_id", account.id)
    sentry_sdk.set_tag("job_id", job_id)
```

- [x] **Step 3: Document the env var**

Append to `backend/.env.example`:
```
# Optional: Sentry DSN for error tracking (empty = disabled)
SENTRY_DSN=
```

- [x] **Step 4: Verify + commit**

Run: `python -m pytest -q && python -m app.eval` → pass (Sentry disabled without DSN, zero behavior change).
Optional live check (needs a real DSN): `SENTRY_DSN=<dsn>` + boot + hit an endpoint that raises → event appears in Sentry with the tags.

```bash
git add backend/requirements.txt backend/app/main.py backend/.env.example
git commit -m "feat: env-gated Sentry with account/job tags"
```

---

### Task 4: Retention scheduler via lifespan

Replaces the per-upload `storage.cleanup()` (Task 8 stopgap) and the deprecated `@app.on_event("startup")` with a proper hourly loop.

**Files:**
- Modify: `backend/app/main.py:74-77` (startup hook), upload endpoint (remove cleanup call)

- [x] **Step 1: Implement the lifespan**

Replace the `@app.on_event("startup")` block with (before `app = FastAPI(...)`):
```python
import asyncio
from contextlib import asynccontextmanager

_retention_logger = logging.getLogger("reconops.retention")


async def _retention_loop():
    while True:
        try:
            storage.cleanup()
        except Exception:
            _retention_logger.exception("retention cleanup failed")
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app):
    storage.ensure_dirs()
    task = asyncio.create_task(_retention_loop())
    yield
    task.cancel()
```
and pass it: `app = FastAPI(title="ReconOps AI", version="0.1.0", lifespan=lifespan)`.
Add `import logging` to main.py imports if not present.

- [x] **Step 2: Remove the per-upload cleanup**

Delete the `storage.cleanup()` call (and its comment) at the top of `upload_and_reconcile` — the scheduler owns retention now.

- [x] **Step 3: Verify + commit**

Run: `python -m pytest -q` → pass (TestClient's context manager exercises the lifespan).
Run: `grep -n "on_event\|storage.cleanup()" backend/app/main.py` → only the lifespan's loop call remains.

```bash
git add backend/app/main.py
git commit -m "feat: hourly retention scheduler via lifespan (replaces per-upload cleanup)"
```

---

### Task 5: Production images — Caddy edge + compose stack

**Files:**
- Create: `deploy/Dockerfile.edge`, `deploy/Caddyfile`, `deploy/env.example`
- Create: `docker-compose.prod.yml`

- [x] **Step 1: Edge image (multi-stage)**

`deploy/Dockerfile.edge`:
```dockerfile
FROM node:20-alpine AS build
WORKDIR /app
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ .
RUN npm run build

FROM caddy:2-alpine
COPY deploy/Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/dist /srv
```

`deploy/Caddyfile`:
```
# RECONOPS_DOMAIN=recon.example.com -> automatic HTTPS via Let's Encrypt.
# Unset (local) -> plain HTTP on :80.
{$RECONOPS_DOMAIN::80} {
	encode gzip

	handle /api/* {
		reverse_proxy backend:8000
	}

	handle {
		root * /srv
		try_files {path} /index.html
		file_server
	}
}
```
(Note: Caddyfile requires tabs for indentation.)

- [x] **Step 2: Production compose file**

`docker-compose.prod.yml` (repo root):
```yaml
services:
  backend:
    build: ./backend
    restart: unless-stopped
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips=*
    environment:
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:?set in .env}
      - ANTHROPIC_MODEL=${ANTHROPIC_MODEL:-}
      - ANTHROPIC_ROW_MODEL=${ANTHROPIC_ROW_MODEL:-claude-haiku-4-5}
      - RECONOPS_MAX_LLM_ROWS=${RECONOPS_MAX_LLM_ROWS:-25}
      - SENTRY_DSN=${SENTRY_DSN:-}
      - RECONOPS_ENV=${RECONOPS_ENV:-production}
      - RECONOPS_CORS_ORIGINS=${RECONOPS_CORS_ORIGINS:-}
    volumes:
      - reconops-data:/app/data

  edge:
    build:
      context: .
      dockerfile: deploy/Dockerfile.edge
    restart: unless-stopped
    depends_on:
      - backend
    ports:
      - "80:80"
      - "443:443"
    environment:
      - RECONOPS_DOMAIN=${RECONOPS_DOMAIN:-}
    volumes:
      - caddy-data:/data
      - caddy-config:/config

volumes:
  reconops-data:
  caddy-data:
  caddy-config:
```

`deploy/env.example` (copied to `<repo>/.env` on the server — `.env` is gitignored):
```
# --- required ---
ANTHROPIC_API_KEY=sk-ant-...
RECONOPS_DOMAIN=recon.example.com   # leave empty for local HTTP-only runs

# --- optional ---
SENTRY_DSN=
ANTHROPIC_MODEL=
ANTHROPIC_ROW_MODEL=claude-haiku-4-5
RECONOPS_MAX_LLM_ROWS=25
RECONOPS_CORS_ORIGINS=
RECONOPS_ENV=production
```

- [ ] **Step 3: Verify the full stack locally** *(pending: Docker engine blocked by kernel-held stale socket — verify after next Windows reboot; CI builds both images meanwhile)*

```bash
cd <repo-root>
ANTHROPIC_API_KEY=sk-local-test docker compose -f docker-compose.prod.yml up --build -d
curl -s http://localhost/api/health          # -> {"ok":true,...,"version":"0.1.0"}
curl -s http://localhost/ | head -3          # -> built index.html
docker compose -f docker-compose.prod.yml logs backend | head -5   # JSON log lines
docker compose -f docker-compose.prod.yml down
```
Expected: health OK **through the proxy**, static frontend served, JSON logs visible. (Docker 29 is installed on this machine — run this for real, not hypothetically.)

- [x] **Step 4: Commit**

```bash
git add deploy/ docker-compose.prod.yml
git commit -m "feat: production compose stack — Caddy edge with auto-HTTPS + built frontend"
```

---

### Task 6: CI builds the prod images

**Files:**
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Add the job**

Append to `.github/workflows/ci.yml`:
```yaml
  prod-images:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Backend image
        run: docker build -t reconops-backend ./backend
      - name: Edge image (frontend build + Caddy)
        run: docker build -t reconops-edge -f deploy/Dockerfile.edge .
```

- [ ] **Step 2: Commit, push, verify all three CI jobs green**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: build production images on every push"
```
After merge to main: `gh run watch <id> --exit-status` → success.

---

### Task 7: Deployment runbook

**Files:**
- Create: `docs/DEPLOY.md`
- Modify: `README.md` (link to it), `PRODUCTIZATION_PLAN.md` (§2.4 note pointing here)

- [ ] **Step 1: Write the runbook**

`docs/DEPLOY.md` must contain, with exact commands (no placeholders except `<domain>`/`<server-ip>`):
1. **Prerequisites** — Ubuntu 24.04 VPS (1 vCPU / 1GB is enough for pilot), a domain, DNS A record `<domain> -> <server-ip>`.
2. **Install Docker** — the four official `apt` commands from docs.docker.com/engine/install/ubuntu.
3. **Bring-up** —
   ```bash
   git clone https://github.com/ramareddy1/DRAS.git && cd DRAS
   cp deploy/env.example .env && nano .env   # set ANTHROPIC_API_KEY + RECONOPS_DOMAIN (+ SENTRY_DSN)
   docker compose -f docker-compose.prod.yml up --build -d
   ```
4. **Smoke test** — `curl -s https://<domain>/api/health` → `{"ok":true,...}`; open the site, upload the two bundled sample CSVs, download the Excel export.
5. **Update procedure** — `git pull && docker compose -f docker-compose.prod.yml up --build -d` (compose recreates only changed services).
6. **Logs** — `docker compose -f docker-compose.prod.yml logs -f backend` (one JSON line per request; grep by `request_id`).
7. **Backup** — nightly crontab entry:
   ```
   0 3 * * * docker run --rm -v dras_reconops-data:/data -v /root/backups:/backup alpine tar czf /backup/reconops-$(date +\%F).tgz -C /data . && find /root/backups -name 'reconops-*.tgz' -mtime +14 -delete
   ```
   (volume name is `<dirname>_reconops-data`; verify with `docker volume ls`.)
8. **Restore** — stop stack, untar into the volume with the mirror-image `docker run ... tar xzf`, start stack.

- [ ] **Step 2: Cross-link + commit**

Add a "## Deploying" section to `README.md` pointing at `docs/DEPLOY.md`; add "Implemented — see docs/DEPLOY.md and docs/plans/2026-07-13-production-deployment.md" under §2.4 in `PRODUCTIZATION_PLAN.md`.

```bash
git add docs/DEPLOY.md README.md PRODUCTIZATION_PLAN.md
git commit -m "docs: production deployment runbook"
```

---

## Definition of done

- `docker compose -f docker-compose.prod.yml up --build` on a fresh clone serves the app at `http://localhost` (locally) and `https://<domain>` (VPS) with a working upload→results→export flow.
- Every request emits one JSON log line with a request ID that round-trips in `X-Request-ID`.
- With `SENTRY_DSN` set, a forced exception during upload appears in Sentry tagged `account_id` + `job_id`.
- Retention runs hourly without requests.
- CI builds both prod images on every push.
