"""FastAPI app for ReconOps AI pilot."""
from __future__ import annotations

import json
import math
import os
import uuid
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from .agent import run_job
from .llm import is_configured
from .memory import accounts as accounts_memory
from .memory import (
    decision_log,
    learned_aliases,
    metrics as metrics_store,
    notes as notes_store,
    observations as observations_store,
    rule_proposer,
    rules_store,
    triage as triage_store,
)
from .models import (
    Account, BindResponse, DecisionLogEntry, ReconcileConfig, Rule, SemanticBinding,
)
from .report import build_report
from .tools.binding import bind_columns
from .tools.extract import extract_from_text
from .tools.ingest import preview, read_table
from . import storage

load_dotenv()

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

from .obs import RequestLogMiddleware, setup_logging, setup_sentry

app = FastAPI(title="ReconOps AI", version="0.1.0")

setup_logging()
setup_sentry()
app.add_middleware(RequestLogMiddleware)

# In production the edge proxy serves frontend and API same-origin, so CORS
# rarely applies; the env var covers split-origin setups (staging, previews).
_cors_origins = [
    o.strip() for o in os.getenv(
        "RECONOPS_CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",") if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Account-Id", "X-Request-ID"],
)


def require_account(x_account_id: str = Header(default="")) -> Account:
    """FastAPI dependency: load the account named by the X-Account-Id header.

    Returns 401 when the header is missing or names an account that doesn't
    exist. Pilot has no auth — the UUID is the access token.
    """
    if not x_account_id:
        raise HTTPException(
            status_code=401,
            detail="Account not initialized. Create one at POST /api/accounts.",
        )
    acc = accounts_memory.load_account(x_account_id)
    if acc is None:
        raise HTTPException(
            status_code=401,
            detail=f"Unknown account id '{x_account_id}'.",
        )
    return acc


@app.on_event("startup")
def _startup():
    storage.ensure_dirs()
    storage.cleanup()


