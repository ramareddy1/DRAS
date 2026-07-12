# ReconOps AI — Architecture & Design Plan (v3, account-scoped, insight-density-driven)

> **Revision note (v3).** v2 made the AI-native turn (ontology, agent
> orchestrator, memory, rationale objects, HITL). v3 closes four structural
> gaps surfaced by the design critique:
>
> 1. **Account as a first-class entity.** Tribal knowledge is account-scoped
>    from day one. `brand_id` is replaced by an explicit `Account`.
> 2. **TriageItem as a cross-job entity.** Discrepancies live across jobs as
>    persistent items the user triages — not as a per-job list that resets.
> 3. **Causal vocabulary has explicit homes in the schema.** `Rationale` gains
>    a `user_reason` free-text field; `Rule` gains `user_origin_text`;
>    accounts get a `brand_notes` log + onboarding intake.
> 4. **Insight density is the headline metric**, with three counter-metrics
>    (override rate, revocation rate, trust-adjusted density) so the system
>    can't game its way into a black box.
>
> The four v2 commitments (AI-native, memory, HITL/explainability, ontology)
> survive unchanged. v3 adds a fifth commitment — **insight density as the
> measure of progress** — and threads it through the data contracts and the
> UI behavior.

---

## 1. Goal & non-goals

**Goal.** Validate, with real e-commerce operators, that a reconciliation
service that **learns each account's idiosyncrasies** is something they will
use repeatedly and pay for. Each reconciliation should make the next one
smarter, and the interface should show *less data and more insight* over time.

Two metrics together define "success" for an account:

- **Time-to-first-useful-result** under 2 minutes on day one.
- **Insight density** — the fraction of rows the system handles without the
  user having to look at them — rising from job to job, while override rate
  stays low (see §4.5 for the precise definition).

**Non-goals (for the pilot).**

- Multi-user-per-account collaboration (but accounts themselves are real).
- Authentication / login (account is a UUID generated on first visit;
  carried via header + localStorage).
- Realtime ingestion / direct system integrations (Shopify webhooks, Stripe
  API). File upload remains the universal entry point.
- High concurrency / horizontal scale.

**Required, not deferred** (changed from v2):

- The LLM is on the critical path. Failing loudly when unavailable.
- Every classification ships with a rationale **plus** an optional free-text
  `user_reason` capture surface when the user disagrees with it.
- Every classification is correctable; corrections persist to the account.
- Shared vocabulary (ontology) for the engine and the agent.
- Account-scoped memory with a clean read order (account first, ontology
  second).
- Insight density + counter-metrics tracked from day one.

---

## 2. Target user & job-to-be-done

- **User**: ops / finance lead at a $1M–$20M e-commerce brand.
- **Pain**: 2–6 hours/month manually VLOOKUP-ing exports. The same manual
  diffing repeats every month — the work is **not getting easier**.
- **JTBD**: "When I close the month, help me confirm every order was paid,
  every paid order shipped, and explain anything that doesn't line up — and
  remember what I told you last time so the work shrinks over time."

The product's job is to (a) eliminate the manual diff, (b) translate raw
diffs into operator-language insights, (c) **become measurably more accurate
at this specific account's data every time it runs**, and (d) **show the
user less data and more synthesis as it learns**.

---

## 3. High-level architecture

