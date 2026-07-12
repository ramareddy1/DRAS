# ReconOps AI — Pilot

**Upload. Reconcile. Know where your money is.**

An AI-powered data reconciliation service for small e-commerce brands ($1M–$20M revenue). Upload exports from any two systems (Shopify, Stripe, 3PL portals, accounting, supplier invoices) and get a clear report: what matches, what doesn't, and how much money is at stake.

This is a **pilot/MVP** — single-machine, JSON-on-disk storage, no auth. Designed to validate the concept with real users before investing in infrastructure. The architecture rationale lives in [PLAN.md](PLAN.md).

---

## Project layout

```
reconops-ai/
├── frontend/             React + Vite + Tailwind UI
│   └── src/
│       ├── pages/        Upload, Results, History
│       ├── components/   Layout, DropZone, ColumnMapper, DataTable
│       └── api/          Backend client
├── backend/              FastAPI reconciliation engine
│   └── app/
│       ├── main.py       HTTP endpoints
│       ├── reconciler.py Core matching + classification logic
│       ├── insights.py   Claude API + template fallback
│       ├── report.py     Excel report generator
│       ├── storage.py    JSON-on-disk job store
│       └── models.py     Pydantic schemas
├── samples/              Pre-generated demo CSVs + the generator
├── docker-compose.yml
└── PLAN.md               Architecture & design decisions
```

---

## Quickstart

### 1. Backend

```bash
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # optional: set ANTHROPIC_API_KEY for LLM insights
uvicorn app.main:app --reload --port 8000
```

Backend at http://localhost:8000. Health check: http://localhost:8000/api/health.

### 2. Frontend

```bash
cd frontend
npm install
npm run dev
```

Frontend at http://localhost:5173. Vite proxies `/api` → backend, so CORS is a non-issue locally.

### 3. Demo with the sample data

`/samples` contains two ready-to-use demo sets:

| Recon type | Source A | Source B | Key | Amount | Date |
|---|---|---|---|---|---|
| Orders vs. Payments | `shopify_orders.csv` | `stripe_payments.csv` | `order_id` ↔ `order_reference` | `order_total` ↔ `amount` | `order_date` ↔ `settlement_date` |
| Inventory | `shopify_inventory.csv` | `threepl_stock_report.csv` | `sku` ↔ `sku` | `quantity` ↔ `qty_on_hand` | — |

The column mapper auto-suggests the right columns — you usually don't have to touch it.

Regenerate the samples:
```bash
python samples/generate_samples.py
```

---

## What the engine does

1. **Ingestion** — pandas reads CSV (tries UTF-8, UTF-8-BOM, Latin-1) or XLSX. Headers and string cells are stripped. Numeric and date columns are coerced.
2. **Key matching** — exact match first; unmatched rows go through a fuzzy pass that normalizes prefixes (`#`, `ORD-`, `INV-`, `pi_`), case, whitespace, and leading zeros. Fuzzy matches are counted separately.
3. **Amount comparison** — each matched record is classified:
   - `match` — within $0.01 or 0.5% tolerance
   - `minor` — < $10 or < 3% difference
   - `major` — ≥ $100 or ≥ 3% difference
   - `fee_offset` — matches a known processor fee shape (Stripe 2.9% + $0.30, PayPal 2.99%, PayPal 3.49% + $0.49) — flagged so the user doesn't chase fees as losses
4. **Timing** — if date columns exist on both sides, computes mean / std / range of deltas and flags outliers beyond 2σ.
5. **AI insights** — Claude `claude-opus-4-7` writes a 3-section operations-analyst summary (quality, top patterns, suggested actions). Falls back to a deterministic template when no API key is configured.
6. **Excel export** — multi-tab workbook (Summary, Matched, Unmatched A, Unmatched B, Discrepancies, Insights) with color-coded status and conditional row formatting.

---

## API

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/api/health` | Liveness + LLM-configured flag |
| POST | `/api/preview` | Single file → first 5 rows + suggested column mapping |
| POST | `/api/upload` | Two files + config JSON → `{ job_id, status }` |
| GET  | `/api/status/{job_id}` | Job status |
| GET  | `/api/results/{job_id}` | Full result JSON |
| GET  | `/api/results/{job_id}/export` | Excel download |

---

## Pilot constraints (intentional)

- **No auth.** UUID job IDs act as access tokens. Ship auth later.
- **No database.** Jobs stored as JSON files under `backend/data/jobs/`.
- **No job queue.** Reconciliation runs synchronously inside the `POST /api/upload` request — expected runtime is 1–5s for files under 10MB.
- **Retention.** Uploaded source files cleaned up after 24h; result JSON kept for 7 days.
- **Single machine.** Frontend on `:5173`, backend on `:8000`.

See [PLAN.md](PLAN.md) for why each of those is a deliberate choice and what would change in a production build.

---

## Docker

```bash
docker compose up --build
```

Frontend at http://localhost:5173, backend at http://localhost:8000.
