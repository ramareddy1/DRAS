import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { getInbox, resolveTriage } from "../api/client.js";

const STATE_TONE = {
  open: "bg-blue-100 text-blue-800",
  recurring: "bg-amber-100 text-amber-800",
  deferred: "bg-slate-100 text-slate-600",
};
const STATUS_TONE = {
  fee_offset: "bg-yellow-100 text-yellow-800",
  minor: "bg-yellow-100 text-yellow-800",
  major: "bg-red-100 text-red-800",
  unmatched_a: "bg-purple-100 text-purple-800",
  unmatched_b: "bg-purple-100 text-purple-800",
};

function money(v) {
  if (v == null) return "—";
  return `$${Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

function TriageRow({ item, onResolved }) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  const act = async (action) => {
    setBusy(true); setMsg("");
    try {
      await resolveTriage(item.id, { action, user_reason: reason || undefined });
      onResolved(item.id);
    } catch (e) {
      setMsg(e.message);
      setBusy(false);
    }
  };

  return (
    <div className="bg-white border border-slate-200 rounded-lg">
      <button onClick={() => setOpen((v) => !v)} className="w-full text-left px-4 py-3 flex items-center gap-3">
        <span className={`px-2 py-0.5 rounded text-[10px] font-medium ${STATE_TONE[item.state] || "bg-slate-100"}`}>{item.state}</span>
        <span className={`px-2 py-0.5 rounded text-[10px] font-medium ${STATUS_TONE[item.status] || "bg-slate-100 text-slate-700"}`}>{item.status}</span>
        <span className="font-mono text-sm text-slate-800">{item.row_key || "(no key)"}</span>
        {item.fee_pattern && <span className="text-xs text-slate-500">{item.fee_pattern}</span>}
        <span className="ml-auto text-sm font-medium text-slate-700">{item.impact ? money(item.impact) : ""}</span>
        {item.recurrence > 1 && <span className="text-[10px] text-amber-700">seen {item.recurrence}×</span>}
        {item.new_this_job && <span className="text-[10px] text-good">new</span>}
        <span className="text-slate-400 text-xs">{open ? "▲" : "▼"}</span>
      </button>

      {open && (
        <div className="border-t border-slate-200 px-4 py-3">
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs mb-3">
            {item.amount_a != null && <div><span className="text-slate-500">A:</span> {money(item.amount_a)}</div>}
            {item.amount_b != null && <div><span className="text-slate-500">B:</span> {money(item.amount_b)}</div>}
            {item.diff_abs != null && <div><span className="text-slate-500">Δ:</span> {money(item.diff_abs)}</div>}
            <div><span className="text-slate-500">side:</span> {item.side}</div>
          </div>

          {item.rationale?.rationale?.length > 0 && (
            <ul className="mb-3 space-y-1">
              {item.rationale.rationale.map((e, i) => (
                <li key={i} className="text-xs text-slate-600 border-l-2 border-slate-200 pl-2">
                  <span className="font-mono text-brand">{e.source}</span> — {e.evidence}
                </li>
              ))}
            </ul>
          )}

          <textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Why is this expected? (becomes the rule's reason if this recurs)"
            className="w-full border border-slate-300 rounded px-2 py-1.5 text-sm resize-y min-h-[44px] mb-2 outline-none focus:border-brand"
          />
          {msg && <p className="text-xs text-bad mb-2">{msg}</p>}
          <div className="flex gap-2 flex-wrap">
            <button disabled={busy} onClick={() => act("mark_expected")}
              className="text-xs px-3 py-1.5 rounded bg-good text-white hover:opacity-90 disabled:opacity-50">
              Mark expected
            </button>
            <button disabled={busy} onClick={() => act("investigate")}
              className="text-xs px-3 py-1.5 rounded bg-slate-200 text-slate-700 hover:bg-slate-300 disabled:opacity-50">
              Investigate later
            </button>
            <button disabled={busy} onClick={() => act("add_rule")}
              className="text-xs px-3 py-1.5 rounded border border-brand text-brand hover:bg-blue-50 disabled:opacity-50">
              Always treat this way (add rule)
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export default function InboxPage() {
  const [items, setItems] = useState(null);
  const [error, setError] = useState("");

  const load = () => {
    getInbox().then((d) => setItems(d.items || [])).catch((e) => setError(e.message));
  };
  useEffect(load, []);

  const onResolved = (id) => setItems((cur) => (cur || []).filter((i) => i.id !== id));

  if (error) return <div className="text-bad">Error: {error}</div>;
  if (items === null) return <div className="text-slate-500">Loading…</div>;

  return (
    <div className="max-w-4xl">
      <h1 className="text-2xl font-semibold text-navy mb-1">Inbox</h1>
      <p className="text-sm text-slate-600 mb-6">
        Cross-job triage queue. Items that recur accumulate here instead of cluttering every run.
        Teach me once — they stop surfacing.
      </p>

      {items.length === 0 ? (
        <div className="text-center py-12 text-slate-500">
          <p className="text-lg">Inbox zero. 🎉</p>
          <p className="text-sm mt-1">Nothing needs your attention right now.</p>
          <Link to="/" className="text-brand hover:underline mt-2 inline-block">Run a reconciliation →</Link>
        </div>
      ) : (
        <div className="space-y-2">
          {items.map((it) => <TriageRow key={it.id} item={it} onResolved={onResolved} />)}
        </div>
      )}
    </div>
  );
}