```
                   ┌──────────────────────────────────────────────┐
                   │  Agent orchestrator (LLM with tools)         │
                   │  - per-account loop                          │
                   │  - decides flow per job                      │
                   │  - asks user when ambiguous                  │
                   │  - emits rationales + TriageItems            │
                   └──────────────────────────────────────────────┘
                          │              │              │
              ┌───────────┘              │              └────────────┐
              ▼                          ▼                           ▼
   ┌────────────────────┐   ┌────────────────────────┐   ┌──────────────────────┐
   │ Semantic layer     │   │ Deterministic tools    │   │ Memory layer         │
   │ (global)           │   │ - read_table           │   │ (PER ACCOUNT)        │
   │ - Concept graph    │   │ - bind_columns         │   │ - Profile + notes    │
   │ - Curated aliases  │   │ - match_by_key         │   │ - Rules (incl. fees) │
   │                    │   │ - compare_amounts      │   │ - Decision log       │
   │                    │   │ - detect_fee_pattern   │   │ - Column embeddings  │
   │                    │   │ - apply_account_rules  │   │ - Learned aliases    │
   │                    │   │ - extract_from_text    │   │ - Observations       │
   │                    │   │ - propose_class.       │   │ - TriageItems        │
   │                    │   │ - ask_user             │   │ - Metric snapshots   │
   └────────────────────┘   └────────────────────────┘   └──────────────────────┘
                                       │
                                       ▼
                       ┌────────────────────────────────────┐
                       │ Per-job output (rationale-laden)   │
                       │ + emitted TriageItems              │
                       │ + insight-density snapshot         │
                       └────────────────────────────────────┘
                                       │
                                       ▼
                       ┌────────────────────────────────────┐
                       │ Frontend                           │
                       │ - Triage inbox (cross-job)         │
                       │ - Conversation surface (notes)     │
                       │ - Results page (responsive to      │
                       │   insight density)                 │
                       │ - Rules + observations             │
                       └────────────────────────────────────┘
```

Two processes, one machine. The agent orchestrator is the spine. The
*account* is the unit of memory and the unit of access.

---

## 4. The five load-bearing commitments

### 4.1 AI-native control flow

(Unchanged from v2.) The orchestrator decides per job; deterministic primitives
are callable tools. The LLM is on the critical path.

Tool surface (additions in **bold**):

| Tool | Purpose |
|---|---|
| `read_table(file)` | Robust ingest |
| `bind_columns(df, account)` | Bindings using ontology + **account-learned aliases** + value-shape heuristics |
| `match_by_key(a, b, binding)` | Exact + fuzzy key match |
| `compare_amounts(matched, tolerance, brand_rules)` | Raw $/% diffs |
| `detect_fee_pattern(diff, profile)` | Generic + account-custom fee shapes |
| `apply_account_rules(rows, account)` | Run learned rules before classification |
| `timing_stats(matched)` | Mean / σ / outliers |
| `propose_classification(row, evidence)` | LLM proposes (status, confidence, rationale) |
| `ask_user(question, choices, context)` | Pause and surface a question; resume on answer |
| **`extract_from_text(text, context, account)`** | Parse unstructured causal vocabulary (intake, notes, justifications) into proposed aliases / rules / brand facts |
| **`emit_triage_item(account, rationale, signature)`** | Persist a row as a cross-job TriageItem |

### 4.2 Institutional memory (account-scoped)

**Account is the unit of memory.** Each `Account` has its own namespace under
`data/accounts/{account_id}/`. The pilot generates an Account UUID on first
visit, stores it in localStorage, and sends it as an `X-Account-Id` header.
No login; the UUID is the access token. *Cross-account reads are forbidden in
the agent's tool surface.*

Per account, six stores:

1. **Profile** — display name, tolerances, custom fee rates, known source
   labels, time zone.
2. **Brand notes** — append-only `[{ at, text, parsed_proposals }]`. Onboarding
   intake answers and drop-in notes live here. Each entry's
   `parsed_proposals` is what `extract_from_text` produced.
3. **Rules** — active + pending rules. Each rule carries `user_origin_text` —
   the free-text justification (if any) that seeded it.
4. **Decision log** — append-only JSONL of every user disagreement with the
   system, including the free-text `user_reason`.
5. **Learned aliases + column embeddings** — confirmed `column_name →
   concept_id` bindings. Read **before** the global ontology in
   `bind_columns`.
6. **Observations** — agent-written durable notes the LLM can reference next
   time. "Stripe payouts arriving 2 days later on average since March 1."
7. **TriageItems** — see §4.3 below.

### 4.3 Human-in-the-loop, explainability, and the TriageItem

Every system output is a **proposal**. The unit the user acts on is the
`TriageItem` — not a row in a job, but a persistent thing in the account's
inbox.

```
TriageItem {
  id, account_id, created_at, last_seen_at,
  signature: str,           # stable hash of (concept_pair, value_pattern, side, fee_shape?)
  state: open | resolved | deferred | recurring,
  source_job_ids: [str],    # every job that produced this signature — recurring items accumulate
  rationale: Rationale,     # latest evidence
  resolution: { action, by, at, rule_id?, user_reason? }?
}
```

