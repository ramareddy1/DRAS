# Agent Runtime — Technical Architecture

> **Status:** design spec (not yet executed). Companion to
> [`docs/plans/2026-07-30-agentic-platform.md`](../plans/2026-07-30-agentic-platform.md),
> which states *what* the product becomes. This document states *how* — the
> module boundaries, data model, and wire contracts that the phased plan
> builds against.
>
> UI reference: [`prototype/index.html`](../../prototype/index.html).
> Deployed at https://reckon-prototype.vercel.app

The plan doc is directionally right and its keep-list is correct. This spec
fixes the seams it leaves open, corrects six places where it under-specifies
what the code actually does (§5), and records four decisions that everything
else depends on.

---

## 0. Decisions

| # | Decision | Consequence |
|---|---|---|
| 1 | **The agent run is *the* execution primitive.** One `runs` + `run_events` pair in Postgres, in-process worker, no broker. | `docs/plans/2026-07-19-async-job-execution.md` is **absorbed, not executed** — today's reconciliation job becomes a run whose plan is one macro-tool call. Two run concepts would mean two state machines, two audit trails, two replay paths. |
| 2 | **The prototype's shell is the only shell.** Three panes, no nav bar. | Existing pages (`InboxPage`, `RulesPage`, `MetricsPage`, `ResultsPage`) keep working but render *inside* the centre pane, reached by the agent composing a link or by deep link. §7.1 of the plan taken literally. |
| 3 | **The prototype is a structural and behavioural spec, not a visual one.** Current Tailwind styling stays. | The prototype's value is the interaction model — blocks, visible plans, memory rail, autonomy dial — not its palette. Do not port the paper/serif design system. |
| 4 | **Own the loop via the Anthropic SDK Tool Runner** (`client.beta.messages.tool_runner`). | Not LangChain, not LangGraph, not the Claude Agent SDK, not Managed Agents. Rationale in §2.6. |

---

## 1. Data model

### 1.1 The spine

Two tables. Nothing else in this spec works if these are wrong.

**`runs`** — one row per goal.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid | |
| `account_id` | uuid FK, indexed | scoping preserved exactly as today |
| `goal` | jsonb | `{intent, entities, artifacts, constraints}` (plan §3.1) |
| `status` | enum | `pending │ running │ suspended │ done │ failed │ aborted` |
| `autonomy` | enum | `observe │ assist │ auto` — resolved at start, consulted at each gate |
| `playbook_id` | uuid, null | which playbook drove this, if any |
| `budget` | jsonb | `{usd_cap, tool_call_cap, wall_clock_s, task_budget_tokens}` |
| `spend` | jsonb | running totals; the enforcement counter |
| `suspended_on` | bigint FK → `run_events.id`, null | the unanswered question |
| `transcript` | jsonb | the mirrored `messages` array — see §1.2 |
| `created_at` / `ended_at` / `error` | | |

`suspended` + `suspended_on` **is** the `ask_user` mechanism (plan §3.3). A run
parks in Postgres; the answer arrives on a different request, possibly in a
different process; the loop resumes from `transcript`.

**`run_events`** — one append-only log, four consumers: SSE tail, audit band,
`replay.py`, eval diff.

`id` (bigserial), `run_id`, `account_id`, `seq`, `type`, `payload` jsonb, `at`.

The bigserial is load-bearing — SSE reconnect uses `Last-Event-ID` against it,
so it must be a total order.

The type enum is the backend↔frontend contract:

```
goal_received · plan_proposed · plan_approved · step_started · step_completed
tool_called · tool_returned · tool_failed · assistant_text · render
question_asked · question_answered
proposal_emitted · proposal_accepted · proposal_rejected
critic_check · budget_exceeded · run_finished
```

Derived directly from the prototype: `tool_called`/`tool_returned` are the
register lines, `step_completed` is `tickStep`, `question_asked` is the
paused-run block, and `proposal_*` covers rule, concept, playbook, and
preference cards uniformly.

### 1.2 Why `transcript` is a column, not derived from events

These look redundant. Conflating them is the mistake to avoid.

- **`transcript` is machine state** — the exact `messages` array, including
  thinking blocks, which must round-trip byte-identically or prompt caching
  breaks and the API rejects modified thinking blocks.
- **`run_events` is audit and UI truth** — what happened, in human terms,
  with provenance.

Reconstructing the transcript from events introduces drift at exactly the
point where drift is unrecoverable. Keep it opaque and never hand-edited.
Still one write path: the loop writes both in the same transaction.

