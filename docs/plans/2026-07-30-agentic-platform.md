# From ReconOps to a Generic Agentic Data-Operations Platform

> **Status:** design document (not yet executed). Companion prototype:
> [`prototype/index.html`](../../prototype/index.html) — an AI-native UI
> demonstration deployable as a static site (Vercel).
>
> This document answers four questions:
> 1. What has to change for the flow to be *truly agentic* (§2–§3)?
> 2. How does the product become *generic* — usable beyond e-commerce
>    reconciliation (§4)?
> 3. How does the system *learn, adapt, and derive context* — and how does
>    the ontology become a *reusable, derivable layer per domain* (§5–§7)?
> 4. What is the migration path from today's codebase, and how is it
>    deployed and shared (§8–§10)?

---

## 1. Honest gap analysis: what we have vs. "truly agentic"

PLAN.md v3 already *commits* to an agent orchestrator, ontology, and memory.
The implementation delivered the memory and ontology layers, but the
"orchestrator" is not agentic. Being precise about the gap matters, because
several pieces we need already exist and should not be rebuilt.

| Dimension | Today (implemented) | Truly agentic (target) |
|---|---|---|
| **Control flow** | `backend/app/agent.py` runs a fixed 9-step pipeline (ingest → bind → match → compare → classify → capped LLM review → insights). The sequence never varies. | An LLM planner receives a *goal*, inspects available tools + memory + data profile, and decides the sequence at runtime. The pipeline becomes one macro-tool the planner *may* call. |
| **LLM role** | Three bounded advisory spots: batched second opinions (can never flip a verdict), insight prose, `extract_from_text` for notes. | The LLM is the spine: it plans, selects tools, asks the user when ambiguous, writes observations, and proposes ontology/rule changes. Deterministic tools remain the only way numbers are computed. |
| **Ontology** | One hand-curated `backend/app/ontology/concepts.yaml`, e-commerce-only, read-only at runtime. Per-account learned aliases layer on top. | Three-tier ontology (core / domain pack / account overlay) that the system can *derive* from data + conversation, version, and export as a reusable **domain pack**. |
| **Context** | Context = whatever the fixed pipeline passes to each function. Account memory is consulted only at hard-coded points (`bind_columns`, rules). | Context is *assembled per task*: intent (conversation) + data profile (schema fingerprint) + account memory + domain ontology + connection metadata, with a budget and a relevance ranking. |
| **Learning** | Aliases, rules, decisions, observations, triage — all per account, all consumed by fixed code paths. Metric: trust-adjusted insight density. | Same stores, plus **procedural memory** (successful plans distilled into playbooks) and **preference memory** (how this user wants results). Metric generalizes to an autonomy score. |
| **UI** | Fixed routes and workflows: upload → results → inbox → rules → metrics. The user navigates the app. | One conversational surface. The agent composes the UI per response (generative UI blocks: tables, triage cards, charts, proposals). The app navigates to the user. |
| **Product scope** | "Reconcile two e-commerce exports." | "Operational data agent" for any tabular-data domain; reconciliation is the first *playbook* of the first *domain pack*. |

**What we keep unchanged (these are assets, not debt):**

- Every deterministic tool in `backend/app/tools/` (ingest, binding,
  matching, amounts, classify, timing, extract). They become the agent's
  tool registry.
- The entire memory layer (`backend/app/memory/`) and its account scoping.
- Verdicts stay deterministic and auditable. **The agent never computes
  money.** It orchestrates, explains, proposes; tools decide.
- The trust machinery: TriageItems, silent-action audit band, counter-metrics.
- The eval/replay harness (`backend/app/eval.py`, `replay.py`) — extended
  to replay agent runs, not just pipeline runs.
- Auth, orgs, Postgres/object-storage work, deployment runbook.

---

## 2. Target architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  CONVERSATIONAL SURFACE (generative UI)                             │
│  goal in → streamed plan, tool events, questions, UI blocks out     │
└──────────────────────────────┬──────────────────────────────────────┘
                               │  SSE / WebSocket
