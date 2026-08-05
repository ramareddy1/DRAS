# Agent Resume — Design

**Status:** approved, not yet implemented
**Date:** 2026-08-03
**Depends on:** [`2026-08-01-agent-runtime-architecture.md`](2026-08-01-agent-runtime-architecture.md) (§2.6 suspend/resume, §4.6 mid-conversation system messages)
**Closes:** the first entry under "Known gaps deferred past Phase A" in [`../plans/2026-08-01-phase-a-agent-runtime.md`](../plans/2026-08-01-phase-a-agent-runtime.md)

## Problem

Phase A suspends a run when a tool call hits an autonomy gate, and persists everything needed to continue: the transcript, the spend, and `suspended_on` pointing at the `question_asked` event. Nothing consumes that state. There is no way to answer the question, so a suspended run is terminal in practice.

This also blocks the Phase A acceptance argument. `run_reconciliation` is `Effect.write`, and writes gate at every autonomy level including `auto`, so the only path that drives the macro-tool through the loop ends in a suspend. `eval.parity_report` compares at the tool boundary precisely because resume does not exist.

## The API constraint that shapes everything

Spec §4.6 says resume "uses mid-conversation system messages" — `{"role": "system", ...}` appended to `messages[]`, Opus 4.8 only, no beta header, preserving the cached prefix. That mechanism is real, but §4.6 omits two rules that make it insufficient on its own:

1. A mid-conversation system message **must follow a `user` turn** (or an assistant turn ending in *server*-tool use — not client `tool_use`), and cannot be `messages[0]`.
2. Every `tool_use` block must be answered by a matching `tool_result`, and all results for a turn must arrive in **one** user message.

A suspended transcript ends with an assistant turn holding unanswered `tool_use` blocks. So a system message cannot simply be appended. The approval decision has to *become* the `tool_result`:

```
[...assistant turn with tool_use blocks]                    persisted at suspend
{"role": "user",   "content": [tool_result × every pending id]}   required
{"role": "system", "content": <user note>}                        now legal, optional
```

The system message carries the user's prose; it does not do the structural work.

## Decisions

| # | Decision | Rejected alternative |
|---|---|---|
| 1 | **Whole-turn approve/reject.** One decision covers every pending call in the turn. | Per-call verdicts. The gate is already all-or-nothing per turn (`runtime.py`, "Pass 1"); per-call decisions would add partial-turn execution ordering and partial critic checks for a case we do not have. |
| 2 | **Reject feeds a denial back and the loop continues,** guarded against repeats. | Reject aborts the run. A single "no" would kill a run that has done real work, forcing a restart to say "not that — do this instead." |
| 3 | **Wall clock counts agent compute only;** suspended time is excluded. | Counting true elapsed time. `DEFAULT_WALL_CLOCK_S = 120` and human approval takes minutes to hours, so every approved run would trip `budget_exceeded` on resume. The cap exists to stop a *runaway* agent (§2.7); a run blocked on a human is not overreaching. |

**Decision 2's guard.** A rejected turn is exactly when the model is most likely to re-propose the same call, which would gate again and ping-pong. `MAX_ITERATIONS` and `tool_call_cap` bound the loop but are not a *good* stop — the run burns its budget on something the user already refused. So rejected `(tool_name, input)` pairs are recorded on the run, and an identical re-proposal aborts instead of gating a second time.

## Bugs this fixes on the way

Both were found while planning, not while writing this design. Neither is reachable today; resume is what makes both reachable, so both are in scope here rather than deferred.

### `Spend` cannot cross a process boundary

`Spend.started_at` is `time.monotonic()` — process-local, arbitrary epoch. `to_dict()` serializes only the derived `elapsed_s`, and there is no `from_dict`. Decision 1 of the architecture spec says a run resumes in a *different process*, where a monotonic value from the original process is meaningless.

The consequence is not just that resume cannot rehydrate spend — it is that a fresh `Spend()` resets `tool_calls` and `usd` to zero. The hard caps would silently become **per-segment instead of per-run**: suspend and resume in a loop and no cap ever trips. This must be fixed as part of resume regardless of the wall-clock decision.

`_would_exceed` builds a throwaway `Spend` to probe the next call against the caps. That probe must carry `accumulated_s` too, or a resumed run under-reports elapsed time at exactly the moment the cap matters.

### `_dispatch` cannot resolve the macro-tool

`_dispatch` resolves a tool name with `getattr(tools_core, name, None)`, but `run_reconciliation` is defined in `tools_macro`. Verified against the live registry rather than by reading:

```
in tools_core?       False
in registry._TOOLS?  True
_TOOLS keys: ['bind_columns', 'match_datasets', 'profile_schema', 'run_reconciliation']
```

Approving a gated `run_reconciliation` therefore raises `KeyError: unknown tool`. It is latent only because that tool is `Effect.write`, so it gates at every autonomy level and never reaches dispatch — **resume is the first code path that gets past the gate**, which makes this a resume blocker rather than a cleanup.

The fix routes dispatch through `registry.callable_for(name)`, the same source of truth the schemas are built from. That also closes the attribute-lookup hazard the existing code comment already flags as a follow-up: `getattr` on a module will happily return any public symbol, registered as a tool or not.

## Design

### `agent_runtime/budget.py`