### 1.3 `jobs` becomes an artifact

`jobs` survives as the **reconciliation artifact**, not an execution record. It
gains `run_id`; its `status` column is deprecated because the run owns status.
Everything referencing `job_id` — `ResultsPage`, `/api/compare`, export
tokens, `triage_items` — keeps working untouched.

This is what makes the macro-tool (plan §2.2) honest: a classic recon is a run
whose plan is one `run_reconciliation` call, producing a `jobs` row exactly as
`agent.py` does today.

### 1.4 `run_artifacts`

| Column | Notes |
|---|---|
| `id`, `run_id`, `account_id` | |
| `kind` | `dataset │ export │ chart` |
| `storage_key` | via the existing `storage_s3.py` abstraction |
| `schema_fingerprint` | jsonb, from the profiler (§4.4) |
| `row_count`, `created_at` | |

Required by decision 1: because runs suspend and resume in a *different
process*, a `dataset_id` cannot be a key into an in-memory dict. Artifacts
persist and rehydrate. `jobs` remains the one artifact kind with its own table
because it has downstream consumers and its own lifecycle.

### 1.5 Memory and ontology

The plan specifies `playbooks.json` and `preferences.json` as files. **That is
stale** — Phase 2.2 already moved rules, triage, decisions, and metrics into
Postgres. New memory is Postgres from the start.

- **`playbooks`** — `account_id` *or* `pack_id`, `name`, `trigger` jsonb,
  `steps` jsonb (concept-typed args), `provenance`, `stats` jsonb, `autonomy`
- **`preferences`** — `account_id`, `key`, `value` jsonb, `provenance`
- **`overlay_concepts`** — `account_id`, `concept_id`, `parent`, `entity`,
  `role`, `aliases`, `invariants`, `provenance` jsonb
  (`{run_id, column, user_confirmation}`),
  `status: proposed │ confirmed │ pack_candidate`
- **`pack_subscriptions`** — `account_id`, `pack_id`, `version`
- **`accounts.ontology_version`** — int, bumped on any overlay or subscription
  write; the `OntologyView` cache key (§4.2)

Packs themselves stay as files in `packs/` — versioned in git, diffable,
CI-testable against their own eval sets. Only the *subscription* is data. A
pack in the database is a pack you cannot review in a PR.

### 1.6 Scoping and retention

Every table above carries `account_id` and joins the Phase 2.5 purge path.
`run_events` will be the highest-volume table in the system and holds
row-level detail from customer files — it must be in `retention_days` sweeps
and in `delete_account` **from the first migration**, not retrofitted. The
governance doc already promises this.

Migrations land as `0008_runs` through roughly `0012`, each independently
reversible. Alembic currently tops out at `0007_connections`.

---

## 2. Tool contract

### 2.1 The rule everything follows from

**Tools take handles and return summaries. Row data never crosses the model
boundary — not in, not out.**

```python
@beta_tool
def bind_columns(dataset_id: str) -> BindingSummary:
    """Map a dataset's columns onto ontology concepts.

    Call this after profile_schema on any newly loaded dataset, before
    matching or classifying. Returns which columns bound and which did not.
    """
```

`BindingSummary` is
`{dataset_id, bound_count, total_count, mappings: [...], unbound: [...]}` —
bounded, small, every field a scalar or a reference the model passes onward.

This is what makes the plan's cost guardrails arithmetic work. Without it a
4,102-row claims file lands in the context window and the prototype's
`≤ $0.40 · ≤ 30 tool calls` budget line is fiction.

**Every tool declares an explicit output cap.** `unbound` returns at most N
columns, `samples` at most N values. A tool whose output grows with row count
is a bug, not a tuning problem.

### 2.2 ID namespaces

`dataset_id` (tabular artifact), `job_id` (reconciliation result, existing),
`concept_id` (ontology reference, existing). Tool arguments draw only from
these plus scalars and column names. A signature containing a list of rows is
rejected at registration.

### 2.3 Effect classification drives the autonomy dial

| Effect | Examples | `observe` | `assist` | `auto` |
|---|---|---|---|---|
| `read` | `profile_schema`, `bind_columns`, `match_by_key`, `classify`, `compare_runs`, `render` | proposed | runs freely | runs freely |
| `external` | `shopify.pull_orders`, `stripe.pull_charges` | proposed | pauses | runs within budget |
| `write` | `create_rule`, `resolve_triage`, `save_playbook`, `run_reconciliation` | proposed | pauses | proposal unless covered by an accepted rule/playbook |