A reconciliation produces TriageItems by signature, **deduped against the
account's existing open items**. The same kind of recurring gap (e.g., the
weekly wholesale invoice that always lags by one day) accumulates
`source_job_ids` instead of cluttering the inbox six times.

The triage surface (cross-job, account-wide) lets the user:

| Action | What it teaches |
|---|---|
| `Mark expected` — with optional free-text `user_reason` | Decision log entry; if signature recurs N times, propose a rule |
| `Investigate` — defer | TriageItem.state = deferred; carries forward |
| `Add rule…` | Rule scaffolded from the signature + user_reason text → goes to pending; user reviews on `/rules` |
| `Override binding / classification` | Decision log + learned alias / pending rule |
| `Ask back` — drop-in question into the conversation surface | Appended to brand_notes; parsed by `extract_from_text` |

**Rationale schema (v3 addition: `user_reason`):**

```
Rationale {
  row_key: str
  status: enum
  confidence: float
  rationale: [Evidence]
  alternatives: [Alt]
  user_reason: str?     # free-text "why I disagreed" — set by HITL action
}
```

**Why the free-text field is load-bearing.** This is the single highest-signal
moment in the product — the user is in maximum context, and a one-line
justification compresses tribal knowledge that no structured form would
capture. `user_reason` becomes the `origin` of any rule the system later
proposes, replayable verbatim: "you told me on May 12 that Acme always
invoices for full PO even when partial-shipped."

### 4.4 Semantic / ontology layer

(Unchanged from v2 in shape.) The seeded concept graph stays curated. What
changes in v3:

- **Read order in `bind_columns`** is now explicit:
  1. Account-learned aliases (highest priority).
  2. Account column-embedding index (semantic match against past
     confirmations).
  3. Global ontology aliases (seeded).
  4. Value-shape heuristics.
- **New-concept proposals.** When the agent encounters a column that none of
  the above can bind with confidence > 0.3, it can *propose* a new concept
  (with proposed entity, role, and aliases). Proposals go into the account's
  brand notes for our (system maintainer) review — concept-graph edits are
  not autonomous, but the proposals accumulate as a roadmap input.

### 4.5 Insight density — the headline metric

**Definition.**

```
auto_handled  = rows whose final classification was decided by a rule,
                a fee pattern, or a high-confidence deterministic match,
                AND which did NOT surface as a TriageItem this job

needed_user   = TriageItems surfaced in the user's inbox this job

insight_density = auto_handled / (auto_handled + needed_user)
```

This is the fraction of work the system did silently. It should rise from
job 1 to job N as the account's memory grows.

**Counter-metrics, tracked alongside, equally visible:**

- `override_rate` = (auto_handled rows the user later corrected) / auto_handled.
  Rising override rate = the system is being too confident.
- `revocation_rate` = (rules accepted then later turned off) / rules_active.
  Rising revocation rate = the system is proposing rules too eagerly.
- `trust_adjusted_density = insight_density × (1 - override_rate)`.

**The single number on the dashboard is `trust_adjusted_density`**, not
`insight_density` alone. This is the metric that can't be juked.

**Where it's surfaced:**

- A persistent strip in the app header per account: "Insight density: 64%
  (trust-adjusted 61%) · 6 jobs". Click → trendline.
- After every job: "This run, the system handled 12 rows on its own (3 more
  than last time). 2 of last time's auto-handled rows were corrections —
  trust-adjusted density still rising."
- Always visible: **silent-action audit band** on the Results page — "12
  rows handled by 3 rules → audit." Non-collapsing. The user never has to
  look, but always can. This is the trust mechanism.

**What changes in the UI as density rises** (v3, explicit):

- High-confidence rationale rows collapse under "Looks fine — expand to
  audit."
- Rules that fired silently appear in the audit band, grouped by rule, with
  per-rule "show rows" expansion.
- Discrepancies needing attention are promoted to the top, with their
  `user_reason` text field already focused for fast capture.
