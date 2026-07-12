/**
 * Phase 5: side drawer that displays a Rationale and captures the user's
 * verdict. The free-text `user_reason` now posts to /api/decisions — which
 * logs the correction, resolves any matching triage item, and (after enough
 * repeats) lets the rule proposer memorize it.
 */
import { useState } from "react";
import { recordDecision } from "../api/client.js";

function StatusPill({ status, confidence }) {
  const tone = {
    match: "bg-green-100 text-green-800",
    minor: "bg-yellow-100 text-yellow-800",
    fee_offset: "bg-yellow-100 text-yellow-800",
    major: "bg-red-100 text-red-800",
  }[status] || "bg-slate-100 text-slate-700";
  return (
    <span className="inline-flex items-baseline gap-2">
      <span className={`px-2 py-0.5 rounded text-xs font-medium ${tone}`}>{status}</span>
      {confidence != null && (
        <span className="text-xs text-slate-500">{Math.round(confidence * 100)}% confidence</span>
      )}
    </span>
  );
}

function EvidenceRow({ source, evidence, weight }) {
  return (
    <li className="border-l-2 border-slate-200 pl-3 py-1">
      <div className="text-xs font-mono text-brand">{source}</div>
      <div className="text-sm text-slate-700">{evidence}</div>
      {weight != null && weight !== 1.0 && (
        <div className="text-[10px] text-slate-400 mt-0.5">weight: {weight.toFixed(2)}</div>
      )}
    </li>
  );
}

function AltRow({ status, confidence, reason }) {
  return (
    <li className="text-xs text-slate-600 py-1">
      <span className="font-medium text-slate-700">{status}</span>
      <span className="text-slate-400"> · {Math.round((confidence || 0) * 100)}%</span>
      <span className="text-slate-500"> — {reason}</span>
    </li>
  );
}

export default function RationaleDrawer({ row, jobId, onClose, onDecision }) {
  if (!row) return null;
  const rat = row.rationale || {};
  const evidence = rat.rationale || [];
  const alternatives = rat.alternatives || [];

  const [reason, setReason] = useState(rat.user_reason || "");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState("");
  const [err, setErr] = useState("");

  const submit = async (userStatus) => {
    setBusy(true); setErr(""); setDone("");
    try {
      const res = await recordDecision({
        job_id: jobId,
        row_key: rat.row_key || row.key,
        original_status: rat.status || row.status,
        user_status: userStatus,
        user_reason: reason || undefined,
      });
      setDone(
        userStatus === "expected"
          ? (res.resolved_triage ? "Marked expected — it won't surface again." : "Noted as expected.")
          : "Flagged to investigate."
      );
      onDecision && onDecision(res);
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div onClick={onClose} className="fixed inset-0 bg-slate-900/30 z-40" aria-hidden="true" />
      <aside
        className="fixed top-0 right-0 h-full w-full sm:w-[480px] bg-white shadow-xl z-50 overflow-y-auto"
        role="dialog"
        aria-label="Row rationale"
      >
        <div className="border-b border-slate-200 px-5 py-4 flex items-start justify-between">
          <div>
            <div className="text-xs text-slate-500 uppercase tracking-wide">Row</div>
            <div className="font-mono text-sm text-slate-800">{rat.row_key || row.key}</div>
            <div className="mt-2">
              <StatusPill status={rat.status || row.status} confidence={rat.confidence} />
            </div>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-700 text-xl leading-none" aria-label="Close">×</button>
        </div>

        <div className="px-5 py-3 border-b border-slate-200 grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
          {row.amount_a != null && (
            <div><span className="text-slate-500">A:</span> <span className="font-mono">{row.amount_a.toLocaleString?.() ?? row.amount_a}</span></div>
          )}
          {row.amount_b != null && (
            <div><span className="text-slate-500">B:</span> <span className="font-mono">{row.amount_b.toLocaleString?.() ?? row.amount_b}</span></div>
          )}
          {row.diff_abs != null && (
            <div><span className="text-slate-500">Δ:</span> <span className="font-mono">{row.diff_abs}</span> <span className="text-slate-400">({row.diff_pct}%)</span></div>
          )}
          {row.match_type && (
            <div><span className="text-slate-500">Match:</span> {row.match_type}</div>
          )}
          {row.delta_days != null && (
            <div><span className="text-slate-500">Delay:</span> {row.delta_days}d</div>
          )}
          {row.fee_pattern && (
            <div className="col-span-2"><span className="text-slate-500">Fee shape:</span> {row.fee_pattern}</div>
          )}
        </div>

        <section className="px-5 py-4">
          <h3 className="text-sm font-medium text-slate-800 mb-2">Why this classification</h3>
          {evidence.length === 0 ? (
            <p className="text-xs text-slate-500">No evidence recorded.</p>
          ) : (
            <ul className="space-y-2">
              {evidence.map((e, i) => <EvidenceRow key={i} {...e} />)}
            </ul>
          )}
        </section>

        {alternatives.length > 0 && (
          <section className="px-5 py-4 border-t border-slate-200">
            <h3 className="text-sm font-medium text-slate-800 mb-2">Also considered</h3>
            <ul className="space-y-1">
              {alternatives.map((a, i) => <AltRow key={i} {...a} />)}
            </ul>
          </section>
        )}

        {/* user_reason capture — live in Phase 5 */}
        <section className="px-5 py-4 border-t border-slate-200">
          <h3 className="text-sm font-medium text-slate-800 mb-1">Your note</h3>
          <p className="text-xs text-slate-500 mb-2">
            Disagree, or know why this is expected? Tell the system — it will use this on future runs.
          </p>
          <textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="e.g. Stripe standard fees · Acme always invoices full PO · subscription billing"
            className="w-full border border-slate-300 rounded px-2 py-1.5 text-sm resize-y min-h-[60px] outline-none focus:border-brand"
          />
          {done && <p className="text-xs text-good mt-2">{done}</p>}
          {err && <p className="text-xs text-bad mt-2">{err}</p>}
          <div className="flex gap-2 mt-2">
            <button disabled={busy} onClick={() => submit("expected")}
              className="text-xs px-3 py-1.5 rounded bg-good text-white hover:opacity-90 disabled:opacity-50">
              Mark expected
            </button>
            <button disabled={busy} onClick={() => submit("investigate")}
              className="text-xs px-3 py-1.5 rounded bg-slate-200 text-slate-700 hover:bg-slate-300 disabled:opacity-50">
              Investigate
            </button>
          </div>
          {!jobId && (
            <p className="text-[10px] text-slate-400 mt-2">
              (Open this row from a results page to capture a decision.)
            </p>
          )}
        </section>
      </aside>
    </>
  );
}