`external` is separate from `read` because it is read-only against *your* data
but costs money and rate limit against someone else's. This distinction is
absent from the plan and needs to exist — a runaway planner re-pulling Shopify
thirty times is a different failure from one re-matching thirty times.

The dial copy in the prototype's right rail is this table in prose.

### 2.4 Critic post-conditions

Each tool registers deterministic checks in `critic.py`, keyed by tool name:

- `match_by_key` → `rows_matched + rows_unmatched == rows_in`
- `classify` → no verdict field carries LLM provenance
- `run_reconciliation` → today's existing invariants, unchanged
- any tool returning bound columns → ontology invariants from the resolved
  concept set

Pack invariants (`packs/*/invariants.py`) register into the same table — that
is what lets a domain pack extend the critic with zero core changes, the
Phase E acceptance test.

A failed check writes `critic_check{passed: false}` and either aborts or
replans. Never swallowed. `plan_abort_rate` (plan §6.4) counts these.

### 2.5 Registry layering — where prompt caching is won or lost

- **Tier 1, always in `tools`:** core deterministic tools plus memory read
  tools. Identical bytes for every account, sorted by name, so the whole
  tools prefix is one cache entry shared across the entire customer base.
- **Tier 2, `defer_loading: true`, discovered via
  `tool_search_tool_regex_20251119`:** pack tools and connection tools, which
  vary per account. Tool search *appends* discovered schemas rather than
  swapping the tool list, preserving the tier-1 prefix.

Inline pack tools would give every account a unique byte sequence at position
0 — no cache ever shared, and installing a pack invalidates that account's
cache wholesale.

**Naming** follows the prototype: bare verbs for core tools (`bind_columns`,
`match_by_key`, `apply_rules`, `classify`, `compare_runs`, `profile_schema`),
`provider.verb` for connection tools (`shopify.pull_orders`,
`stripe.pull_charges`). Those strings are what users read in the tool
register; they were written to be legible.

**Descriptions state trigger conditions, not just behaviour.** Opus 4.8
reaches for tools more conservatively than prior models, and prescriptive
"call this when…" descriptions measurably raise should-call rate.

### 2.6 Why the Tool Runner, and not the alternatives

- **LangChain** — provider abstraction. Nothing here needs it; it would sit
  between us and the parameters that matter.
- **LangGraph** — the only serious alternative, because `interrupt()` plus the
  Postgres checkpointer gives cross-process suspend/resume natively. Rejected
  because its checkpointer is **a second state store**: its tables would hold
  resumable state while `run_events` holds auditable state, reintroducing the
  two-state-machine problem decision 1 exists to avoid. Secondary cost is
  parameter lag — this design needs `thinking: {type: "adaptive"}`,
  `output_config.effort`, `task_budget`, Opus-4.8-only mid-conversation
  system messages, tool search with `defer_loading`, and byte-precise
  `cache_control` placement.
- **Claude Agent SDK** — Claude Code as a library, with built-in file/bash
  tools. Wrong shape; our deterministic tools *are* the product.
- **Managed Agents** — Anthropic hosts the loop and a sandbox. Our tools run
  against customer data in our Postgres/S3, so they would be custom tools our
  orchestrator executes anyway, and byte-exact replay is lost.

**What the Tool Runner gives us:** `@beta_tool` generates each tool's JSON
schema from the Python signature and docstring, which is most of §2.1's work.
Per-turn hooks cover approval gates, error interception, result modification,
and retries.

**What we do ourselves:** we mirror the message history into
`runs.transcript` as we iterate, because the Python runner keeps its own copy
and does not expose it, and cannot be resumed mid-loop. On resume we construct
a **fresh** runner from the persisted history.

**Verify on day one** (both are assumptions this design rests on):
1. `@beta_tool`-generated schemas serialize byte-identically across runs.
2. Raw tool definitions (`tool_search_tool_regex`) can be mixed into the
   runner's `tools` list alongside decorated ones.

### 2.7 Budgets — two, not one

- `output_config.task_budget` (beta `task-budgets-2026-03-13`, minimum 20,000
  tokens) is **model-aware**: the model sees a countdown and wraps up
  gracefully instead of being cut off.
- Hard caps — tool calls, wall clock, dollars — are **ours**, enforced in the
  loop against `runs.spend`, emitting `budget_exceeded`.

The prototype's `≤ $0.40 · ≤ 30 tool calls · ~20s` is the second kind. Both
belong in the design.

---

## 3. Block protocol

### 3.1 One channel

