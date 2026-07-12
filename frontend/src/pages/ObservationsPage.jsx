import { useEffect, useState } from "react";
import { getObservations, flagObservation } from "../api/client.js";

export default function ObservationsPage() {
  const [items, setItems] = useState(null);
  const [error, setError] = useState("");
  const [flagged, setFlagged] = useState(new Set());

  useEffect(() => {
    getObservations().then((d) => setItems(d.observations || [])).catch((e) => setError(e.message));
  }, []);

  const flag = async (idx, text) => {
    try {
      await flagObservation(text);
      setFlagged((s) => new Set(s).add(idx));
    } catch { /* swallow — feedback is best-effort */ }
  };

  if (error) return <div className="text-bad">Error: {error}</div>;
  if (items === null) return <div className="text-slate-500">Loading…</div>;

  return (
    <div className="max-w-3xl">
      <h1 className="text-2xl font-semibold text-navy mb-1">Observations</h1>
      <p className="text-sm text-slate-600 mb-6">
        Things I've noticed about your data over time. If I'm wrong about one, tell me — it helps me learn.
      </p>

      {items.length === 0 ? (
        <p className="text-sm text-slate-400">
          No observations yet. As I see patterns across reconciliations, I'll note them here.
        </p>
      ) : (
        <div className="space-y-2">
          {items.map((o, i) => (
            <div key={i} className="bg-white border border-slate-200 rounded-lg p-4 flex items-start justify-between gap-3">
              <div>
                <div className="text-[10px] uppercase tracking-wide text-slate-400">
                  {o.category} · {o.at ? new Date(o.at).toLocaleString() : ""}
                </div>
                <div className="text-sm text-slate-700 mt-0.5">{o.text}</div>
              </div>
              {flagged.has(i) ? (
                <span className="text-[10px] text-slate-400 shrink-0">flagged · thanks</span>
              ) : (
                <button onClick={() => flag(i, o.text)}
                  className="text-[10px] text-slate-400 hover:text-bad shrink-0">mark wrong</button>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
