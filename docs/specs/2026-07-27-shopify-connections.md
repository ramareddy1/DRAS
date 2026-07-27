# Shopify Connections — Design Spec

**Status:** Approved (brainstorming), pending implementation plan.
**Scope:** First sub-project of Phase 3.1 (`PRODUCTIZATION_PLAN.md`). Covers a
generic connections/credential-storage framework plus a Shopify custom-app
connector with on-demand order pulls into the existing reconcile flow.
Explicitly **out of scope**: Stripe Connect, the weekly scheduler, and
auto-reconcile-and-email — each is a separate future spec.

## Why this scope

Phase 3.1 as written ("read-only Shopify app + Stripe Connect; scheduled
weekly auto-pull → auto-reconcile → email") bundles several independent
subsystems: a connections framework, a Shopify connector, a Stripe Connect
connector, and a scheduler/auto-reconcile/email pipeline. This spec covers
only the first two — the smallest slice that ships real, demoable value
("connect once, stop re-uploading Shopify orders every week") without
requiring the others to exist first.

## 1. Architecture: the connector just produces a CSV

The core idea: **the Shopify connector's only job is to produce a
`File`/CSV — the exact same artifact a manual upload already produces.**
Nothing downstream of that (preview, column binding, `run_job`, results)
changes.

Flow for "Source A = Shopify":

1. User clicks "Use Shopify orders" next to the Source A dropzone on the
   upload page (shown only if a connection exists), picks a start/end
   date, clicks "Fetch."
2. Frontend calls `POST /api/connections/shopify/orders` with
   `{start_date, end_date}`.
3. Backend loads the account's stored (decrypted) access token, pulls
   orders from Shopify's Admin API for that date range (paginated), maps
   them into a DataFrame with ontology-recognized column names, and
   returns the result as CSV bytes (`text/csv`,
   `Content-Disposition: attachment`).
4. Frontend wraps those bytes in `new File([blob],
   "shopify-orders-<start>_<end>.csv")` and hands it to the upload page's
   **existing** `handleFile("a", file)` — the same function a
   drag-and-drop upload already calls.
5. From that point on — preview, column mapping, bind, submit,
   `/api/upload`, `run_job` — is **identical, unmodified code**. The
   reconcile engine never knows the file didn't come from disk.

This means the feature only adds: (a) credential storage/connect UI, and
(b) one endpoint that turns "date range" into "CSV bytes." It reuses 100%
of the existing reconciliation pipeline (`run_job` is already
DataFrame-in, source-agnostic).

## 2. Shopify auth model: Custom App token

Merchant creates a Custom App in their own Shopify Admin (Settings → Apps
and sales channels → Develop apps), scopes it read-only (`read_orders`),
and pastes the generated Admin API access token + shop domain into
ReconOps. No OAuth flow, no Shopify Partner account, no app review, no
redirect/callback endpoints. This is a deliberate trade against a
Shopify-Partners OAuth "Connect" button: OAuth gives smoother onboarding
but requires Partner registration and (for public listing) app review —
out of proportion to this slice's scope. OAuth can be added later as an
alternative onboarding path without changing how tokens are stored or
used downstream.

One Shopify connection per account (enforced by a DB unique constraint).
Connecting a new store replaces the old one. Multi-store-per-account is
out of scope; revisit if it comes up.

## 3. Data model

New table, migration `0007_connections.py`, following the existing
`payload`-JSONB-plus-queryable-columns convention in
`app/db/models.py`:

```python
class ConnectionORM(Base):
    __tablename__ = "connections"

    id = Column(String(36), primary_key=True)
    account_id = Column(String(36), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    provider = Column(String, nullable=False, index=True)          # "shopify"
    status = Column(String, nullable=False, default="connected")   # connected | error
    payload = Column(JSONB, nullable=False, default=dict)          # shop_domain, encrypted_token, scopes, connected_at, last_synced_at
    __table_args__ = (UniqueConstraint("account_id", "provider", name="uq_connection_account_provider"),)
```

- `account_id` FK with `ondelete="CASCADE"` joins the same cascade family
  as `jobs`/`rules`/`triage_items`/`decisions`/`metrics`. **No changes
  needed to `delete_account`** (`app/memory/accounts.py`), which already
  relies entirely on FK cascades for these tables.
- The unique constraint enforces "one Shopify connection per account" at
  the DB level.
- The access token is encrypted (Fernet, `cryptography` package — new
  dependency) using a server-side key from a new
  `RECONOPS_ENCRYPTION_KEY` env var, and lives inside `payload` — never a
  plain column, never returned by any endpoint. Follows the same
  env-var-secret pattern already used for SMTP
  (`app/auth/emailer.py`'s `smtp_configured()` guard). If the key isn't
  set, the connect endpoint returns a clear 500 ("Integrations not
  configured on this server") rather than failing silently.
- A corresponding Pydantic `Connection` model (id, provider, shop_domain,
  status, connected_at, last_synced_at — **no token field**) is added to
  `app/models.py`, so it's structurally impossible to leak the token
  through any response that returns this model.

## 4. Backend API surface

New file `app/routers/connections.py` (a FastAPI `APIRouter`, mounted in
`main.py` via `app.include_router`) — `main.py` is already ~850 lines
with every endpoint inline; this is the one structural change made here,
scoped only to the new feature, not touching existing routes.

```
POST   /api/connections/shopify          — connect (owner-only)
GET    /api/connections                  — list this account's connections (any member)
DELETE /api/connections/{provider}       — disconnect (owner-only, no-op if none)
POST   /api/connections/shopify/orders   — pull orders as CSV (any member)
```

**`POST /api/connections/shopify`** — body `{shop_domain, access_token}`.
Validates `shop_domain` matches `^[a-z0-9-]+\.myshopify\.com$`. Makes one
test call (`GET /admin/api/{version}/shop.json`) to confirm the token
actually works before saving anything — a bad token fails fast with a
clear 400, not silently on first sync. On success: encrypt token, upsert
the `connections` row, return the (secret-free) `Connection` model.
Gated with the existing `require_owner` dependency (same as retention
settings / account deletion) since this is credential management.

**`GET /api/connections`** — gated with `require_account` (any member).
Returns `[]` if none connected.

**`DELETE /api/connections/{provider}`** — `require_owner`, deletes the
row; no-ops (204) if nothing exists for that provider.

**`POST /api/connections/shopify/orders`** — body
`{start_date, end_date}` (ISO dates). `require_account`. Loads +
decrypts the token, pulls orders, returns `text/csv` with
`Content-Disposition: attachment`. Updates `last_synced_at` on success.
Returns 404 if no Shopify connection exists for the account, 400 for an
invalid or oversized date range.

## 5. Shopify connector internals

New file `app/integrations/shopify.py` — isolated from the router (the
router handles HTTP/auth concerns; this handles "talk to Shopify and
produce a DataFrame"). Core function, pure and independently testable:

```python
def fetch_orders(shop_domain: str, access_token: str, start_date: date, end_date: date) -> pd.DataFrame:
    ...
```

- **API client:** `httpx` (new dependency), sync usage — these are
  short-lived, request-scoped calls; async adds no value here.
- **API version:** pinned constant, e.g. `SHOPIFY_API_VERSION = "2024-01"`.
- **Pagination:** Shopify's Orders API is cursor-based via the response
  `Link` header (`rel="next"`), not page numbers. Loop: request → parse
  `page_info` cursor from `Link` → follow until no `next` link remains.
  Page size `limit=250` (Shopify's max).
- **Rate limiting:** Shopify's REST Admin API allows ~2 requests/sec
  (leaky bucket). On `429`, read `Retry-After` and sleep, up to 3 retries
  per page; if still rate-limited after that, raise so the router can
  surface a 502 ("Shopify rate-limited this request — try a smaller date
  range or retry shortly").
- **Date-range cap:** since this runs synchronously inside one HTTP
  request (no background job for the fetch itself), cap the requestable
  range at **180 days** — reject longer ranges with 400 before making any
  Shopify calls.
- **Column mapping** (Shopify field → DataFrame column, chosen to match
  existing `app/ontology/concepts.yaml` aliases so semantic binding works
  with zero ontology changes):

  | Shopify field | DataFrame column | Ontology role matched |
  |---|---|---|
  | `name` (e.g. `#1001`) | `order_id` | primary_key/orders |
  | `created_at` | `created_at` | event_time/shared |
  | `total_price` | `order_total` | primary_amount/orders |
  | `total_tax` | `tax` | component/orders |
  | `currency` | `currency` | attribute/shared |
  | `financial_status` | `order_status` | attribute/shared |

## 6. Frontend changes

**`SettingsPage.jsx`** — new "Integrations" section, following the exact
owner-gated pattern the "Data retention" section already uses:

- Not connected: shop domain input (placeholder
  `yourstore.myshopify.com`), access token input (`type="password"`),
  "Connect" button, plus a one-line note on where to generate the token
  in Shopify Admin. Calls a new `connectShopify({shop_domain,
  access_token})` client function; inline error on failure.
- Connected: shows shop domain, connected date, last-synced time (or
  "Never synced"), and a "Disconnect" button — gated behind `isOwner`,
  same as the retention Save button and Danger Zone.
- Fetched via a new `getConnections()` call added to the page's existing
  `useEffect` alongside `getMyAccount()`/`getMe()`.

**`UploadPage.jsx`** — minimal, additive change to the existing per-source
block:

- On mount, also call `getConnections()` to know if Shopify is connected
  (`hasShopify`).
- Next to each `DropZone` (both Source A and Source B — not tied to recon
  type), if `hasShopify`, show a toggle: "or use Shopify orders." Picking
  it swaps that slot's UI from `DropZone` to a compact date-range picker
  + "Fetch" button, reusing the same `which` ("a"/"b") plumbing already
  in the component.
- "Fetch" calls a new `fetchShopifyOrders({start_date, end_date})`, gets
  the CSV blob back, wraps it as `new File([blob],
  "shopify-orders-<start>_<end>.csv", {type: "text/csv"})`, and calls the
  **existing, unmodified** `handleFile(which, file)`. Everything from
  there (preview, `ColumnMapper`, submit) is unchanged code.
- Toggling back to "Upload file" clears that slot's state, same as
  picking a different file today.

No new page, no new route.

## 7. Error handling and edge cases

- **Invalid/stale token:** caught at connect time by the `shop.json` test
  call. If a token is later revoked externally, the next `/orders` call
  gets a 401 from Shopify; the connector surfaces a 502 with detail
  ("Shopify rejected the stored credential — reconnect your store") and
  marks the connection `status="error"` so Settings can show a
  "Needs reconnect" state.
- **Wrong shop domain format:** rejected with 400 before any Shopify call.
- **Date range too large:** rejected with 400 before any Shopify call
  (180-day cap).
- **No connection when `/orders` is called:** 404 with a clear message.
- **Rate limiting:** handled inside the connector (up to 3 retries per
  page, honoring `Retry-After`); exhausted retries surface as 502.
- **Empty result (no orders in range):** not an error — returns a valid
  CSV with header row only; the existing preview/bind flow already
  handles a near-empty file the same way it handles an empty CSV upload
  (fails the existing "bind at least one primary_key" gate, or shows an
  empty preview) — no new handling needed.
- **Account deletion mid-connection:** covered structurally by the FK
  cascade (section 3) — no code path needed.
- **Concurrent connect race** (two tabs connecting simultaneously): the
  DB unique constraint on `(account_id, provider)` makes this an upsert —
  last write wins, no corruption, no special locking (matches how
  `update_profile` already works for account settings).

## 8. New dependencies and env vars

`backend/requirements.txt`: add `httpx` and `cryptography`.

`backend/.env.example`, new section:

```
# --- integrations ---
RECONOPS_ENCRYPTION_KEY=   # Fernet key (44-char urlsafe base64) for encrypting connection credentials at rest.
                           # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## 9. Testing plan

**Backend** (new `backend/tests/test_connections.py`):

- `fetch_orders()` unit tests, mocking Shopify HTTP responses
  (`unittest.mock` or `respx`) — no real network calls. Cover: single
  page, multi-page (`Link` header pagination), 429 → retry-after →
  success, and column-mapping correctness (confirm the mapped columns
  actually bind to the expected ontology role via `bind_columns`).
- Encryption round-trip test for the token encrypt/decrypt helper.
- Endpoint tests: connect with valid/invalid token (mocking the
  `shop.json` validation call); connect requires owner (403 for a
  non-owner member); disconnect no-ops cleanly when nothing exists;
  `/orders` 404s with no connection; 400 on bad domain / oversized date
  range; account-isolation (account A can't see or use account B's
  connection).
- Full suite re-run at the end:
  `RECONOPS_DATABASE_URL=postgresql://reconops:reconops@localhost:5433/reconops_test .venv/Scripts/python.exe -m pytest -q`

**Frontend:** no automated test suite exists in this repo. Manual
in-browser verification: connect flow (good token, bad token), Settings
showing connected state + disconnect, the upload-page toggle appearing
only when connected, fetching a date range and confirming it flows into
the existing preview/bind/reconcile screens unchanged, and `npm run
build` succeeding.

## Explicitly out of scope (future specs)

- Stripe Connect connector.
- Weekly scheduler / auto-pull.
- Auto-reconcile (fully automatic `run_job` invocation without user
  review) — this spec keeps the user in the loop via the existing
  preview/bind screens.
- Emailing reconciliation results.
- Shopify OAuth ("Connect" button / Partner app) as an alternative to the
  Custom App token flow.
- Multi-store-per-account.