┌──────────────────────────────▼──────────────────────────────────────┐
│  AGENT RUNTIME  (backend/app/agent/ package)                        │
│  ┌────────────┐  ┌───────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │ Planner    │→ │ Executor  │→ │ Critic/      │→ │ Learner      │  │
│  │ goal→plan  │  │ tool loop │  │ verifier     │  │ distills     │  │
│  │            │  │           │  │ (determin.)  │  │ playbooks    │  │
│  └────────────┘  └───────────┘  └──────────────┘  └──────────────┘  │
│  Budgets: $ / tool-calls / wall-clock · HITL gates · event stream   │
└───────┬──────────────────┬──────────────────┬───────────────────────┘
        │                  │                  │
┌───────▼───────┐  ┌───────▼────────┐  ┌──────▼───────────────────────┐
│ TOOL REGISTRY │  │ ONTOLOGY LAYER │  │ MEMORY LAYER (per account)   │
│ typed, self-  │  │ 3 tiers:       │  │ episodic: decisions.jsonl    │
│ describing    │  │  core          │  │ semantic: observations,      │
│ existing      │  │  domain pack   │  │   learned aliases, embeddings│
│ tools/* +     │  │  account       │  │ procedural: playbooks (NEW)  │
│ pack tools    │  │   overlay      │  │ preferences (NEW)            │
└───────────────┘  └────────────────┘  └──────────────────────────────┘
        │                  │
┌───────▼──────────────────▼──────────────────────────────────────────┐
│ CONTEXT DERIVATION SERVICE                                          │
│ data profiler (schema fingerprint) · intent parser · assembler      │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.1 The agent runtime

Replace the fixed pipeline with a **plan–execute–verify–learn** loop built
on the Claude API tool-use loop (or the Claude Agent SDK):

- **Planner.** Input: user goal (free text or an event like "two files
  dropped"), account memory digest, data profile, available tools +
  playbooks. Output: a plan — an ordered list of steps with expected
  outcomes, surfaced to the user *before and during* execution.
- **Executor.** Standard tool-use loop. Every tool call and result is an
  event on the stream (this is the audit band, generalized).
- **Critic/verifier.** Deterministic post-conditions per tool (e.g. "match
  rate reported = rows matched / rows in", "no verdict was produced by the
  LLM", ontology invariants from `concepts.yaml`). A failed check aborts
  or re-plans — it is never silently swallowed.
- **Learner.** After a run the agent writes observations (exists today) and
  — new — distills a successful multi-step run into a **playbook**
  (§6.3) and proposes ontology/rule updates through the existing proposal
  machinery.

**Autonomy is budgeted, not binary.** Each account has an autonomy level
(the UI exposes it as a dial):

| Level | Behavior |
|---|---|
| `observe` | Agent proposes every step; user approves each. Day-one default for a new domain. |
| `assist` | Agent executes read-only tools freely, pauses at any write/decision (rule creation, triage resolution, export). |
| `auto` | Agent runs end-to-end within budget; all writes still land as proposals unless covered by an accepted rule/playbook. Earned, per playbook, when trust-adjusted density for that playbook stays above threshold. |

This generalizes the existing insight-density mechanic: autonomy is
*earned per capability*, and the same counter-metrics (override rate,
revocation rate) demote a playbook back to `assist` automatically.

### 2.2 The old pipeline becomes a macro-tool

`run_reconciliation(file_a, file_b, config)` — today's `agent.py` flow —
stays available as a single registered tool. The planner calls it when the
goal is exactly the classic recon ("these two files, match them"). This
gives us:

- Zero regression risk during migration: the deterministic path is intact.
- A latency floor: the classic case stays <15s because the planner makes
  one decision, not thirty.
- An honest ablation: eval can compare planner-composed runs against the
  macro-tool on the same inputs.

---

## 3. What "truly agentic flow" means concretely — five changes

1. **Goal-directed entry, not form-directed.** The entry point is a
   conversation ("did everything we shipped in June get paid?"), a file
   drop, or a connection event (Shopify sync landed) — all normalized into
   a *goal* object: `{intent, entities, artifacts, constraints}`. The
   upload form survives as one affordance among several, not the front door.

2. **Runtime tool selection.** The planner sees the tool registry
   (JSON-schema descriptors generated from the existing functions in
   `tools/`) and composes them. New behavior emerges without new routes:
   "compare this month against last month, ignoring the wholesale
   invoices" is a plan (load two jobs → diff by signature → filter by
   learned rule), not a feature request.

3. **Clarification as a first-class state.** `ask_user` (already specced
   in PLAN.md §4.1, never implemented) becomes real: the run suspends,
   persists (the async-job work from `docs/plans/2026-07-19` provides the
   substrate), the question lands in the conversation surface, and the run
   resumes on answer. Ambiguity stops being a warning banner and becomes a
   dialogue.

4. **Every side effect is a proposal with provenance.** Already the house
   style (rules, triage) — extended to *everything* the agent does:
   ontology edits, playbook creation, preference changes all flow through
   propose → preview blast radius → accept/revoke. The existing
   `user_origin_text` / `user_reason` fields carry over unchanged.

5. **The UI is composed, not navigated.** The agent's response is a typed
   sequence of UI blocks (§7). The frontend renders blocks; it does not
   encode workflows. Fixed routes shrink to: the surface, and deep links
   into artifacts it produced.

---

## 4. Becoming a generic product: domain packs

**Reframing:** the product is an *operational data agent* — "give it the
data exhaust of your operation and it learns to answer, check, and watch
the things you care about." E-commerce reconciliation becomes the first
**domain pack**, not the product.

A domain pack is a versioned, installable bundle:

```
packs/ecommerce-recon/
├── pack.yaml            # id, version, display name, provenance
├── ontology.yaml        # domain concepts (today's concepts.yaml moves here)
├── tools/               # optional pack-specific tools (e.g. fee shapes)
├── playbooks/           # seeded procedures (classic recon, month-diff)
├── invariants.py        # domain invariants for the critic
└── eval/                # golden inputs + expected outputs (CI-runnable)
```

Rules for genericity, learned from this codebase:

- **Core stays tiny.** Runtime, memory, context derivation, tool loop, and
  the *core ontology tier* (§5.1) know nothing about e-commerce. The
  litmus test: a second pack (candidates: SaaS billing recon
  Stripe↔ledger; logistics claims 3PL↔carrier invoices) installs with
  zero core changes. Shipping pack #2 is the acceptance test of the whole
  phase (§9, Phase E).
- **Packs are data + declarations, minimally code.** Ontology, playbooks,
  invariants, eval sets are declarative; pack tools are the escape hatch.
- **Accounts subscribe to packs.** An account's ontology view = core ∪
  subscribed packs ∪ its own overlay. Cross-account isolation is
  preserved exactly as today (PLAN.md §6.6): overlays never leak; packs
  are the *only* sanctioned reuse channel, and they move through explicit
  curation (§5.3), never automatic propagation.

---

## 5. The ontology layer: derived, versioned, reusable per domain

### 5.1 Three tiers

| Tier | Contents | Mutability | Lives in |
|---|---|---|---|
| **Core** | Universal concepts: `identifier`, `money.amount`, `datetime`, `quantity`, `party`, `document`, `line_item`, plus roles (`primary_key`, `primary_amount`, `event_time`) and base invariants (`money has currency`, `identifier is join-safe`). | Curated by us, rarely changes | `backend/app/ontology/core.yaml` |
| **Domain pack** | Domain concepts extending core (`order.gross_total` *is-a* `money.amount`), aliases, value hints, fee shapes, relations (`payment settles order`). | Versioned; changes ship as pack releases | `packs/<pack>/ontology.yaml` |
| **Account overlay** | Learned aliases, column embeddings, account-proposed concepts pending promotion. | Learns continuously (already implemented) | `memory/learned_aliases.py`, `embedding_index.py` |

Resolution order in `bind_columns` is unchanged in spirit — overlay →
embeddings → pack aliases → core → value-shape heuristics — it just gains
one tier. The existing `Concept` dataclass (`ontology/__init__.py:37`)
gains `parent: Optional[str]` (is-a edge to core) and `relations:
tuple[Relation, ...]`; everything else survives.

### 5.2 Ontology induction — deriving the layer instead of hand-writing it

The pipeline that lets a *new domain* bootstrap its ontology from data:

1. **Profile.** On any upload/connection, the data profiler
   (`backend/app/context/profiler.py`, new) computes a schema fingerprint
   per column: name n-grams, dtype, null rate, cardinality, value-shape
   stats (regex sketch, numeric range, datetime-ness — reusing the
   `ValueHints` machinery that already exists).
2. **Match.** Fingerprints are matched against core + subscribed packs
   (alias index + embedding similarity — `embedding_index.py` already does
   the per-account version of this).
3. **Propose.** Unmatched columns go to the LLM with the profile + sample
   values + conversation context: *propose a concept* — id, parent core
   concept, entity, role, aliases, invariants. This is PLAN.md §4.4's
   "new-concept proposals," upgraded from a roadmap note to the induction
   mechanism.
4. **Confirm.** Proposals surface in the conversation as reviewable cards
   (generative UI). A confirmation writes to the *account overlay* and
   records provenance (`derived_from: {job_id, column, user_confirmation}`).
5. **Consolidate.** Overlay concepts confirmed across N jobs (or explicitly
   promoted by the user) become **pack candidates**.

### 5.3 Promotion and reuse: account → pack → ecosystem

```
account overlay ──(N confirmations / explicit promote)──► pack candidate
pack candidate ──(curator review + eval-set addition)───► pack release vN+1
pack release   ──(export)───────────────────────────────► shareable pack
```

- Promotion is *always* human-gated (preserves PLAN.md §6.9: the graph is
  curated, proposals are not autonomous). What changes: curation gets a
  queue and a diff UI instead of "we read brand notes."
- Packs are exportable/importable JSON+YAML bundles → an org that tuned a
  logistics pack can reuse it across its accounts, and eventually packs
  become a marketplace surface. Account overlays are explicitly *not*
  exportable to other accounts — tribal knowledge stays owned (mirrors
  the existing account-export contract, PLAN.md §5.3.4).
- Every pack release carries its eval set; CI runs pack evals exactly like
  `app.eval` runs today. An ontology change that flips golden verdicts
  fails the release.

### 5.4 Ontology as runtime contract, not just a mapping aid

Today the ontology only helps bind columns. In the target architecture it
is also:

- **The critic's rulebook.** Invariants (`ontology/invariants.py` exists,
  underused) become machine-checked post-conditions on tool outputs.
- **The planner's vocabulary.** Plans reference concepts, not column names
  — which is what makes playbooks (§6.3) transferable across accounts
  whose files name the same concept differently.
- **The explanation language.** Rationales already speak concept language;
  this stays.

---

## 6. Learning and adapting to the user

### 6.1 Memory taxonomy (mapping to existing stores)

| Memory type | Contents | Store | Status |
|---|---|---|---|
| **Episodic** | Every decision + `user_reason`; every agent run trace | `decisions.jsonl` (exists) + `runs/` traces (new) | extend |
| **Semantic** | Observations, learned aliases, embeddings, overlay concepts | `observations.jsonl`, `learned_aliases.json`, `embeddings` (all exist) | keep |
| **Procedural** | Playbooks — distilled successful plans | `playbooks.json` (new) | build |
| **Preference** | Output/interaction preferences: report format, materiality phrasing, "always show wholesale separately", channel/cadence | `preferences.json` (new) | build |

### 6.2 The learning loop (one loop, five inputs)

`observe → propose → confirm → apply → measure` — already the shape of the
rules engine; generalized to all memory types:

- **Observe:** run traces, corrections (`user_reason`), repeated triage
  resolutions, free-text notes, *and interaction telemetry* (which blocks
  the user expands, which columns they always re-sort — preference signal).
- **Propose:** rule proposals (exists), concept proposals (§5.2), playbook
  proposals (§6.3), preference proposals ("you've exported Excel after all
  6 runs — attach it automatically?").
- **Confirm:** one consistent proposal-card UX, always with blast-radius
  preview (the rules engine's preview mechanic, generalized).
- **Apply:** each memory type has exactly one write path; all writes carry
  provenance.
- **Measure:** §6.4.

### 6.3 Playbooks — procedural memory

A playbook is a distilled, parameterized plan:

```
Playbook {
  id, account_id | pack_id,       # account-learned or pack-seeded
  name: "Month-end orders vs payments",
  trigger: {intent_patterns, artifact_shapes},   # what goals it matches
  steps: [{tool, args_template, expected}],      # concept-typed args
  provenance: {distilled_from_run_ids, accepted_by, at},
  stats: {runs, success_rate, override_rate},
  autonomy: observe | assist | auto
}
```

- Distilled by the Learner from ≥2 similar successful runs; lands as a
  proposal.
- Because step args are concept-typed (§5.4), a pack-seeded playbook works
  on any account's column names.
- Playbooks are the unit of earned autonomy (§2.1) and the unit of
  scheduling ("run this on the 1st of each month" — the async-job
  substrate makes this a cron over an existing capability).

### 6.4 Metrics: insight density → autonomy score

Keep the existing headline discipline, one level up:

```
autonomy_score(account | playbook) =
    auto_handled_work / total_work × (1 − override_rate)
```

- Per playbook and per account; same counter-metrics (override rate,
  revocation rate) and the same non-collapsing audit band. Rising
  override rate automatically demotes autonomy (§2.1).
- New counter-metric for the planner era: **plan-abort rate** (runs
  aborted by the critic / total runs) — a rising value means the planner
  is overreaching.

---

## 7. AI-native UX (what the prototype demonstrates)

Design principles — each is visible in `prototype/index.html`:

1. **One surface.** A conversation stream is the app. Files drop onto it,
   questions type into it, results render inside it. Inbox/rules/metrics
   become *views the agent composes on request*, not routes to visit.
2. **Generative UI blocks.** Agent responses are typed blocks the frontend
   knows how to render: `plan`, `tool_run`, `binding_card`,
   `recon_summary`, `triage_card`, `proposal_card` (rule / concept /
   playbook), `metric_tile`, `table`, `question`. The agent chooses which
   blocks, in what order — less data, more synthesis, per account
   maturity (the PLAN.md §4.5 behavior, now the rendering model itself).
3. **The plan is visible and interruptible.** Before running, the agent
   shows its plan; during, each tool call streams as a compact event line
   (the audit band generalized); the user can pause, redirect, or take
   over at any step.
4. **Trust chrome is ambient.** Autonomy dial (observe/assist/auto),
   trust-adjusted score, and a "what I know about you" memory panel
   (observations, rules, playbooks, preferences — each revocable in place)
   are persistently one click away. Memory transparency *is* the trust
   story.
5. **Questions come to you.** Clarifications and proposals appear inline in
   the stream and in a small attention tray; nothing blocks silently.
6. **Escape hatches everywhere.** Every generated block has "show the
   rows" / "show why" (rationale + provenance) — the non-collapsing audit
   principle carried into generative UI.

---

## 8. Migration path from the current codebase

Ordered so every phase ships working software and nothing regresses.

### Phase A — Agent runtime beside the pipeline (foundation)

- New package `backend/app/agent_runtime/` (`runtime.py`, `planner.py`,
  `events.py`, `budget.py`, `critic.py`); existing `agent.py` untouched
  and registered as macro-tool `run_reconciliation`.
- Tool registry: JSON-schema descriptors for `tools/ingest.py`,
  `binding.py`, `matching.py`, `amounts.py`, `classify.py`, `timing.py`,
  `extract.py` + memory read tools (account digest, open triage, rules).
- `POST /api/agent/runs` (+ SSE `GET /api/agent/runs/{id}/events`) in
  `main.py`, feature-flagged (`RECONOPS_AGENT_RUNTIME=1`).
- Run traces persisted per account (episodic memory extension).
- Eval gate: classic-recon goals through the planner must match macro-tool
  outputs on the golden set.

### Phase B — Ontology tiers + induction

- Split `concepts.yaml` → `ontology/core.yaml` + `packs/ecommerce-recon/
  ontology.yaml`; add `parent`/`relations` to `Concept`; resolution order
  gains the pack tier.
- `backend/app/context/profiler.py` (schema fingerprints) + induction
  endpoints (propose/confirm concept) + provenance on overlay writes.
- Promotion queue (account overlay → pack candidate) with diff preview.
- Pack loader with per-account subscriptions; pack eval in CI.

### Phase C — Memory upgrade

- `memory/playbooks.py`, `memory/preferences.py`; Learner distillation;
  proposal cards; autonomy levels + demotion wiring; autonomy-score
  metrics beside existing density metrics.

### Phase D — Conversational surface + generative UI

- Frontend: conversation stream page rendering the typed block set (§7.2);
  existing pages remain as agent-composable views; attention tray; memory
  panel; autonomy dial. Old routes retire only when the surface covers
  them.

### Phase E — Genericity proof

- Second domain pack (SaaS billing recon: Stripe payouts ↔ ledger) built
  *only* with pack primitives — zero core edits allowed. Bootstrap its
  ontology via §5.2 induction on real exports. This phase is the
  acceptance test; if it needs core changes, the abstraction is wrong and
  we fix core, not the pack.

**Dependencies:** A → B → C can interleave; D needs A (events) and benefits
from C; E needs B. The Postgres/object-storage and async-job phases
already planned (`docs/plans/2026-07-19`, `2026-07-20`) are prerequisites
for suspended runs (`ask_user`) and scheduled playbooks, and continue
unchanged.

---

## 9. Guardrails and risks

| Risk | Guardrail |
|---|---|
| Agent hallucinating numbers | LLM never computes; only tool outputs enter results. Critic checks per-tool post-conditions + ontology invariants. Already house law — must survive the refactor. |
| Cost blowup from planning loops | Per-run budgets ($ / tool calls / wall-clock); the macro-tool fast path for classic goals; playbooks skip planning for known goals (plan once, reuse many). |
| Non-reproducibility | Every run trace persisted; `replay.py` extended to re-execute traces with pinned tool versions; golden-set eval on both planner and macro-tool paths in CI. |
| Ontology drift / pack rot | Human-gated promotion, versioned packs, pack eval sets in CI, revocation → automatic demotion. |
| Autonomy overreach | Earned per-playbook autonomy, override-rate demotion, plan-abort-rate counter-metric, non-collapsing audit band. |
| Privacy / cross-account leakage | Overlays never propagate; packs are the only reuse channel and are curated; account export contract unchanged; runtime tool surface remains account-scoped (no cross-account reads, as today). |

---

## 10. Deployment & sharing topology

Two distinct things ship publicly:

1. **The prototype (now).** `prototype/index.html` is a self-contained
   static page (no build step, no backend, simulated agent) with
   `prototype/vercel.json`. Deploys to Vercel in one step and is safe to
   share publicly — it contains only fabricated demo data.
2. **The product (target).** Vercel hosts the static frontend well, but
   the backend is FastAPI + Postgres + object storage + long-lived
   SSE/background jobs — not a fit for Vercel's serverless functions.
   Target topology:
   - **Frontend:** Vercel (static build of `frontend/`), `VITE_API_BASE`
     pointing at the API host.
   - **Backend:** the existing container on a long-running host —
     current single-VPS runbook (`docs/DEPLOY.md`) or a managed
     container platform (Fly.io / Railway / Render) when we outgrow it.
   - **Postgres:** managed (e.g. Neon) per the Phase 2.2 plan; object
     storage per the existing S3 abstraction (`storage_s3.py`).
   - CORS + cookie config already centralizes in `config.py`; a
     cross-origin frontend needs `SameSite=None; Secure` session cookies
     and an allowed-origins entry — one config change, no code change.

---

## 11. Definition of done (per the PLAN.md tradition)

The transformation is validated when, without help, a user can:

1. Open one surface, state a goal in their own words (or drop files), and
   watch a visible plan execute with tool-level transparency.
2. Be asked a clarifying question mid-run, answer it, and see the run
   resume.
3. See every side effect land as a proposal with provenance and a
   blast-radius preview; accept or revoke in place.
4. Watch the system distill a repeated task into a playbook, approve it,
   schedule it, and see its autonomy rise — and fall when they override it.
5. Watch an unknown column become a proposed concept, confirm it, and see
   it bind automatically next time.
6. Promote matured account concepts toward a domain pack; export a pack;
   install it on a second account and see it work on differently-named
   columns.
7. Stand up a **second domain** end-to-end (Phase E) with zero core code
   changes — the genericity proof.
8. Audit everything: run traces, memory panel, non-collapsing audit band —
   and see the autonomy score with its counter-metrics stay honest.
