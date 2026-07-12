import { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { compareJobs } from "../api/client.js";

function money(v) {
  if (v == null) return "—";
  return `$${Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

function Bucket({ title, items, tone, empty }) {
  const dot = { good: "bg-good", bad: "bg-bad", neutral: "bg-slate-400" }[tone] || "bg-slate-400";
  return (
    <section className="bg-white border border-slate-200 rounded-lg p-4">
      <h2 className="text-sm font-semibold text-slate-800 mb-2 flex items-center gap-2">
        <span className={`w-2 h-2 rounded-full ${dot}`} />
        {title} <span className="text-slate-400 font-normal">({items.length})</span>
      </h2>
      {items.length === 0 ? (
        <p className="text-xs text-slate-400">{empty}</p>
      ) : (
        <ul className="space-y-1">
          {items.map((it, i) => (
            <li key={i} className="text-sm flex items-center gap-2">
              <span className="font-mono text-slate-700">{it.row_key || "(no key)"}</span>
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-600">{it.status}</span>
              {it.diff_abs != null && <span className="text-xs text-slate-500">{money(it.diff_abs)}</span>}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export default function ComparePage() {
  const { id, prevId } = useParams();
  const [data, setData] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    compareJobs(id, prevId).then(setData).catch((e) => setError(e.message));
  }, [id, prevId]);

  if (error) return <div className="text-bad">Error: {error}</div>;
  if (!data) return <div className="text-slate-500">Loading…</div>;

  const cur = data.summary_current || {};
  const prev = data.summary_prev || {};

  return (
    <div className="max-w-4xl">
      <Link to={`/results/${id}`} className="text-sm text-brand hover:underline">← Back to results</Link>
      <h1 className="text-2xl font-semibold text-navy mt-1 mb-1">What changed</h1>
      <p className="text-sm text-slate-600 mb-6">
        Comparing this run against a previous one, matched by the signature of each gap —
        so "the same kind of problem" is tracked even on different rows.
      </p>

      <div className="grid grid-cols-2 gap-3 mb-6">
        <div className="bg-white border border-slate-200 rounded p-3">
          <div className="text-[10px] uppercase tracking-wide text-slate-400">Previous</div>
          <div className="text-sm text-slate-700">
            {prev.matched_pct}% matched · {money(prev.total_discrepancy_value)} discrepancy
          </div>
        </div>
        <div className="bg-white border border-slate-200 rounded p-3">
          <div className="text-[10px] uppercase tracking-wide text-slate-400">This run</div>
          <div className="text-sm text-slate-700">
            {cur.matched_pct}% matched · {money(cur.total_discrepancy_value)} discrepancy
          </div>
        </div>
      </div>

      <div className="space-y-3">
        <Bucket title="Resolved since last time" items={data.resolved} tone="good"
          empty="Nothing from last run has gone away." />
        <Bucket title="New this run" items={data.new} tone="bad"
          empty="No new kinds of gaps appeared." />
        <Bucket title="Still present" items={data.persisting} tone="neutral"
          empty="No recurring gaps." />
      </div>
    </div>
  );
}