`Spend` gains `accumulated_s: float = 0.0` and a `from_dict`. `elapsed_s()` returns `accumulated_s + (monotonic() - started_at)`. `to_dict()` still emits the total under `elapsed_s`, so the persisted shape is unchanged; `from_dict` restores `tool_calls` and `usd`, sets `accumulated_s` from the persisted `elapsed_s`, and restarts the local clock. Decision 3 and the bug fix land in the same change.

### `agent_runtime/store.py`

One new primitive:

```python
claim_suspended(run_id: str, account_id: str) -> Run | None
```

Flips `suspended → running` inside the existing `with_for_update()` lock and returns `None` if the run was not suspended. This is what makes a double-POST safe — without it, two background tasks would each execute the pending tool. It also clears `suspended_on`, which `set_status` cannot currently do (it assigns only when the argument is non-`None`).

### `app/models.py`

`Run` gains `rejected_calls: List[Dict[str, Any]]` and `suspended_at: Optional[str]`. Both live in `payload`; neither is queried or filtered. **No migration** — `payload` is JSONB, and every event type resume emits (`question_answered`, `run_finished`) already exists in `RunEventType`.

### `agent_runtime/runtime.py`

The loop body extracts into `_drive(run, messages, budget, spend, driver)`, shared by both entry points. `execute_run` builds `messages` fresh from the goal; `resume_run` rehydrates them from `runs.transcript`. Gate, critic, and budget logic is written once.

```python
resume_run(*, run_id, account_id, decision, note=None, driver=None) -> Run
```

1. Verify the run is `running` — the route has already claimed it (below); refuse with `ValueError` otherwise, mirroring how `execute_run` refuses a finished run. This covers direct callers such as tests, which claim explicitly before invoking.
2. Rehydrate `messages` from the transcript and `spend` via `Spend.from_dict`.
3. Assert the last entry is an assistant turn carrying `tool_use` blocks. The entire API contract rests on this invariant, so it fails loudly rather than sending a malformed request.
4. Build a `tool_result` for **every** pending `tool_use` id:
   - **approve** — execute the tool through the same critic and budget path as a normal turn; append its real result.
   - **reject** — append `tool_result` with `is_error: true` and the denial text; record `{tool, input}` into `run.rejected_calls`.
5. Append all results in **one** user message. Splitting them across messages trains the model out of parallel tool calls.
6. If `note` is present, append `{"role": "system", "content": note}` — legal now, because it follows a user turn and is last.
7. Emit `question_answered`, then continue `_drive`.

The repeat guard sits in `_drive` ahead of the gate check: a proposed call whose `(name, input)` matches an entry in `rejected_calls` aborts with `run_finished{reason: "repeat_of_rejected_call", tool: ...}`.

Mid-conversation system messages are Opus 4.8-only. `DEFAULT_MODEL` is pinned to exactly that, so this is safe today; the constraint is worth a comment at the call site, since an unsupported model returns 400 `role 'system' is not supported on this model`.

### `agent_runtime/routes.py`

```
POST /api/agent/runs/{run_id}/answer   {decision: "approve"|"reject", note?: str}
  → {run_id, status}
```

Gated on `RECONOPS_AGENT_RUNTIME=1` and scoped through `require_account`, like every other agent route. Returns 404 cross-account.

The route calls `claim_suspended` **synchronously**, before scheduling anything: `None` means the run was not suspended, which is the 409. A claim succeeds at most once, so a double-POST gets one 200 and one 409 rather than two background tasks executing the same pending tool. Only after a successful claim does it schedule `runtime.resume_run` on `BackgroundTasks`, mirroring run creation.

Doing the claim in the background task instead would make the 409 unreportable — the route would have already returned 200.

## Error handling

| Condition | Behavior |
|---|---|
| Run not suspended | 409 from the route (failed `claim_suspended`); `ValueError` from `resume_run` |
| Concurrent double-POST | One claim wins: 200 + one execution; the loser gets 409 |
| Cross-account | 404 — never 403, so the API is not an existence oracle |
| Transcript does not end in an assistant `tool_use` turn | Abort the run with `error` set; do not crash the background task |
| Approved tool raises | Existing `tool_failed` path, unchanged |
| Critic fails on an approved tool | Existing critic-abort path, unchanged |

## Testing

`ScriptedDriver` throughout — no live model calls, consistent with the existing runtime tests.

- Approve executes the pending tool and the loop continues.
- Reject feeds `is_error` back and the model re-plans.
- Reject followed by an identical re-proposal aborts on the guard.
- `tool_calls` and `usd` accumulate across the suspend boundary — pins the per-segment-cap bug.
- Wall clock excludes suspended time.
- Double-resume executes the pending tool exactly once.
- `resume_run` refuses a `done` run.
- Cross-account resume returns 404.
- The resumed transcript places the system note in a legal position (after a user turn, last).

## Out of scope

Stated so they are not mistaken for oversights.

- **`ask_user` as a registered tool.** No such tool exists; every suspend today is an autonomy gate. When it lands, it reuses this same `tool_result` path.
- **Per-call approval** — Decision 1.
- **Abandonment reaper.** A run may sit suspended indefinitely. Reaping stale suspended runs belongs on a `suspended_at` timestamp (added here for exactly that purpose), not on the agent's compute budget; `storage.reap_stale_jobs` is the existing precedent.
- **Autonomy-dial changes mid-run.** §4.6 mentions the same mechanism carrying a dial flip; that is a separate capability.
- **Durable execution.** Resume rides `BackgroundTasks` like run creation, so a restart mid-resume leaves a row in `running`. The run and its events remain durable; replacing the scheduler is a swap, not a rewrite.
