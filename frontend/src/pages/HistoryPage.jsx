import { Link } from "react-router-dom";
import { useEffect, useState } from "react";
import { loadHistory, clearHistory } from "../history.js";
import { getJobs } from "../api/client.js";

function fromServer(j) {
  return {
    job_id: j.job_id,
    created_at: j.created_at,
    recon_type: j.recon_type,
    file_a: (j.filenames || {}).a,
    file_b: (j.filenames || {}).b,
    match_rate: j.matched_pct,
    discrepancy_value: j.total_discrepancy_value,
  };
}

export default function HistoryPage() {
  const [items, setItems] = useState(null); // null = loading
  const [fromLocal, setFromLocal] = useState(false);

  useEffect(() => {
    // Server history is the source of truth (survives cleared browser
    // storage); localStorage is only a fallback when the API is unreachable.
    getJobs()
      .then((d) => setItems((d.jobs || []).map(fromServer)))
      .catch(() => {
        setFromLocal(true);
        setItems(loadHistory());
      });
  }, []);

  if (items === null) {
    return <div className="text-center py-12 text-slate-400">Loading…</div>;
  }

  if (!items.length) {
    return (
      <div className="text-center py-12 text-slate-500">
        <p>No reconciliations yet.</p>
        <Link to="/" className="text-brand hover:underline mt-2 inline-block">Start a new one →</Link>
      </div>
    );
  }

  return (
    <div>
      <div className="flex justify-between items-baseline mb-4">
        <h1 className="text-2xl font-semibold text-navy">History</h1>
        {fromLocal && (
          <button
            onClick={() => { clearHistory(); setItems([]); }}
            className="text-xs text-slate-500 hover:text-bad"
          >Clear</button>
        )}
      </div>
      {fromLocal && (
        <p className="text-xs text-amber-700 mb-3">
          Server unreachable — showing this browser's local history.
        </p>
      )}
      <div className="bg-white border border-slate-200 rounded-lg overflow-hidden">
        <table className="min-w-full text-sm">
          <thead className="bg-slate-100">
            <tr>
              <th className="px-4 py-2 text-left font-medium">Date</th>
              <th className="px-4 py-2 text-left font-medium">Type</th>
              <th className="px-4 py-2 text-left font-medium">Files</th>
              <th className="px-4 py-2 text-right font-medium">Match rate</th>
              <th className="px-4 py-2 text-right font-medium">Discrepancy $</th>
              <th className="px-4 py-2"></th>
            </tr>
          </thead>
          <tbody>
            {items.map((it) => (
              <tr key={it.job_id} className="border-t border-slate-200">
                <td className="px-4 py-2 text-slate-600">
                  {new Date(it.created_at).toLocaleString()}
                </td>
                <td className="px-4 py-2">{it.recon_type}</td>
                <td className="px-4 py-2 text-xs text-slate-500">
                  {it.file_a} ↔ {it.file_b}
                </td>
                <td className="px-4 py-2 text-right">
                  {it.match_rate != null ? `${it.match_rate}%` : "—"}
                </td>
                <td className="px-4 py-2 text-right">
                  {it.discrepancy_value != null ? `$${Number(it.discrepancy_value).toLocaleString()}` : "—"}
                </td>
                <td className="px-4 py-2 text-right">
                  <Link to={`/results/${it.job_id}`} className="text-brand hover:underline">Open</Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
