# Data Handling & Governance

*Last reviewed: 2026-07-26.*

One-page reference for answering a client's security questionnaire without
improvising. If a question isn't answered here, that's a gap to fix in this
doc, not something to answer ad hoc.

## What ReconOps AI is

ReconOps AI reconciles two files you upload (e.g. Shopify orders vs. Stripe
payments) and surfaces discrepancies, matches, and rule-based explanations.
**It is decision support, not accounting advice.** Every verdict is a
suggestion for a human to confirm, override, or teach a correction to — it
does not file, post, or amend anything in your books, and it is not a
substitute for a licensed accountant's judgment.

## Data flow

```
Browser (you)
   |  upload two files (CSV/XLSX), HTTPS
   v
ReconOps AI backend (single VPS, Docker)
   |  - raw files -> S3-compatible object storage (encrypted at rest, AES256)
   |  - structured rows/results -> Postgres (accounts, jobs, rules, decisions, metrics)
   |  - row-level context sent to Anthropic's Claude API for matching/explanation text
   v
Anthropic API (subprocessor -- see below)
   |  response used to render your results; not used to train Anthropic's models
   v
Results shown to you in-browser; exportable as an Excel workbook
```

No data is shared with any party besides Anthropic (the LLM subprocessor)
and, if the operator has configured a Sentry DSN, Sentry (error monitoring —
stack traces and account/job IDs only, never file contents).

## Subprocessors

| Subprocessor | Purpose | Data seen | Training use |
|---|---|---|---|
| Anthropic (Claude API) | Matching/explanation reasoning | Row-level transaction data sent in each request | Anthropic's commercial API does not use your data to train its models. |
| Sentry (optional, operator-configured) | Error monitoring | Stack traces, account/job IDs — never file contents | Not applicable — not an LLM. |

No other third party processes client data. Hosting is a single VPS the
operator controls, plus whichever S3-compatible storage provider they've
configured — there is no additional data-processing vendor beyond those two
rows.

## Retention & deletion

- **Uploaded files and job results:** kept for `retention_days` (default 7,
  configurable 1–365 per workspace via Settings → Danger Zone, or
  `PATCH /api/accounts/me/profile`), then permanently deleted — both the
  Postgres row and the underlying S3 object — by an hourly background sweep
  (`storage.cleanup()`).
- **Full workspace deletion:** any workspace owner can trigger a full purge
  — `DELETE /api/accounts/me`, exposed as "Delete workspace" in Settings →
  Danger Zone — which immediately and irreversibly deletes: the workspace's
  Postgres row and everything that references it (jobs, rules, triage
  items, decisions, metrics, via `ON DELETE CASCADE`), every S3 object
  under that workspace's prefix, and all local JSON state (membership
  list, learned aliases, observations, notes). This does not delete the
  requester's login — they may belong to other workspaces — and does not
  affect any subprocessor's own copy of already-sent API requests.
- **Backups:** nightly, 14-day rotation (see [DEPLOY.md](DEPLOY.md) §7). A
  deleted workspace can reappear in a restored backup for up to 14 days
  after deletion — disclose this if a client asks for guaranteed immediate
  erasure across every copy.

## Data processing agreement (plain-language template)

This is a starting point for a client's legal team to adapt, not a
signable contract on its own.

> **Data Processing Terms**
>
> 1. **Roles.** Client is the data controller. The operator of this
>    ReconOps AI instance is the data processor, acting only on Client's
>    instructions (uploading files, configuring rules, requesting
>    deletion).
> 2. **Scope.** Processor processes the transaction data Client uploads
>    solely to produce reconciliation results for Client, and to
>    incrementally improve Client's own reconciliation rules — never
>    shared across clients.
> 3. **Subprocessors.** Processor uses Anthropic's Claude API to generate
>    matching/explanation output, and may use Sentry for error monitoring.
>    See the Subprocessors table above; Processor will notify Client
>    before adding a new subprocessor.
> 4. **Retention.** Data is retained per Client's configured retention
>    period (default 7 days for job data) and deleted on request via full
>    workspace deletion, subject to the backup-rotation window disclosed
>    above.
> 5. **Security.** Files are encrypted at rest (AES256) and in transit
>    (HTTPS/TLS). Access is scoped per workspace; only members Client adds
>    can view Client's data.
> 6. **Deletion on termination.** On contract termination, Client may
>    trigger full workspace deletion at any time; Processor does not
>    retain Client data beyond the backup-rotation window after that.
> 7. **No independent use.** Processor does not sell, license, or use
>    Client's data for any purpose beyond providing the Service to Client.

## Questions this doc doesn't answer

If a client's security questionnaire asks something not covered above
(SOC 2, HIPAA, a specific region's data-residency requirement, a
subprocessor's own signed DPA on file), that's a real gap — say so rather
than improvising, and escalate to update this doc.
