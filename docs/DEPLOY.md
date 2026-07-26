# Deploying ReconOps AI

Production runs as a two-service Docker Compose stack on a single Linux VPS:
**backend** (uvicorn) and **edge** (Caddy serving the built frontend and
reverse-proxying `/api`, with automatic HTTPS via Let's Encrypt). Data lives
in a named Docker volume — see [Backups](#7-backups).

## 1. Prerequisites

- A VPS running **Ubuntu 24.04** — 1 vCPU / 1 GB RAM is enough for pilot load
  (Hetzner CX11, DigitalOcean basic droplet, etc.)
- A **domain or subdomain** you control (e.g. `recon.example.com`)
- A **DNS A record** pointing that name at the server's IP — create it before
  bring-up so Let's Encrypt issuance succeeds on the first start
- Your **Anthropic API key**, and optionally a **Sentry DSN**

## 2. Install Docker (on the server)

The canonical steps from <https://docs.docker.com/engine/install/ubuntu/>
(check there if these drift):

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update

sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

## 3. Bring-up

```bash
git clone https://github.com/ramareddy1/DRAS.git && cd DRAS
cp deploy/env.example .env
nano .env        # set ANTHROPIC_API_KEY and RECONOPS_DOMAIN (+ SENTRY_DSN if you have one)
sudo docker compose -f docker-compose.prod.yml up --build -d
```

First start takes a few minutes (image builds + certificate issuance).
`.env` is gitignored — it never leaves the server.

## 3b. Auth setup

Sign-in works by emailing a 6-digit code, so production needs SMTP
credentials in `.env` (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`,
`SMTP_FROM`). Any transactional provider works — the free tiers of Resend
or Postmark are plenty for a pilot. Without SMTP, `/api/auth/request-code`
returns 503 and nobody can sign in. Never set `RECONOPS_AUTH_DEV` in
production — it prints sign-in codes in API responses.

**Migrating pre-auth pilot users:** they just sign in with their email in
the same browser they used before — the app claims their old workspace from
the browser's stored UUID automatically (one-time, first claimant wins).

The `data/auth/` directory (users, hashed sessions, HMAC secret) lives on
the same volume as everything else, so the nightly backup already covers it.

## 3c. Database migration

After the first bring-up (and after every `git pull` that adds a new file
under `backend/alembic/versions/`), apply migrations:

```bash
sudo docker compose -f docker-compose.prod.yml exec backend alembic upgrade head
```

**Migrating a pre-Postgres install:** if this server was running before
Phase 2.2, its account/job/rule/triage/decision/metric data is still JSON
files on the `reconops-data` volume. Run the one-shot importer once, after
migrating the schema and before routing real traffic:

```bash
sudo docker compose -f docker-compose.prod.yml exec backend python -m scripts.import_json_to_postgres --data-dir data
```

It's safe to re-run — it upserts by existing id and skips already-imported
decision/metric logs.

## 4. Smoke test

```bash
curl -s https://<domain>/api/health
# -> {"ok":true,"llm_configured":true,"version":"0.1.0"}
```

Then in a browser: open `https://<domain>`, upload the two bundled sample
files (`samples/shopify_orders.csv` + `samples/stripe_payments.csv`), confirm
the results page renders, and download the Excel export.

## 5. Updating

```bash
cd DRAS
git pull
sudo docker compose -f docker-compose.prod.yml up --build -d
```

Compose rebuilds and recreates only the services whose inputs changed.
Confirm with the health endpoint (`version` tells you what's running).

## 6. Logs & debugging

```bash
sudo docker compose -f docker-compose.prod.yml logs -f backend
```

The backend emits one JSON line per request. Every response carries an
`X-Request-ID` header; when a user reports a failure, grep for it:

```bash
sudo docker compose -f docker-compose.prod.yml logs backend | grep '"request_id": "<id>"'
```

With `SENTRY_DSN` set, unhandled exceptions also land in Sentry tagged with
`account_id` and `job_id`.

## 7. Backups

Two things need backing up: the `postgres-data` volume (accounts, jobs,
rules, triage items, decisions, metrics — the data of record) and the
`reconops-data` volume (local cache only — safe to lose, but backed up for
simplicity). Full volume names are `<project>_postgres-data` and
`<project>_reconops-data` where `<project>` is the repo directory name
lowercased (clone as `DRAS` -> `dras_postgres-data`; confirm with
`docker volume ls`).

Nightly backup with 14-day rotation — add via `sudo crontab -e`:

```
0 3 * * * docker compose -f /path/to/DRAS/docker-compose.prod.yml exec -T postgres pg_dump -U reconops reconops | gzip > /root/backups/postgres-$(date +\%F).sql.gz && find /root/backups -name 'postgres-*.sql.gz' -mtime +14 -delete
0 3 * * * docker run --rm -v dras_reconops-data:/data -v /root/backups:/backup alpine tar czf /backup/reconops-$(date +\%F).tgz -C /data . && find /root/backups -name 'reconops-*.tgz' -mtime +14 -delete
```

## 8. Restore

```bash
cd DRAS
sudo docker compose -f docker-compose.prod.yml up -d postgres
gunzip -c /root/backups/postgres-<YYYY-MM-DD>.sql.gz | sudo docker compose -f docker-compose.prod.yml exec -T postgres psql -U reconops reconops
sudo docker compose -f docker-compose.prod.yml down
sudo docker run --rm -v dras_reconops-data:/data -v /root/backups:/backup alpine \
  sh -c "rm -rf /data/* && tar xzf /backup/reconops-<YYYY-MM-DD>.tgz -C /data"
sudo docker compose -f docker-compose.prod.yml up -d
```

## Local production-stack test (no domain)

Leave `RECONOPS_DOMAIN` empty and the edge serves plain HTTP on port 80:

```bash
ANTHROPIC_API_KEY=sk-... docker compose -f docker-compose.prod.yml up --build -d
curl -s http://localhost/api/health
```