- The triage inbox shrinks; the conversation surface grows.

The shape of the Results page literally changes as the account learns.

---

## 5. Component plan

### 5.1 Frontend

| Route | Purpose |
|---|---|
| `/onboarding` *(new)* | First-visit intake interview (3–4 free-text prompts); creates the Account; parses answers via `extract_from_text` into seed brand notes |
| `/` | Recon-type selector → drop → inferred bindings with confidence → submit |
| `/inbox` *(new)* | **Cross-job triage queue.** Persistent. Ranked by $ impact + recurrence + age |
| `/results/:job_id` | Per-job view, now responsive to insight density: audit band, collapsed rationales, promoted-to-top discrepancies |
| `/results/:job_id/compare/:prev_job_id` *(new)* | **"What changed since last month"** — diff between two jobs by TriageItem signature |
| `/conversation` *(new)* | Free-form drop-in notes + parsed extractions per account |
| `/rules` | Active + pending rules; revoke / accept / edit; each shows `user_origin_text` |
| `/observations` *(new, lightweight)* | Agent's durable notes about the account, time-stamped |

Shared header strip: insight density + trust-adjusted, persistent across
routes.

### 5.2 Backend layout

```
app/
├── main.py             — HTTP; X-Account-Id middleware
├── agent.py            — orchestrator; per-account loop
├── tools/              — read_table, bind_columns, match_by_key,
│                         compare_amounts, detect_fee_pattern,
│                         apply_account_rules, timing_stats,
│                         propose_classification, ask_user,
│                         extract_from_text, emit_triage_item
├── ontology/           — seeded concept graph (read-only at runtime)
├── memory/             — accounts.py, rules_store.py, decision_log.py,
│                         embedding_index.py, triage.py, observations.py,
│                         metrics.py    ← computes insight density on each job
├── report.py           — Excel export with rationale + user_reason columns
├── storage.py          — JobResult persistence + account directory layout
└── models.py           — Pydantic schemas
```

### 5.3 Account lifecycle (new)

1. **First visit** — frontend has no `X-Account-Id` → `POST /api/accounts` →
   server creates Account, returns UUID → frontend writes to localStorage.
2. **Onboarding** — `/onboarding` collects free-text answers, posts to
   `/api/accounts/me/notes` with kind=`intake`. Agent parses via
   `extract_from_text`; proposals go to the conversation surface for review.
3. **Subsequent visits** — header carries `X-Account-Id`; all reads/writes
   scoped to that account.
4. **Account export** — `GET /api/accounts/me/export` returns the full
   account JSON (notes, rules, decision log, triage items, observations).
   Users own their tribal knowledge and can take it with them.

### 5.4 Data flow per job (revised)

```python
def run_job(account_id, files):
    acc = memory.load_account(account_id)
    df_a, df_b = ingest(files)

    bind_a = bind_columns(df_a, account=acc)   # account aliases consulted first
    bind_b = bind_columns(df_b, account=acc)
    confirm_or_ask_low_confidence(bind_a, bind_b)

    matched, unmatched = match_by_key(df_a, df_b, bind_a, bind_b)
    matched = compare_amounts(matched, acc.profile.tolerances)
    matched = apply_account_rules(matched, acc.rules)

    rationales = [
        deterministic_rationale(row) if not row.needs_llm
        else propose_classification(row, acc)
        for row in matched
    ]

    triage_items = []
    for r in rationales + unmatched:
        if r.classification == "match" or r.matched_by_rule:
            continue
        sig = signature(r)
        existing = memory.triage.find_open(acc.id, sig)
        if existing:
            existing.add_source(job_id)
            triage_items.append(existing)
        else:
            ti = memory.triage.create(acc.id, sig, r)
            triage_items.append(ti)

    metrics = compute_insight_density(rationales, triage_items, acc)
    memory.metrics.snapshot(acc.id, job_id, metrics)

    return JobResult(rationales=rationales, triage_items=triage_items,
                     bindings=bind_a+bind_b, metrics=metrics,
                     insights=synthesize(rationales, triage_items, acc))
```

Three things to notice:

1. The job no longer "owns" discrepancies — it emits TriageItems into the
   account's inbox.