The plan says the agent emits typed UI blocks; §1.1 says the event log is the
single source. Taken literally those are two channels that will drift.

**Blocks are a pure function of events, computed client-side.** The server
emits only `run_events`; the frontend runs `blocksFromEvents(events)`, a
reducer. This buys a property worth more than it costs:

> Replaying a run renders identically to watching it live, because it is the
> same events through the same reducer.

The non-collapsing audit principle becomes structural rather than
aspirational. Live stream, audit view, and `replay.py` read one vocabulary.

### 3.2 The agent still composes the UI

Most blocks fall out of events mechanically. Some do not — the agent genuinely
chooses "hero line, three tiles, verdict counts" over "a ledger breakdown."
That choice gets one tool:

```python
@beta_tool
def render(block: Block) -> None:
    """Compose a UI block into the conversation for the user to see.

    Call this to present findings — a summary, a ledger breakdown, a triage
    card. Do not use it to narrate; plain text is already shown.
    """
```

`read`-effect, JSON-schema-constrained by the `Block` union. Two reasons this
beats parsing prose: tool inputs are never summarized by the model, so the
block arrives intact; and a schema union gives strict validation of every
block the agent can emit.

### 3.3 Catalogue

**Projected from events** (no agent choice):

| Block | Source events |
|---|---|
| `plan` | `plan_proposed`, ticked by `step_started`/`step_completed` |
| `tool_register` | `tool_called` + `tool_returned`/`tool_failed`, coalesced |
| `question` | `question_asked`, resolved by `question_answered` |
| `proposal` | `proposal_emitted` → `rule │ concept │ playbook │ preference` |
| `agent_text` | `assistant_text` |
| `run_status` | `budget_exceeded`, failed `critic_check`, `run_finished` |

**Agent-composed** (via `render`): `summary`, `audit`, `triage_card`,
`ledger`, `metric_tile`, `table`, `chart`, `memory_summary`.

Every one exists in the prototype. The four proposal variants share one card
shape, which is what makes "one consistent proposal-card UX" (plan §6.2) real
rather than four near-duplicates.

### 3.4 Accepting a proposal writes an event

The prototype's `onAccept` mutates the memory rail in the browser. In
production that inverts: accepting POSTs, the server writes
`proposal_accepted`, and the client's existing SSE subscription updates the
rail from that event.

The memory panel becomes a **live projection of the event stream** rather than
a separate fetch. A second tab stays in sync for free, and revocation uses the
identical path — so "revoke and I'll ask again next time" is one code path
with accept.

### 3.5 Provenance and escape hatches

- `assistant_text.citations: [{event_id, label}]` — the prototype's `.cite`
  chips. Clicking scrolls to and expands that tool event.
- Every agent-composed block carries `source_events: [id]` and optionally
  `dataset_id` + a row filter. That is "show the rows" / "show why", and per
  decision 2 it opens the existing `ResultsPage`/`DataTable` in the centre
  pane. **This is why the old pages survive** — blocks deep-link into them.

### 3.6 Transport

`GET /api/agent/runs/{id}/events` — SSE, `Last-Event-ID` against the
`run_events` bigserial. `POST /api/agent/runs` creates a run. Both
feature-flagged behind `RECONOPS_AGENT_RUNTIME=1` during migration.

Reconnect is lossless by the standard pattern: **open the stream first**,
fetch history via the paginated list endpoint, dedupe by event id, then tail.
Open-then-fetch, not fetch-then-open — the stream buffers from the moment it
opens, so the other order drops everything in the gap.

### 3.7 Two behaviours that fall out for free

- **The no-plan fast path.** No `plan_proposed` event → no plan block. The
  prototype's *"Quick question — no full plan needed. Three tool calls."*
  needs no special casing.
- **The opening briefing.** *"Since Friday: Shopify synced 41 new orders…"* is
  a run with `goal.intent = "briefing"`, triggered on session open or by
  connection sync, rendering as ordinary `assistant_text`. No new machinery —
  just a trigger. (The plan does not describe where this comes from.)

### 3.8 Versioning

`render` payloads carry `schema_version`. Runs are replayed months later
against a reducer that has moved on; without a version you get silent
mis-renders of historical audit records.

---

## 4. Ontology resolution & context assembly

### 4.1 Tiers and cache layers are the same ordering

Core changes almost never, packs change per release, overlays change per
confirmation — least-volatile to most-volatile, which is exactly what a prefix
cache wants. One design, not two:

