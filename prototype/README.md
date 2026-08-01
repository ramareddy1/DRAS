# Reckon — AI-native UI prototype

**Live:** https://reckon-prototype.vercel.app

A single-file, fully static prototype of the agentic product direction described
in [`docs/plans/2026-07-30-agentic-platform.md`](../docs/plans/2026-07-30-agentic-platform.md)
(§7, "AI-native UX"). All data is simulated; there is no backend and nothing
sensitive — safe to deploy and share publicly.

## What it demonstrates

| Plan principle | Where to see it |
|---|---|
| One conversational surface, no fixed routes | The whole app is the stream; inbox/rules/metrics render *inside* it on request |
| Visible, interruptible plans | "Close out June" → plan block with budget; in **Observe** mode it waits for approval |
| Tool-level transparency (audit band) | Mono "register" lines for every tool call; expandable "handled silently by 3 rules — audit" |
| Clarification as a first-class state | The order-date vs payout-date question pauses the run; in **Auto** it resolves from preference memory |
| Every side effect is a proposal | Playbook card, fee-rule update, triage actions — all accept/revoke with provenance |
| Ontology derivation | "Drop an unfamiliar file" → schema profiling → three concept-proposal cards → overlay grows |
| Learning & adapting | Accepting proposals updates the memory rail and the trust-adjusted autonomy score |
| Memory transparency | "What I know about you" rail — every item shows provenance and is revocable in place |
| Earned autonomy | The Observe / Assist / Auto dial changes actual behavior, not just a label |

## Run locally

Open `index.html` in a browser — no build step, no dependencies.

## Deploy to Vercel

From this directory:

```bash
npx vercel deploy --prod
```

Or drag the `prototype/` folder onto https://vercel.com/new. `vercel.json`
adds clean URLs and basic security headers; nothing else is required.
