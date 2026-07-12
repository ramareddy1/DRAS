import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { getMetricsSeries } from "../api/client.js";

function LineChart({ series, keys }) {
  // series: array of metric snapshots; keys: [{k, color, label}]
  const w = 640, h = 220, padL = 36, padR = 12, padT = 12, padB = 28;
  const n = series.length;
  if (n === 0) return null;
  const x = (i) => padL + (n === 1 ? (w - padL - padR) / 2 : (i * (w - padL - padR)) / (n - 1));
  const y = (v) => padT + (1 - Math.max(0, Math.min(1, v))) * (h - padT - padB);

  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="w-full">
      {/* y gridlines at 0/0.5/1 */}
      {[0, 0.5, 1].map((g) => (
        <g key={g}>
          <line x1={padL} y1={y(g)} x2={w - padR} y2={y(g)} stroke="#e2e8f0" strokeWidth="1" />
          <text x={4} y={y(g) + 4} fontSize="10" fill="#94a3b8">{Math.round(g * 100)}%</text>
        </g>
      ))}
      {keys.map(({ k, color }) => {
        const d = series
          .map((s, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(s[k] ?? 0).toFixed(1)}`)
          .join(" ");
        return (
          <g key={k}>
            <path d={d} fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
            {series.map((s, i) => (
              <circle key={i} cx={x(i)} cy={y(s[k] ?? 0)} r="2.5" fill={color} />
            ))}
          </g>
        );
      })}
      {/* x labels: job index */}
      {series.map((s, i) => (
        <text key={i} x={x(i)} y={h - 8} fontSize="9" fill="#94a3b8" textAnchor="middle">
          {i + 1}
        </text>
      ))}
    </svg>
  );
}

const KEYS = [
  { k: "insight_density", color: "#2b6cb0", label: "Insight density" },
  { k: "trust_adjusted_density", color: "#48bb78", label: "Trust-adjusted" },
  { k: "override_rate", color: "#e53e3e", label: "Override rate" },
  { k: "revocation_rate", color: "#ecc94b", label: "Revocation rate" },
];

export default function MetricsPage() {
  const [series, setSeries] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    getMetricsSeries(50).then((d) => setSeries(d.series || [])).catch((e) => setError(e.message));
  }, []);

  if (error) return <div className="text-bad">Error: {error}</div>;
  if (series === null) return <div className="text-slate-500">Loading…</div>;

  const latest = series[series.length - 1];

  return (
    <div className="max-w-3xl">
      <h1 className="text-2xl font-semibold text-navy mb-1">Metrics</h1>
      <p className="text-sm text-slate-600 mb-6">
        Insight density is the headline: the share of rows handled without needing you. It should
        rise as you teach me — while the override rate (how often you correct what I handled
        silently) stays low. If density climbs but override rises too, I'm getting overconfident.
      </p>

      {series.length === 0 ? (
        <div className="text-center py-12 text-slate-500">
          <p>No metrics yet.</p>
          <Link to="/" className="text-brand hover:underline mt-2 inline-block">Run a reconciliation →</Link>
        </div>
      ) : (
        <>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-6">
            {KEYS.map(({ k, color, label }) => (
              <div key={k} className="bg-white border border-slate-200 rounded p-3">
                <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wide text-slate-400">
                  <span className="w-2 h-2 rounded-full" style={{ background: color }} />
                  {label}
                </div>
                <div className="text-xl font-semibold text-navy mt-1">
                  {Math.round((latest[k] ?? 0) * 100)}%
                </div>
              </div>
            ))}
          </div>

          <div className="bg-white border border-slate-200 rounded-lg p-4 mb-4">
            <LineChart series={series} keys={KEYS} />
          </div>

          <div className="bg-white border border-slate-200 rounded-lg overflow-hidden">
            <table className="min-w-full text-sm">
              <thead className="bg-slate-100">
                <tr>
                  <th className="px-3 py-2 text-left font-medium">#</th>
                  <th className="px-3 py-2 text-right font-medium">Auto</th>
                  <th className="px-3 py-2 text-right font-medium">Needed you</th>
                  <th className="px-3 py-2 text-right font-medium">Density</th>
                  <th className="px-3 py-2 text-right font-medium">Trust-adj</th>
                  <th className="px-3 py-2 text-right font-medium">Override</th>
                  <th className="px-3 py-2 text-right font-medium">LLM calls</th>
                </tr>
              </thead>
              <tbody>
                {series.map((s, i) => (
                  <tr key={i} className="border-t border-slate-200">
                    <td className="px-3 py-1.5 text-slate-500">{i + 1}</td>
                    <td className="px-3 py-1.5 text-right">{s.auto_handled}</td>
                    <td className="px-3 py-1.5 text-right">{s.needed_user}</td>
                    <td className="px-3 py-1.5 text-right">{Math.round((s.insight_density ?? 0) * 100)}%</td>
                    <td className="px-3 py-1.5 text-right">{Math.round((s.trust_adjusted_density ?? 0) * 100)}%</td>
                    <td className="px-3 py-1.5 text-right">{Math.round((s.override_rate ?? 0) * 100)}%</td>
                    <td className="px-3 py-1.5 text-right text-slate-500">{s.llm_calls}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
