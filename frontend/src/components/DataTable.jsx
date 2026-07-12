import { useMemo, useState } from "react";
import RationaleDrawer from "./RationaleDrawer.jsx";

export default function DataTable({ rows, columns, statusColumn, pageSize = 50, rowHasRationale = false, jobId, onDecision }) {
  const [sort, setSort] = useState({ key: null, dir: "asc" });
  const [filter, setFilter] = useState("");
  const [page, setPage] = useState(0);
  const [drawerRow, setDrawerRow] = useState(null);

  // Drop the synthetic `rationale` object from the visible column list — it
  // lives in the drawer, not in a column.
  const allKeys = columns || (rows[0] ? Object.keys(rows[0]) : []);
  const cols = allKeys.filter((c) => c !== "rationale");

  const filtered = useMemo(() => {
    if (!filter) return rows;
    const q = filter.toLowerCase();
    return rows.filter((r) =>
      cols.some((c) => String(r[c] ?? "").toLowerCase().includes(q))
    );
  }, [rows, filter, cols]);

  const sorted = useMemo(() => {
    if (!sort.key) return filtered;
    const dir = sort.dir === "asc" ? 1 : -1;
    return [...filtered].sort((a, b) => {
      const va = a[sort.key], vb = b[sort.key];
      if (va == null) return 1;
      if (vb == null) return -1;
      const na = Number(va), nb = Number(vb);
      if (!isNaN(na) && !isNaN(nb)) return (na - nb) * dir;
      return String(va).localeCompare(String(vb)) * dir;
    });
  }, [filtered, sort]);

  const pageRows = sorted.slice(page * pageSize, (page + 1) * pageSize);
  const totalPages = Math.max(1, Math.ceil(sorted.length / pageSize));

  function toggleSort(c) {
    setSort((s) => s.key === c ? { key: c, dir: s.dir === "asc" ? "desc" : "asc" } : { key: c, dir: "asc" });
  }

  function statusClass(v) {
    if (!statusColumn) return "";
    if (v === "match") return "bg-green-100 text-green-800";
    if (v === "minor" || v === "fee_offset") return "bg-yellow-100 text-yellow-800";
    if (v === "major") return "bg-red-100 text-red-800";
    return "";
  }

  return (
    <div>
      <div className="flex justify-between items-center mb-2 gap-3">
        <input
          value={filter}
          onChange={(e) => { setFilter(e.target.value); setPage(0); }}
          placeholder="Filter…"
          className="border border-slate-300 rounded px-2 py-1.5 text-sm w-64"
        />
        <div className="text-xs text-slate-500">
          {sorted.length.toLocaleString()} rows
        </div>
      </div>
      <div className="overflow-x-auto table-scroll border border-slate-200 rounded-lg bg-white">
        <table className="min-w-full text-sm">
          <thead className="bg-slate-100 sticky top-0">
            <tr>
              {cols.map((c) => (
                <th
                  key={c}
                  onClick={() => toggleSort(c)}
                  className="px-3 py-2 text-left font-medium text-slate-700 cursor-pointer hover:bg-slate-200 whitespace-nowrap"
                >
                  {c}
                  {sort.key === c && <span className="ml-1 text-slate-500">{sort.dir === "asc" ? "▲" : "▼"}</span>}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {pageRows.map((r, i) => {
              const clickable = rowHasRationale && r.rationale;
              return (
                <tr
                  key={i}
                  onClick={clickable ? () => setDrawerRow(r) : undefined}
                  className={`${i % 2 ? "bg-slate-50" : ""} ${clickable ? "cursor-pointer hover:bg-blue-50" : ""}`}
                >
                  {cols.map((c) => {
                    const v = r[c];
                    const isStatus = statusColumn === c;
                    return (
                      <td key={c} className="px-3 py-1.5 text-slate-700 whitespace-nowrap max-w-[280px] truncate">
                        {isStatus ? (
                          <span className={`px-2 py-0.5 rounded text-xs ${statusClass(v)}`}>{String(v ?? "")}</span>
                        ) : v == null ? "" : (typeof v === "number" ? v.toLocaleString(undefined, { maximumFractionDigits: 2 }) : String(v))}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
            {pageRows.length === 0 && (
              <tr><td colSpan={cols.length} className="px-3 py-6 text-center text-slate-500">No records</td></tr>
            )}
          </tbody>
        </table>
      </div>
      {rowHasRationale && (
        <div className="text-[11px] text-slate-400 mt-1">Click a row to see why it was classified this way.</div>
      )}
      <RationaleDrawer
        row={drawerRow}
        jobId={jobId}
        onClose={() => setDrawerRow(null)}
        onDecision={onDecision}
      />
      {totalPages > 1 && (
        <div className="flex justify-between items-center mt-2 text-sm">
          <button
            disabled={page === 0}
            onClick={() => setPage((p) => Math.max(0, p - 1))}
            className="px-3 py-1 border rounded disabled:opacity-40"
          >Prev</button>
          <span className="text-slate-500">Page {page + 1} of {totalPages}</span>
          <button
            disabled={page >= totalPages - 1}
            onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
            className="px-3 py-1 border rounded disabled:opacity-40"
          >Next</button>
        </div>
      )}
    </div>
  );
}
