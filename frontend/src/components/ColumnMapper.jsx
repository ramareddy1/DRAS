/**
 * Phase 1: per-column semantic binding UI.
 *
 * For each column, render a row with the inferred concept binding + confidence
 * pill, an override dropdown (one-click confirm), and a sample-value preview.
 */
function ConfidencePill({ confidence, provenance }) {
  const tone =
    provenance === "user_confirmed" ? "bg-blue-100 text-blue-800"
    : confidence >= 0.85 ? "bg-green-100 text-green-800"
    : confidence >= 0.6  ? "bg-yellow-100 text-yellow-800"
    : "bg-red-100 text-red-800";
  const label =
    provenance === "user_confirmed" ? "confirmed"
    : `${Math.round(confidence * 100)}%`;
  return <span className={`inline-block px-1.5 py-0.5 rounded text-[10px] font-medium ${tone}`}>{label}</span>;
}

export default function ColumnMapper({ label, preview, bindings, concepts, onChange }) {
  if (!preview) return null;
  const cols = preview.columns;
  const conceptIds = concepts.map((c) => c.id);

  function bindingFor(col) {
    return bindings.find((b) => b.column_name === col) || null;
  }

  function setConcept(col, conceptId) {
    const next = bindings.filter((b) => b.column_name !== col);
    if (conceptId) {
      const existing = bindingFor(col);
      next.push({
        column_name: col,
        concept_id: conceptId,
        confidence: 1.0,
        provenance: "user_confirmed",
        evidence: existing?.evidence ?? [],
        alternatives: existing?.alternatives ?? [],
      });
    }
    onChange(next);
  }

  function sampleValues(col) {
    return preview.rows
      .map((r) => r[col])
      .filter((v) => v !== null && v !== undefined && v !== "")
      .slice(0, 3)
      .map((v) => String(v));
  }

  return (
    <div className="border border-slate-200 rounded-lg p-4 bg-white">
      <div className="flex items-baseline justify-between mb-3">
        <h4 className="font-medium text-slate-800">{label}</h4>
        <span className="text-xs text-slate-500">
          {preview.row_count.toLocaleString()} rows · {cols.length} columns
        </span>
      </div>

      <div className="border border-slate-200 rounded overflow-hidden">
        <table className="text-sm min-w-full">
          <thead className="bg-slate-100 text-xs uppercase text-slate-600">
            <tr>
              <th className="px-3 py-2 text-left font-medium">Column</th>
              <th className="px-3 py-2 text-left font-medium">Concept</th>
              <th className="px-3 py-2 text-left font-medium">Sample values</th>
            </tr>
          </thead>
          <tbody>
            {cols.map((col) => {
              const b = bindingFor(col);
              const isInferred = b && b.provenance === "inferred";
              return (
                <tr key={col} className="border-t border-slate-200">
                  <td className="px-3 py-2 font-medium text-slate-800 whitespace-nowrap">{col}</td>
                  <td className="px-3 py-2">
                    <div className="flex items-center gap-2">
                      <select
                        value={b?.concept_id || ""}
                        onChange={(e) => setConcept(col, e.target.value || null)}
                        className="border border-slate-300 rounded px-2 py-1 text-sm bg-white min-w-[200px]"
                      >
                        <option value="">— unbound —</option>
                        {conceptIds.map((c) => <option key={c} value={c}>{c}</option>)}
                      </select>
                      {b && <ConfidencePill confidence={b.confidence} provenance={b.provenance} />}
                      {isInferred && (
                        <button
                          onClick={() => setConcept(col, b.concept_id)}
                          className="text-xs text-brand hover:underline"
                          title="Confirm this binding"
                        >
                          confirm
                        </button>
                      )}
                    </div>
                    {b?.evidence?.length > 0 && (
                      <div className="text-[11px] text-slate-500 mt-1">
                        {b.evidence.slice(0, 2).join(" · ")}
                      </div>
                    )}
                  </td>
                  <td className="px-3 py-2 text-xs text-slate-600">
                    {sampleValues(col).join(" · ") || <span className="text-slate-400">(empty)</span>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
