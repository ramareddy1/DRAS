import { Link } from "react-router-dom";
import { loadHistory, clearHistory } from "../history.js";
import { useState } from "react";

export default function HistoryPage() {
  const [items, setItems] = useState(loadHistory());

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
        <button
          onClick={() => { clearHistory(); setItems([]); }}
          className="text-xs text-slate-500 hover:text-bad"
        >Clear</button>
      </div>
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