2. `compute_insight_density` runs per job, snapshotted to `metrics/` for the
   trendline.
3. The agent reads the account everywhere.

### 5.5 Memory layer (revised directory layout)

```
data/
├── accounts/{account_id}/
│   ├── profile.json
│   ├── notes.jsonl          — drop-in notes + onboarding intake
│   ├── rules.json           — active + pending; each has user_origin_text
│   ├── decisions.jsonl      — append-only; each has user_reason
│   ├── learned_aliases.json — column_name → concept_id (per account)
│   ├── embeddings.parquet   — semantic memory of past confirmations
│   ├── observations.jsonl   — agent-written durable notes
│   ├── triage.json          — current TriageItem set + history
│   └── metrics.jsonl        — per-job insight-density snapshots
└── jobs/{job_id}.json       — per-job payload, references account_id
```

---

## 6. Key design decisions (v3)

### 6.1 Account is a first-class entity, in Phase 1

| Considered | Chosen | Why |
|---|---|---|
| Single global brand_id placeholder (v2) | UUID Account, created on first visit, scoped everywhere | Tribal knowledge is the differentiating value prop. The moment two real users share one machine, unscoped memory cross-contaminates. Pushing this into Phase 4 means a forced migration of every memory write made before that point. |

### 6.2 TriageItem is a first-class cross-job entity

| Considered | Chosen | Why |
|---|---|---|
| Per-job discrepancy list | Persistent TriageItem keyed by signature | Operators don't reconcile in isolation — they reconcile *against last time*. A recurring gap is one item with N source_jobs, not N copies cluttering the inbox. Without this, the "less data, more insight" experience cannot exist. |

### 6.3 Free-text causal vocabulary has explicit schema homes

| Considered | Chosen | Why |
|---|---|---|
| Rule DSL only | `Rationale.user_reason`, `Rule.user_origin_text`, `Account.notes` | Operators encode tribal knowledge as fragmentary stories, not as predicates. Forcing structure first loses the signal. We can always parse later; we can't recover what we never asked for. |

### 6.4 Insight density + counter-metrics, from day one

| Considered | Chosen | Why |
|---|---|---|
| One metric — insight density | `trust_adjusted_density = density × (1 − override_rate)` as headline; `revocation_rate` and silent-action audit band as additional pressure | A single metric is gameable: the system could auto-classify everything and brag about 95% density while quietly being wrong. Triangulating with override rate and rule revocations forces the system to be both confident *and* correct. |

### 6.5 LLM on critical path (unchanged from v2)

### 6.6 Ontology stays curated; aliases learn per account (refined from v2)

The seeded concept graph is global and read-only at runtime. **Aliases learn
per account.** Cross-account alias propagation is explicitly out of scope —
one user's tribal knowledge is not another user's.

### 6.7 HITL is mandatory at every decision (unchanged from v2)

### 6.8 Agent orchestrator, not pipeline (unchanged from v2)

### 6.9 Concept-graph extensions are proposals, not autonomous

| Considered | Chosen | Why |
|---|---|---|
| Agent autonomously adds concepts | Proposals only; we (maintainers) review | Letting any account autonomously add concepts to the global graph breaks the shared-vocabulary premise. The agent can propose; humans (us, for now) curate. Per-account *aliases* learn freely; the *graph* doesn't. |

### 6.10 Surviving v2 decisions

- File-based ingest (universal across systems).
- Synchronous reconciliation for files ≤10MB (no queue yet).
- JSON-on-disk for jobs (no Postgres yet).
- UUID job IDs + account IDs as access tokens (no login yet).
- Single-process, single-machine pilot scope.
- Pilot still ships without a full pytest suite; the decision log + replay
  is the regression suite.

---

## 7. Data contracts