def _clean(obj):
    """Replace NaN/Inf with None so JSON serialization stays valid."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj


@app.get("/api/health")
def health():
    return {"ok": True, "llm_configured": is_configured(), "version": app.version}


@app.get("/api/concepts")
def concepts():
    """Return the concept graph for the UI dropdown."""
    from .ontology import CONCEPTS
    return [
        {"id": c.id, "type": c.type, "role": c.role, "entity": c.entity, "aliases": list(c.aliases)}
        for c in CONCEPTS.values()
    ]


# --- Accounts (v3) ---------------------------------------------------------

@app.post("/api/accounts", response_model=Account)
def create_account(payload: Optional[dict] = None):
    """Create a new account. No auth — the returned UUID is the access token.

    Pilot: the frontend calls this once on first visit and stores the ID in
    localStorage. We also seed the default fee-pattern rules so brands get
    Stripe / PayPal handling out of the box.
    """
    display_name = (payload or {}).get("display_name") if payload else None
    acc = accounts_memory.create_account(display_name=display_name)
    rules_store.seed_defaults(acc.id)
    return acc


@app.get("/api/accounts/me", response_model=Account)
def get_my_account(account: Account = Depends(require_account)):
    return account


@app.patch("/api/accounts/me/profile", response_model=Account)
def patch_profile(payload: dict, account: Account = Depends(require_account)):
    """Update per-account settings (tolerances, materiality thresholds)."""
    allowed = {k: (payload or {}).get(k) for k in
               ("time_zone", "amount_tolerance_abs", "amount_tolerance_pct",
                "materiality_abs", "materiality_pct")}
    try:
        return accounts_memory.update_profile(account.id, allowed)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid profile update: {e}")


@app.post("/api/preview")
async def preview_file(
    file: UploadFile = File(...),
    x_account_id: str = Header(default=""),
):
    data = await file.read()
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File exceeds 10MB limit.")
    try:
        df = read_table(data, file.filename or "upload.csv")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read file: {e}")
    p = preview(df)
    p["filename"] = file.filename
    p["size_bytes"] = len(data)
    # Phase 4: account-scoped learned aliases take precedence over the global ontology.
    # We use the header opportunistically — preview works even without an account.
    acc_id = x_account_id if x_account_id and accounts_memory.account_exists(x_account_id) else None
    p["bindings"] = [b.model_dump() for b in bind_columns(df, account_id=acc_id)]
    return _clean(p)


@app.post("/api/bind", response_model=BindResponse)
async def bind_file(
    file: UploadFile = File(...),
    x_account_id: str = Header(default=""),
):
    """Infer SemanticBindings for an uploaded file without taking a full preview."""
    data = await file.read()
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File exceeds 10MB limit.")
    try:
        df = read_table(data, file.filename or "upload.csv")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read file: {e}")
    acc_id = x_account_id if x_account_id and accounts_memory.account_exists(x_account_id) else None
    return BindResponse(
        filename=file.filename or "upload.csv",
        row_count=int(len(df)),
        columns=list(df.columns),
        bindings=bind_columns(df, account_id=acc_id),
    )


@app.post("/api/upload")
async def upload_and_reconcile(
    file_a: UploadFile = File(...),
    file_b: UploadFile = File(...),
    config: str = Form(...),
    account: Account = Depends(require_account),
):
    # Enforce the 24h/7d retention promise on long-lived servers — startup-only
    # cleanup never fires again on a server that stays up.
    storage.cleanup()

    try:
        cfg_dict = json.loads(config)
        cfg = ReconcileConfig(**cfg_dict)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid config: {e}")

    data_a = await file_a.read()
    data_b = await file_b.read()
    if len(data_a) > MAX_FILE_SIZE or len(data_b) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File exceeds 10MB limit.")

    try:
        df_a = read_table(data_a, file_a.filename or "a.csv")
        df_b = read_table(data_b, file_b.filename or "b.csv")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read file: {e}")

    job_id = str(uuid.uuid4())

    base_payload = {
        "job_id": job_id,
        "account_id": account.id,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "schema_version": 3,
        "config": cfg.model_dump(),
        "filenames": {"a": file_a.filename, "b": file_b.filename},
        "bindings_a": cfg.source_a.model_dump(),
        "bindings_b": cfg.source_b.model_dump(),
    }

    try:
        result = run_job(account=account, df_a=df_a, df_b=df_b, cfg=cfg, job_id=job_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Reconciliation failed: {e}")

    payload = {
        **base_payload,
        "status": "complete",
        "summary": result.summary.model_dump(),
        "matched": result.matched,
        "unmatched_a": result.unmatched_a,
        "unmatched_b": result.unmatched_b,
        "discrepancies": result.discrepancies,
        "timing": result.timing,
        "insights": result.insights,
        "insights_status": result.insights_status,
        "llm_calls": result.llm_calls,
        "metrics": result.metrics.model_dump(mode="json") if result.metrics else None,
        "triage_emitted_count": len(result.triage_emitted),
        "rule_applications": result.rule_applications,
        "expected_unmatched_a": result.expected_unmatched_a,
        "expected_unmatched_b": result.expected_unmatched_b,
        "binding_warning": result.binding_warning,
        "key_col_a": result.key_col_a,
        "key_col_b": result.key_col_b,
    }
    storage.save_job(job_id, _clean(payload))
    return {"job_id": job_id, "status": "complete"}


def _backfill_rationale(rows):
    """Add a minimal rationale object to legacy (pre-v3) matched rows so the
    frontend drawer never crashes. v1/v2 stored `status` and `fee_pattern` but
    no structured rationale; we synthesize one from those fields."""
    for r in rows:
        if "rationale" in r and r["rationale"]:
            continue
        status = r.get("status") or "match"
        source = "legacy"
        if r.get("fee_pattern"):
            source = "fee_pattern_legacy"
            evidence = f"legacy fee pattern: {r['fee_pattern']}"
        elif status == "match":
            evidence = "legacy: marked match (pre-v3 job; no structured evidence stored)"
        else:
            evidence = (f"legacy: diff_abs=${r.get('diff_abs')}, "
                        f"diff_pct={r.get('diff_pct')}%")
        r["rationale"] = {
            "row_key": r.get("key", ""),
            "status": status if status in ("match", "minor", "major", "fee_offset") else "match",
            "confidence": 0.5,
            "rationale": [{"source": source, "evidence": evidence, "weight": 1.0}],
            "alternatives": [],
            "user_reason": None,
        }
    return rows


def _load_job_for_account(job_id: str, account: Account) -> dict:
    job = storage.load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    # Pre-v3 jobs may not have account_id; treat them as accessible (pilot data
    # is throwaway). Once v3 jobs exist, enforce strict account scoping.
    job_account = job.get("account_id")
    if job_account is not None and job_account != account.id:
        raise HTTPException(status_code=404, detail="Job not found")
    # Backfill rationale on any legacy job so the v3 frontend can read it.
    if job.get("schema_version", 1) < 3:
        _backfill_rationale(job.get("matched", []))
        _backfill_rationale(job.get("discrepancies", []))
    return job


@app.get("/api/jobs")
def list_jobs_endpoint(account: Account = Depends(require_account)):
    """Server-side job history — survives cleared browser storage."""
    return _clean({"jobs": storage.list_jobs(account.id)})


@app.get("/api/status/{job_id}")
def status(job_id: str, account: Account = Depends(require_account)):
    job = _load_job_for_account(job_id, account)
    return {"job_id": job_id, "status": job.get("status", "complete"), "created_at": job["created_at"]}


@app.get("/api/results/{job_id}")
def results(job_id: str, account: Account = Depends(require_account)):
    return _load_job_for_account(job_id, account)


@app.get("/api/results/{job_id}/export")
def export(
    job_id: str,
    x_account_id: str = Header(default=""),
    account_id: str = "",
):
    """Export accepts X-Account-Id OR ?account_id= because <a href> downloads
    can't set a header. Pilot only; production should use a short-lived
    signed token."""
    acc_id = x_account_id or account_id
    if not acc_id:
        raise HTTPException(status_code=401, detail="Account not initialized.")
    acc = accounts_memory.load_account(acc_id)
    if acc is None:
        raise HTTPException(status_code=401, detail=f"Unknown account id '{acc_id}'.")
    job = _load_job_for_account(job_id, acc)
    from .models import ReconcileResult, Summary
    result = ReconcileResult(
        job_id=job["job_id"],
        created_at=job["created_at"],
        config=ReconcileConfig(**job["config"]),
        summary=Summary(**job["summary"]),
        matched=job["matched"],
        unmatched_a=job["unmatched_a"],
        unmatched_b=job["unmatched_b"],
        discrepancies=job["discrepancies"],
        insights=job["insights"],
        timing=job.get("timing"),
    )
    blob = build_report(result)
    fname = f"reconops-{job_id[:8]}.xlsx"
    return Response(
        content=blob,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ===========================================================================
# Phase 5 — HITL surfaces (thin wrappers around the Phase 4 memory modules)
# ===========================================================================

_STATE_RANK = {"open": 0, "recurring": 1, "deferred": 2}


@app.get("/api/inbox")
def inbox(job_id: str = "", account: Account = Depends(require_account)):
    """Cross-job triage queue. Ranked by state (open > recurring > deferred),
    then $ impact, then recurrence count."""
    items = triage_store.list_open(account.id)

    def _impact(i):
        return abs(i.diff_abs or 0.0)

    items.sort(key=lambda i: (_STATE_RANK.get(i.state, 9), -_impact(i), -len(i.source_job_ids)))
    out = []
    for i in items:
        d = i.model_dump(mode="json")
        d["recurrence"] = len(i.source_job_ids)
        d["impact"] = round(_impact(i), 2)
        d["new_this_job"] = bool(job_id) and (job_id in i.source_job_ids) and len(i.source_job_ids) == 1
        out.append(d)
    return _clean({"items": out, "count": len(out)})


@app.post("/api/triage/{item_id}/resolve")
def resolve_triage(item_id: str, payload: dict, account: Account = Depends(require_account)):
    """Resolve a triage item. action ∈ {mark_expected, investigate, add_rule}.

    - mark_expected: stops the signature re-surfacing in future inboxes.
    - investigate:   defers it (stays visible, lower priority).
    - add_rule:      pins the signature to a status going forward (active rule).

    Every action also appends to the decision log so the override-rate metric
    and the rule proposer can learn from it.
    """
    action = (payload or {}).get("action", "")
    if action not in ("mark_expected", "investigate", "add_rule"):
        raise HTTPException(status_code=400, detail="action must be mark_expected | investigate | add_rule")
    user_reason = (payload or {}).get("user_reason")
    item = triage_store.get(account.id, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Triage item not found")

    created_rule = None
    if action == "add_rule":
        forced_status = (payload or {}).get("status") or (
            item.status if item.status in ("match", "minor", "major", "fee_offset") else "match"
        )
        rule = Rule(
            account_id=account.id, kind="force_status",
            description=((payload or {}).get("description")
                         or f"Always treat {item.signature[:8]}… as '{forced_status}'"),
            # Ceiling: 3× the diff the rule was taught on, floor $50 — a rule
            # learned on fee noise must never swallow a big discrepancy.
            when={"signature_prefix": item.signature,
                  "max_abs_diff": round(max(3 * abs(item.diff_abs or 0.0), 50.0), 2)},
            then={"status": forced_status},
            origin="user_rule", user_origin_text=user_reason,
            confidence=0.9, state="active",
        )
        rules_store.add_rule(account.id, rule)
        created_rule = rule.model_dump(mode="json")
        resolved = triage_store.resolve(account.id, item_id, action="accept",
                                        user_reason=user_reason, rule_id=rule.id)
    else:
        resolved = triage_store.resolve(account.id, item_id, action=action, user_reason=user_reason)

    decision_log.append(account.id, DecisionLogEntry(
        job_id=(item.source_job_ids[-1] if item.source_job_ids else None),
        row_key=item.row_key, signature=item.signature,
        original_status=item.status,
        user_status=("expected" if action in ("mark_expected", "add_rule") else "investigate"),
        user_reason=user_reason,
    ))
    proposed = rule_proposer.propose_from_decisions(account.id)
    return _clean({
        "resolved": resolved.model_dump(mode="json") if resolved else None,
        "created_rule": created_rule,
        "proposed_rules": [r.model_dump(mode="json") for r in proposed],
    })


@app.get("/api/rules")
def list_rules(account: Account = Depends(require_account)):
    rules = rules_store.load_rules(account.id)

    def grp(states):
        return [r.model_dump(mode="json") for r in rules if r.state in states]

    return {"active": grp(("active",)), "pending": grp(("pending",)), "revoked": grp(("revoked",))}


@app.get("/api/rules/{rule_id}/preview")
def preview_rule(rule_id: str, account: Account = Depends(require_account)):
    """What would this rule have done across recent jobs? Shown before Accept
    so the user sees the blast radius of a force_status rule."""
    from .models import Rationale
    rules = rules_store.load_rules(account.id)
    rule = next((r for r in rules if r.id == rule_id), None)
    if rule is None or rule.kind != "force_status":
        raise HTTPException(status_code=404, detail="force_status rule not found")
    prefix = rule.when.get("signature_prefix") or ""
    affected, total = [], 0.0
    if prefix:
        for meta in storage.list_jobs(account.id, limit=10):
            job = storage.load_job(meta["job_id"]) or {}
            for row in job.get("matched", []):
                rat = row.get("rationale") or {}
                try:
                    sig = triage_store.signature_for_matched(Rationale.model_validate(rat), row)
                except Exception:
                    continue
                if sig.startswith(prefix):
                    affected.append({"job_id": meta["job_id"], "key": row.get("key"),
                                     "status": rat.get("status"), "diff_abs": row.get("diff_abs")})
                    total += abs(row.get("diff_abs") or 0.0)
    return _clean({"rule_id": rule_id, "affected_count": len(affected),
                   "total_abs_diff": round(total, 2), "rows": affected[:50],
                   "max_abs_diff_ceiling": rule.when.get("max_abs_diff")})


@app.post("/api/rules/{rule_id}/accept")
def accept_rule(rule_id: str, account: Account = Depends(require_account)):
    r = rules_store.update_rule(account.id, rule_id, {"state": "active"})
    if r is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    return r.model_dump(mode="json")


@app.post("/api/rules/{rule_id}/revoke")
def revoke_rule_endpoint(rule_id: str, account: Account = Depends(require_account)):
    """Revoke an active rule or reject a pending one (both → state=revoked)."""
    if not rules_store.revoke_rule(account.id, rule_id):
        raise HTTPException(status_code=404, detail="Rule not found")
    return {"ok": True, "rule_id": rule_id, "state": "revoked"}


@app.post("/api/decisions")
def record_decision(payload: dict, account: Account = Depends(require_account)):
    """Drawer-driven decision capture. The user marks a matched row expected or
    overrides its status with a free-text reason. We recompute the row's
    signature from the job so this lands on the same signature the inbox uses."""
    job_id = (payload or {}).get("job_id")
    row_key = (payload or {}).get("row_key")
    user_status = (payload or {}).get("user_status") or "expected"
    user_reason = (payload or {}).get("user_reason")
    original_status = (payload or {}).get("original_status")
    signature = (payload or {}).get("signature")

    if job_id and row_key and not signature:
        job = storage.load_job(job_id)
        if job and job.get("account_id") in (None, account.id):
            from .models import Rationale
            for row in job.get("matched", []):
                if str(row.get("key")) == str(row_key):
                    rat = row.get("rationale") or {}
                    try:
                        signature = triage_store.signature_for_matched(Rationale.model_validate(rat), row)
                        original_status = original_status or rat.get("status")
                    except Exception:
                        pass
                    break

    decision_log.append(account.id, DecisionLogEntry(
        job_id=job_id, row_key=row_key, signature=signature,
        original_status=original_status, user_status=user_status, user_reason=user_reason,
    ))

    resolved = None
    if signature and user_status == "expected":
        items = triage_store.load_all(account.id)
        match = triage_store.find_by_signature(items, signature)
        if match is not None:
            resolved = triage_store.resolve(account.id, match.id,
                                            action="mark_expected", user_reason=user_reason)
    proposed = rule_proposer.propose_from_decisions(account.id)
    return _clean({
        "ok": True, "signature": signature, "resolved_triage": bool(resolved),
        "proposed_rules": [r.model_dump(mode="json") for r in proposed],
    })


@app.get("/api/accounts/me/notes")
def list_notes(account: Account = Depends(require_account)):
    return {"notes": notes_store.all_notes(account.id)}


@app.post("/api/accounts/me/notes")
def add_note(payload: dict, account: Account = Depends(require_account)):
    """Drop-in note or onboarding intake answer. Runs extract_from_text so the
    UI can show parsed proposals for confirmation."""
    text = ((payload or {}).get("text") or "").strip()
    kind = (payload or {}).get("kind", "note")
    if kind not in ("intake", "note", "justification"):
        kind = "note"
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    parsed = extract_from_text(text=text, account_id=account.id, context_kind=kind)
    entry = notes_store.append(account.id, text, kind=kind, parsed_proposals=parsed)
    return _clean({"entry": entry, "proposals": parsed})


@app.post("/api/accounts/me/notes/confirm")
def confirm_proposals(payload: dict, account: Account = Depends(require_account)):
    """Apply the proposals the user kept: aliases → learned_aliases,
    rule_proposals → pending rules, brand_facts → observations."""
    proposals = (payload or {}).get("proposals") or {}
    origin_text = (payload or {}).get("source_text")
    applied = {"aliases": 0, "rules": 0, "facts": 0}

    for a in proposals.get("alias_proposals", []):
        cid, text = a.get("concept_id"), a.get("text")
        if cid and text:
            learned_aliases.upsert(account.id, text, cid)
            applied["aliases"] += 1

    for rp in proposals.get("rule_proposals", []):
        rule = Rule(
            account_id=account.id, kind="custom",
            description=rp.get("description", ""),
            when={"freeform_type": rp.get("type", "custom")}, then={},
            origin="user_note", user_origin_text=origin_text or rp.get("description"),
            confidence=float(rp.get("confidence") or 0.6), state="pending",
        )
        rules_store.add_rule(account.id, rule)
        applied["rules"] += 1

    for f in proposals.get("brand_facts", []):
        observations_store.append(
            account.id, f.get("fact", ""),
            category="brand_fact:" + (f.get("category") or "general"),
        )
        applied["facts"] += 1

    return {"ok": True, "applied": applied}


@app.get("/api/observations")
def list_observations(account: Account = Depends(require_account)):
    return {"observations": observations_store.recent(account.id)}


@app.post("/api/observations/feedback")
def observation_feedback(payload: dict, account: Account = Depends(require_account)):
    decision_log.append(account.id, DecisionLogEntry(
        user_status="observation_wrong",
        user_reason=(payload or {}).get("text"),
    ))
    return {"ok": True}


@app.get("/api/metrics/series")
def metrics_series(limit: int = 12, account: Account = Depends(require_account)):
    s = metrics_store.series(account.id, limit=limit)
    return _clean({"series": [m.model_dump(mode="json") for m in s]})


@app.get("/api/compare/{job_id}/{prev_job_id}")
def compare_jobs(job_id: str, prev_job_id: str, account: Account = Depends(require_account)):
    """'What changed since last time' — diff two jobs by TriageItem signature."""
    cur = _load_job_for_account(job_id, account)
    prev = _load_job_for_account(prev_job_id, account)
    from .models import Rationale

    def sigs(job):
        out = {}
        for row in job.get("discrepancies", []):
            rat = row.get("rationale") or {}
            try:
                sig = triage_store.signature_for_matched(Rationale.model_validate(rat), row)
            except Exception:
                continue
            out[sig] = {"row_key": row.get("key"), "status": rat.get("status"),
                        "diff_abs": row.get("diff_abs")}
        key_cols = {"a": job.get("key_col_a"), "b": job.get("key_col_b")}
        for side, key in (("a", "unmatched_a"), ("b", "unmatched_b")):
            for row in job.get(key, []):
                if "_expected_by_rule" in row:
                    continue
                sig = triage_store.signature_for_unmatched(side, row, key_col=key_cols[side])
                out[sig] = {"row_key": str(triage_store._first_key_value(row, key_col=key_cols[side])),
                            "status": f"unmatched_{side}"}
        return out

    cur_s, prev_s = sigs(cur), sigs(prev)
    new = [{"signature": s, **v} for s, v in cur_s.items() if s not in prev_s]
    resolved = [{"signature": s, **v} for s, v in prev_s.items() if s not in cur_s]
    persisting = [{"signature": s, **v} for s, v in cur_s.items() if s in prev_s]
    return _clean({
        "job_id": job_id, "prev_job_id": prev_job_id,
        "new": new, "resolved": resolved, "persisting": persisting,
        "summary_current": cur.get("summary"), "summary_prev": prev.get("summary"),
    })
