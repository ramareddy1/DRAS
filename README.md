# ReconOps AI

**Upload. Reconcile. Know where your money is.**

An AI-assisted reconciliation service for small e-commerce brands ($1M–$20M
revenue). Upload exports from any two systems (Shopify, Stripe, 3PL portals,
accounting, supplier invoices) and get a clear, deterministic report: what
matches, what doesn't, and how much money is at stake — with a per-account
rules engine that learns which gaps are expected.

The original architecture rationale lives in [PLAN.md](PLAN.md); the
productization roadmap and its progress live in
[PRODUCTIZATION_PLAN.md](PRODUCTIZATION_PLAN.md).

**Status:** hardened past the pilot stage. Email-code sign-in with cookie
sessions and owner/analyst roles, deterministic classification with a
capped AI review, many-to-one matching proven on 20k rows of real
marketplace data, CI-guarded tests + eval, and a one-runbook production
deployment (Caddy edge, auto-HTTPS). Still JSON-on-disk and synchronous
(Postgres and async jobs are the next roadmap phases).

---

## Project layout

```
DRAS/
├── frontend/                React + Vite + Tailwind UI (login gate, upload,
│   └── src/                 results, inbox, rules, metrics, history)
├── backend/
│   └── app/
│       ├── main.py          HTTP endpoints (all session-gated)
│       ├── agent.py         Reconciliation orchestrator
│       ├── auth/            OTP sign-in, sessions, membership, export tokens
│       ├── tools/           Ingest, binding, matching, amounts, classify
│       ├── memory/          Per-account rules, triage, decisions, metrics
│       ├── ontology/        Concept graph for column auto-binding
│       ├── obs.py           JSON access logs + request IDs + Sentry
│       ├── eval.py          Deterministic regression eval (runs in CI)
│       └── report.py        Excel export
├── samples/                 Demo CSVs + Olist real-data pair builder
├── deploy/                  Caddyfile, edge image, server env template
├── docker-compose.prod.yml  Production stack (see docs/DEPLOY.md)
└── docs/
    ├── DEPLOY.md            VPS runbook
    └── plans/               Executed implementation plans
```

---

## Quickstart (local dev)

### 1. Backend

```bash
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env       # set ANTHROPIC_API_KEY; set RECONOPS_AUTH_DEV=1
RECONOPS_AUTH_DEV=1 uvicorn app.main:app --reload --port 8000
```

`RECONOPS_AUTH_DEV=1` makes sign-in codes appear on the login screen instead
of requiring SMTP — local only, never in production.

### 2. Frontend

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173, sign in with any email (the 6-digit code shows
inline in dev mode) — a workspace is created for you automatically.

### 3. Demo data

| Recon type | Source A | Source B |
|---|---|---|
| Orders vs. Payments | `samples/shopify_orders.csv` | `samples/stripe_payments.csv` |
| Inventory | `samples/shopify_inventory.csv` | `samples/threepl_stock_report.csv` |

Columns auto-bind via the ontology — you usually don't have to touch the
mapper. For a real-world stress test, drop the Kaggle Olist dataset into
`samples/Kaggle/Olist_datasets/` and run
`python samples/build_olist_pair.py` (20k orders, multi-payment vouchers,
genuine discrepancies).

### 4. Tests & eval

```bash
cd backend
python -m pytest -q        # unit + endpoint tests
python -m app.eval         # deterministic regression eval (exit 0 = pass)
```

Both run in CI on every push, plus production image builds.

---

## What the engine does

1. **Ingestion** — CSV (UTF-8/BOM/Latin-1) or XLSX; headers and cells
   normalized.
2. **Column binding** — ontology aliases + value-shape signals auto-map
   columns to concepts; per-account learned aliases take precedence.
3. **Many-to-one matching** — rows sharing a normalized key are aggregated
   (multiple payments/vouchers settling one order), then matched exact-first
   with a fuzzy fallback. Low-confidence joins proceed with a visible warning
   instead of stalling. Mixed currencies are refused loudly.
4. **Deterministic classification** — `match` / `minor` / `major` per
   account-configurable tolerance and materiality thresholds. Fee shapes
   (Stripe/PayPal seeded per account) live in the rules store, so revoking a
   fee rule actually changes verdicts.
5. **Capped AI review** — the top discrepancies by $ impact (default 25) get
   one batched advisory pass; it adds evidence to the audit trail but can
   never flip a verdict. An LLM outage degrades gracefully — jobs always
   complete.
6. **Learning loop** — recurring gaps dedupe into a triage inbox; repeated
   "expected" decisions propose rules (amount-capped so a rule taught on fee
   noise can never hide a large discrepancy, with a blast-radius preview
   before accepting). Every decision records who made it.
7. **Excel export** — multi-tab workbook via short-lived signed download
   tokens.

---

## API (all session-gated except `/api/health` and `/api/auth/*`)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/auth/request-code` · `/verify` · `/logout`, GET `/me` | Email-OTP sign-in, cookie sessions |
| POST | `/api/accounts` · `/api/accounts/claim` | Create workspace / claim a legacy one |
| GET/POST | `/api/accounts/me/members` | Team + roles (owner/analyst) |
| PATCH | `/api/accounts/me/profile` | Tolerances & materiality (owner) |
| POST | `/api/preview` · `/api/upload` | Column preview / run a reconciliation |
| GET | `/api/jobs` · `/api/status/{id}` · `/api/results/{id}` | History & results |
| POST | `/api/results/{id}/export-token` → GET `.../export?token=` | Tokenized Excel download |
| GET/POST | `/api/inbox` · `/api/triage/{id}/resolve` · `/api/decisions` | Triage & decisions |
| GET/POST | `/api/rules` (+ `/preview`, `/accept`, `/revoke`) | Rules lifecycle |
| GET | `/api/metrics/series` · `/api/compare/{id}/{prev}` | Insight-density trend, job diff |

---

## Current constraints (next roadmap phases)

- **JSON-on-disk storage** (atomic writes + per-account locks) — Postgres +
  object storage is Phase 2.2.
- **Synchronous reconciliation** inside the upload request (fine ≤10MB
  files) — background jobs are Phase 2.3.
- **Single machine** — by design until the above land.

Data retention runs hourly (24h uploads / 7d results). See
[PRODUCTIZATION_PLAN.md](PRODUCTIZATION_PLAN.md) for the full phase status.

---

## Docker (dev)

```bash
docker compose up --build
```

## Deploying (production)

A single-VPS production stack (Caddy edge with automatic HTTPS + built
frontend, uvicorn backend, named data volume, SMTP-backed sign-in) ships in
`docker-compose.prod.yml`. Full runbook — bring-up, auth setup, smoke tests,
updates, backups: **[docs/DEPLOY.md](docs/DEPLOY.md)**.