| Layer | Content | Shared across |
|---|---|---|
| tools | tier-1 registry, sorted | **every account** |
| system ¶1 ⟨breakpoint⟩ | fixed instructions + **core** ontology | **every account** |
| system ¶2 ⟨breakpoint⟩ | subscribed **pack** ontologies | accounts on the same pack versions |
| system ¶3 ⟨breakpoint⟩ | account digest: **overlay** concepts, accepted rules, preferences, playbook triggers | that account |
| messages | goal, data profile, retrieved context, conversation | nothing |

Changing the system prompt invalidates the system and messages caches but
**not** the tools cache, so tier-1 tools stay shared as everything below
varies. Accepting a rule invalidates only ¶3 and below.

**Measure early:** Opus 4.8's minimum cacheable prefix is 4096 tokens. Tools +
instructions + core ontology must clear that bar or ¶1 silently never caches —
no error, just `cache_creation_input_tokens: 0`.

### 4.2 `OntologyView` replaces the module globals

`ontology/__init__.py` currently loads `concepts.yaml` at import into
module-level `CONCEPTS` and `ALIAS_INDEX`. That narrows to core-tier-only
(`CORE_CONCEPTS`); per-account resolution moves to an object:

```python
@dataclass(frozen=True)
class OntologyView:
    account_id: str
    concepts: Mapping[str, Concept]
    alias_index: Mapping[str, str]
    packs: tuple[PackRef, ...]
    fingerprint: str          # cache key, and cache-stability guarantee
```

`Concept` gains `parent` (is-a edge into core), `relations`, `tier`, and
`source`. The last two are not decoration — they render the prototype's left
rail (*"41 concepts · + 5 concepts you taught it"*) and carry provenance on
every binding.

**Two resolution orders, which the plan conflates:**

- **Merge order** (building the view): core → packs in subscription order →
  overlay. Later tiers extend or override.
- **Binding order** (matching a column): overlay aliases → embeddings → pack
  aliases → core aliases → value-shape heuristics. Plan §5.1's order, gaining
  one tier.

Views are built per `(account_id, ontology_version, pack_versions)` and held
in a process LRU.

### 4.3 What the refactor touches

This is the riskiest change in the plan; the plan's "it just gains one tier"
understates it.

| File | Change |
|---|---|
| `ontology/__init__.py` | globals narrow to core tier; core-only callers keep working |
| `tools/binding.py` (301 lines) | signature change to accept a view — the largest edit |
| `ontology/invariants.py` | becomes critic-registered, takes a view |
| `main.py` `/api/concepts` | becomes account-scoped |
| `memory/learned_aliases.py`, `memory/embedding_index.py` | feed the overlay tier rather than being consulted ad hoc |

### 4.4 The assembler and the volatility trap

`backend/app/context/` gets two modules.

**`profiler.py`** computes a `SchemaFingerprint` per column — name n-grams,
dtype, null rate, cardinality, value-shape stats — reusing the existing
`ValueHints` machinery. Stored on `run_artifacts.schema_fingerprint` so a
resumed run does not re-profile.

**`assembler.py`** builds the message array against a token budget, using
`count_tokens` rather than estimating.

**The trap.** The plan asks for "a relevance ranking" over account memory. An
account with 200 rules cannot ship all of them. But **ranking is volatile** —
reorder the digest per goal and ¶3's bytes change every run, so the
account-tier cache never hits.

Split by volatility, not by importance:

- **¶3, stable, deterministically sorted by id:** accepted rules, confirmed
  overlay concepts, preferences, playbook triggers. Cacheable.
- **After the last breakpoint, in `messages`:** goal-specific retrieval —
  similar past runs, observations matching this data profile, artifacts in
  play. Volatile by nature, and free because it is past the cache boundary.

If ¶3 genuinely overflows the budget, evict by a stable rule (drop revoked,
drop unused-in-N-runs) — never reorder.

### 4.5 Induction needs no new mechanisms

Plan §5.2's five steps map onto machinery already specified:

1. **Profile** — `profile_schema` → `SchemaFingerprint`
2. **Match** — `bind_columns` against the `OntologyView`
3. **Propose** — unmatched columns + profile + samples → LLM →
   `proposal_emitted{kind: "concept"}` → the prototype's concept card
   (`is-a` / samples / aliases / invariant)
4. **Confirm** — `proposal_accepted` → row in `overlay_concepts` with
   provenance; bump `ontology_version`
5. **Consolidate** — N confirmations flips status to `pack_candidate`

Step 3 is the only LLM call, and it proposes; it never writes. House law
holds: **the LLM never computes money.**

