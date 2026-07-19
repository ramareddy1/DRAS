import { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { getExportToken, getResults } from "../api/client.js";
import DataTable from "../components/DataTable.jsx";
import { saveHistoryItem, loadHistory } from "../history.js";

const RULE_SRC = /^(rule:|stripe_fee|paypal_fee)/;

function AuditBand({ data, onAudit }) {
  const ruleRows = (data.matched || []).filter((r) =>
    (r.rationale?.rationale || []).some((e) => RULE_SRC.test(e.source))
  );
  const expectedUnmatched = (data.expected_unmatched_a || 0) + (data.expected_unmatched_b || 0);
  if (ruleRows.length === 0 && expectedUnmatched === 0) return null;

  // distinct rule labels
  const labels = {};
  for (const r of ruleRows) {
    const lbl = r.fee_pattern || "a rule";
    labels[lbl] = (labels[lbl] || 0) + 1;
  }
  const labelText = Object.entries(labels)
    .map(([l, n]) => `${n}× ${l}`)
    .slice(0, 4)
    .join(" · ");

  return (
    <button
      onClick={onAudit}
      className="w-full text-left bg-green-50 border border-green-200 rounded-lg px-4 py-3 mb-4 hover:bg-green-100/70"
    >
      <div className="text-sm font-medium text-green-900">
        {ruleRows.length} row{ruleRows.length === 1 ? "" : "s"} handled silently by your rules
        {expectedUnmatched > 0 && ` · ${expectedUnmatched} expected-unmatched suppressed`}
      </div>
      {labelText && <div className="text-xs text-green-800/80 mt-0.5">{labelText}</div>}
      <div className="text-[11px] text-green-700/70 mt-0.5">Click to audit these in the Matched tab →</div>
    </button>
  );
}

function Stat({ label, value, sub, tone }) {
  const toneCls = {
    good: "border-good text-good",
    warn: "border-warn text-yellow-700",
    bad: "border-bad text-bad",
    neutral: "border-slate-300 text-slate-800",
  }[tone || "neutral"];
  return (
    <div className={`border-l-4 ${toneCls} bg-white rounded shadow-sm p-3`}>
      <div className="text-xs text-slate-500 uppercase tracking-wide">{label}</div>
      <div className="text-2xl font-semibold mt-1">{value}</div>
      {sub && <div className="text-xs text-slate-500 mt-0.5">{sub}</div>}
    </div>
  );
}

const TABS = [
  { id: "matched", label: "Matched" },
  { id: "discrepancies", label: "Discrepancies" },
  { id: "unmatched_a", label: "Unmatched (A only)" },
  { id: "unmatched_b", label: "Unmatched (B only)" },
];

export default function ResultsPage() {
  const { id } = useParams();
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [tab, setTab] = useState("discrepancies");

  const reload = () => getResults(id).then(setData).catch((e) => setError(e.message));

  useEffect(() => {
    getResults(id).then((d) => {
      setData(d);
      saveHistoryItem({
        job_id: d.job_id,
        created_at: d.created_at,
        recon_type: d.config?.recon_type,
        file_a: d.filenames?.a,
        file_b: d.filenames?.b,
        label_a: d.config?.label_a,
        label_b: d.config?.label_b,
        match_rate: d.summary?.matched_pct,
        discrepancy_value: d.summary?.total_discrepancy_value,
      });
    }).catch((e) => setError(e.message));
  }, [id]);

  if (error) return <div className="text-bad">Error: {error}</div>;
  if (!data) return <div className="text-slate-500">Loading…</div>;

  const s = data.summary;
  const matchTone = s.matched_pct >= 95 ? "good" : s.matched_pct >= 80 ? "warn" : "bad";
  const labelA = data.config.label_a;
  const labelB = data.config.label_b;

  const tabRows = {
    matched: data.matched,
    discrepancies: data.discrepancies,
    unmatched_a: data.unmatched_a,
    unmatched_b: data.unmatched_b,
  }[tab];

  const statusCol = (tab === "matched" || tab === "discrepancies") ? "status" : null;

  // Most recent *other* job in history → offer a "what changed" comparison.
  const prevJob = loadHistory().find((h) => h.job_id && h.job_id !== id);

  return (
    <div>
      <div className="flex items-baseline justify-between mb-4 flex-wrap gap-2">
        <div>
          <Link to="/" className="text-sm text-brand hover:underline">← New reconciliation</Link>
          <h1 className="text-2xl font-semibold text-navy mt-1">
            {labelA} ↔ {labelB}
          </h1>
          <p className="text-xs text-slate-500 mt-0.5">
            Job {data.job_id} · {new Date(data.created_at).toLocaleString()}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Link to="/inbox" className="text-sm text-brand hover:underline px-2">Inbox →</Link>
          {prevJob && (
            <Link
              to={`/results/${id}/compare/${prevJob.job_id}`}
              className="border border-slate-300 text-slate-700 px-3 py-2 rounded text-sm hover:bg-slate-50"
            >
              What changed
            </Link>
          )}
          <button
            onClick={async () => {
              const { token } = await getExportToken(data.job_id);
              window.location.href =
                `/api/results/${data.job_id}/export?token=${encodeURIComponent(token)}`;
            }}
            className="bg-navy text-white px-4 py-2 rounded text-sm font-medium hover:bg-brand"
          >
            Download Excel report
          </button>
        </div>
      </div>

      {data.binding_warning && (
        <div className="mb-4 rounded-md bg-amber-50 border border-amber-200 px-4 py-3 text-sm text-amber-800">
          <span className="font-semibold">Check the join: </span>
          {data.binding_warning.message}
        </div>
      )}

      <AuditBand data={data} onAudit={() => setTab("matched")} />

      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3 mb-6">
        <Stat label={`Total ${labelA}`} value={s.total_a.toLocaleString()}
              sub={`$${s.total_amount_a.toLocaleString()}`} />
        <Stat label={`Total ${labelB}`} value={s.total_b.toLocaleString()}
              sub={`$${s.total_amount_b.toLocaleString()}`} />
        <Stat label="Matched" value={`${s.matched_pct}%`}
              sub={`${s.matched.toLocaleString()} records${s.fuzzy_matches ? ` (${s.fuzzy_matches} fuzzy)` : ""}`}
              tone={matchTone} />
        <Stat label={`Unmatched in ${labelA}`} value={s.unmatched_a.toLocaleString()} tone={s.unmatched_a ? "warn" : "good"} />
        <Stat label={`Unmatched in ${labelB}`} value={s.unmatched_b.toLocaleString()} tone={s.unmatched_b ? "warn" : "good"} />
        <Stat label="Discrepancy $" value={`$${s.total_discrepancy_value.toLocaleString()}`}
              sub={`${s.discrepancies} records`} tone={s.discrepancies ? "bad" : "good"} />
      </div>

      <div className="border-b border-slate-200 mb-3 flex gap-1 flex-wrap">
        {TABS.map((t) => {
          const count = {
            matched: s.matched, discrepancies: s.discrepancies,
            unmatched_a: s.unmatched_a, unmatched_b: s.unmatched_b,
          }[t.id];
          return (
            <button key={t.id} onClick={() => setTab(t.id)}
              className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px ${
                tab === t.id ? "border-brand text-brand" : "border-transparent text-slate-600 hover:text-slate-800"
              }`}>
              {t.label} <span className="text-slate-400">({count})</span>
            </button>
          );
        })}
      </div>

      <DataTable
        rows={tabRows || []}
        statusColumn={statusCol}
        rowHasRationale={tab === "matched" || tab === "discrepancies"}
        jobId={data.job_id}
        onDecision={reload}
      />

      <section className="mt-8 bg-white border border-slate-200 rounded-lg p-5">
        <h2 className="text-lg font-semibold text-navy mb-2">AI insights</h2>
        {data.insights_status === "unavailable" ? (
          <div className="rounded-md bg-amber-50 border border-amber-200 px-4 py-3 text-sm text-amber-800">
            AI summary unavailable for this run — the matching and
            classification above are complete and deterministic.
          </div>
        ) : (
          <div className="prose prose-sm max-w-none whitespace-pre-wrap text-slate-700">
            {data.insights}
          </div>
        )}
        {data.timing && (
          <div className="mt-4 text-xs text-slate-500">
            Timing: avg {data.timing.mean_days}d delta · range {data.timing.min_days} to {data.timing.max_days}d · {data.timing.outliers} outliers
          </div>
        )}
      </section>
    </div>
  );
}