```
Concept                     SemanticBinding              Rule
───────────────────         ─────────────────            ─────────────────────
id: "order.gross_total"     column_name: str             id: str
type: money|string|...      concept_id: str              account_id: str
role: primary_amount|...    confidence: float            type: enum
aliases: [str]              provenance: enum             when: predicate (DSL)
invariants: [str]           evidence: [str]              then: action (DSL)
                            alternatives: [...]          origin: str
                                                         user_origin_text: str?  ← v3
                                                         confidence: float
                                                         state: active|pending|revoked

Rationale                   TriageItem                   Account                  Note
─────────────────           ──────────────────           ─────────────────        ─────────────
row_key: str                id: str                      id: str (UUID)           at: datetime
status: enum                account_id: str              display_name: str        text: str
confidence: float           created_at, last_seen_at     created_at               kind: intake|note
rationale: [Evidence]       signature: str               profile: {...}           parsed_proposals
alternatives: [Alt]         state: open|resolved|...     time_zone: str
user_reason: str?  ← v3     source_job_ids: [str]
                            rationale: Rationale
                            resolution: {...}?

JobResult                   AccountMetrics
─────────────────────       ─────────────────────────
job_id, account_id          job_id, at
created_at                  auto_handled, needed_user
bindings: [SemanticBinding] insight_density
matched_rationales: [...]   override_rate
triage_items_emitted: [str] revocation_rate
unmatched_a, unmatched_b    trust_adjusted_density
timing: {...}?
insights: str
citations: {claim → rows}
metrics: AccountMetrics
```

---

## 8. Error handling

(Unchanged from v2 principles.) Adds:

- Missing `X-Account-Id` → `401 "Account not initialized; create one at POST /api/accounts."`
- Account UUID not found → `404`.
- `extract_from_text` parse failure → graceful: text is stored as raw note,
  no proposals emitted, user can re-trigger parsing.

---

## 9. Performance budget

| Stage | Target |
|---|---|
| File read + preview | <500ms |
| Bind columns (incl. account lookup) | <1s |
| Match + compare | <3s for 10MB |
| Apply account rules | <100ms (≤200 rules per account) |
| LLM judgments | <8s (≤50 calls/job, batched where possible) |
| TriageItem dedupe (by signature) | <200ms |
| Insight-density compute + snapshot | <50ms |
| Excel build | <2s |
| **End-to-end** | <15s typical, <30s when many LLM calls |

If LLM cost grows: first widen the deterministic-confidence threshold; then
cache propose_classification by `(signature, rationale-shape hash)`.

---

## 10. What's still punted

- **Multi-user-per-account.** A user can only have one account; accounts are
  single-user for the pilot.
- **Authentication.** UUID account IDs only.
- **Direct system integrations** (Shopify / Stripe OAuth).
- **Multi-file (3+ source) reconciliation.**
- **Scheduled / recurring runs.**
- **Concept-graph editing UI** (we curate; users propose via brand notes).
- **Cross-account learning** — explicitly out of scope; each account's
  tribal knowledge stays its own.
- **PII / SOC2 posture** — explicit policy required before exposing beyond
  pilot users.

---

## 11. Definition of done for the pilot

The pilot is "done enough to validate" when a target user can, **without
help**:

1. Land on the app, see an onboarding intake (3–4 prompts), and have their
   answers parsed into a starter brand profile.
2. Drop two CSVs → bindings inferred and pre-filled with confidence pills.
3. Reconcile → results in <15s with rationale per row.
4. For each discrepancy, see a plain-English rationale and a free-text
   `user_reason` capture surface.
5. Mark a discrepancy as expected with a one-line reason → the next time the
   same signature appears, it does not surface again (and the rule's
   `user_origin_text` replays the original justification).
6. View the cross-job **triage inbox** and see items deferred from prior
   runs, plus a "what changed since last month" view.
7. Watch **insight density rise and override rate stay low** across at least
   three jobs — visible as a persistent strip in the header.
8. Drop a free-text note ("we just switched to net-45 with Acme") and see
   the agent extract a proposed rule for review.
9. Visit `/rules` and `/observations` to see what the system has learned
   about their account, in their own words where applicable.
10. Download an Excel report with rationale + user_reason columns.
11. Visit again the next day, hit the same Account UUID (via localStorage),
    and see everything intact.

Steps 5, 6, 7, and 8 are the differentiating behavior — the parts a
pure-deterministic system cannot produce.

Step 7 is the headline. **If insight density isn't rising with low override
rate, the product isn't working — regardless of what any individual
reconciliation looks like.**