### 4.6 Resume uses mid-conversation system messages

`{"role": "system", ...}` appended to `messages[]` (Opus 4.8 only, no beta
header) injects the user's `ask_user` answer, or an autonomy-dial change,
*after* a suspend without touching the cached prefix. This is the mechanism
behind the prototype's paused-run resume and its mid-session dial flip.

---

## 5. Corrections to `2026-07-30-agentic-platform.md`

Found by reading the code, not the doc.

1. **The run substrate does not exist.** `main.py:331` hands jobs to
   `BackgroundTasks`; `main.py:378` spawns a daemon `threading.Thread`. No
   `runs` table, no queue, no durable state. The plan cites the async-jobs
   plan as "the substrate", but that plan is unexecuted and predates the
   agent-run concept. **Dependency #1.**
2. **Ontology is an import-time singleton** — see §4.2/§4.3. The plan's
   "it just gains one tier" understates the highest-risk refactor in the plan.
3. **`llm.py` cannot run an agent.** `call_claude()` (`llm.py:45`) is
   one-shot, text-out, no tools, no streaming, no conversation state. A new
   module, not an extension — and it must remain the single usage-logging
   chokepoint it is today.
4. **Tool args must be handles, not payloads** (§2.1). The plan never states
   this and everything downstream depends on it.
5. **Trace and event stream are one object** (§1.1). Two mechanisms drift.
6. **Naming drift:** plan §2 says `backend/app/agent/`, §8 says
   `backend/app/agent_runtime/`. §1 says "9-step pipeline"; `agent.py` has 11
   numbered steps. **Use `backend/app/agent_runtime/`.**

**Prototype commitments the plan does not capture:** the no-plan fast path
(§3.7), blocks carrying effects (§3.4), and the unprompted opening briefing
(§3.7).

**Dependency bump:** `anthropic==0.39.0` predates the Tool Runner,
`output_config`, and adaptive thinking entirely. `llm.py:19` defaults to
`claude-opus-4-7`; it should be `claude-opus-4-8`. The planner uses
`thinking: {type: "adaptive"}` with `output_config: {effort: "xhigh"}` for
agentic work — `budget_tokens` returns a 400 on 4.8, so nothing new should
reach for it. The SDK bump is a Phase A task and will surface incidental
breakage in `llm.py`'s existing call sites.

---

## 6. Open questions

Genuinely arguable, deliberately unresolved:

1. **`transcript` placement** — on `runs`, or its own table? It is large and
   rewritten every turn, which makes `runs` rows churn.
2. **`run_events.payload`** — free jsonb, or typed per event?
3. **`run_reconciliation` effect** — classed `write` here because it produces
   triage items. But if producing proposals counts as write, nearly
   everything is write and `assist` collapses into `observe`.
4. **`triage_card` ownership** — agent-composed via `render` (agent surfaces
   two of nine) or projected from a `triage_emitted` event (guarantees
   nothing is hidden)? Trust argument cuts both ways.
5. **Whether packs deserve a cache breakpoint** — four exist total; spending
   one on packs pays off only once multiple accounts share pack versions.

---

## 7. Phase mapping

The plan's phases hold; this spec changes what lands in each.

| Phase | Add / change |
|---|---|
| **A** — runtime beside the pipeline | Absorbs the async-jobs plan. Migrations `0008`–`0009` (`runs`, `run_events`, `run_artifacts`). SDK bump. `agent_runtime/` with the Tool Runner loop, transcript mirroring, critic registry, budget enforcement. Tier-1 tool registry. SSE endpoints. Eval gate unchanged: classic-recon goals through the planner must match macro-tool outputs on the golden set. |
| **B** — ontology tiers + induction | `OntologyView`, `accounts.ontology_version`, `overlay_concepts`, `pack_subscriptions`, `context/profiler.py`, `context/assembler.py`, the ¶1–¶3 cache layering, tier-2 deferred tools behind tool search. |
| **C** — memory upgrade | `playbooks`, `preferences` tables; Learner distillation; autonomy demotion wiring. |
| **D** — conversational surface | Three-pane shell (decision 2), `blocksFromEvents` reducer, SSE client with lossless reconnect, memory rail as live event projection. Existing pages become panes. |
| **E** — genericity proof | Second pack with zero core edits. The critic registry (§2.4) and pack-tier ontology (§4.1) are what make this testable. |

**Dependencies:** A → B → C can interleave; D needs A; E needs B. The Postgres
and object-storage work is already done (Phase 2.2, committed).
