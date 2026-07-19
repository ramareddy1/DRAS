import { useEffect, useState } from "react";
import { Link, NavLink, Outlet } from "react-router-dom";
import { ensureAccount } from "../account.js";
import { getMe, getMetricsSeries, logout } from "../api/client.js";

function Sparkline({ points, color = "#48bb78" }) {
  // points: array of 0..1 values
  if (!points.length) return null;
  const w = 120, h = 28, pad = 2;
  const n = points.length;
  const x = (i) => pad + (n === 1 ? w / 2 : (i * (w - 2 * pad)) / (n - 1));
  const y = (v) => h - pad - v * (h - 2 * pad);
  const d = points.map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  return (
    <svg width={w} height={h} className="overflow-visible">
      <path d={d} fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx={x(n - 1)} cy={y(points[n - 1])} r="2.5" fill={color} />
    </svg>
  );
}

function DensityStrip() {
  const [series, setSeries] = useState(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    let alive = true;
    getMetricsSeries(12).then((d) => { if (alive) setSeries(d.series || []); }).catch(() => setSeries([]));
    return () => { alive = false; };
  }, []);

  if (series === null) return null;
  if (series.length === 0) {
    return (
      <div className="bg-slate-50 border-b border-slate-200">
        <div className="max-w-7xl mx-auto px-6 py-1.5 text-[11px] text-slate-400">
          Insight density appears here once you run your first reconciliation.
        </div>
      </div>
    );
  }

  const latest = series[series.length - 1];
  const density = latest.insight_density ?? 0;
  const trust = latest.trust_adjusted_density ?? 0;
  const override = latest.override_rate ?? 0;
  const prev = series.length > 1 ? series[series.length - 2] : null;
  const delta = prev ? trust - (prev.trust_adjusted_density ?? 0) : 0;
  const pct = (v) => `${Math.round(v * 100)}%`;
  const arrow = delta > 0.0005 ? "▲" : delta < -0.0005 ? "▼" : "·";
  const arrowCls = delta > 0.0005 ? "text-good" : delta < -0.0005 ? "text-bad" : "text-slate-400";

  return (
    <div className="bg-slate-50 border-b border-slate-200">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full max-w-7xl mx-auto px-6 py-1.5 flex items-center gap-5 text-[12px] text-slate-600 hover:bg-slate-100"
      >
        <span className="font-medium text-slate-700">Insight density</span>
        <span className="font-mono text-navy">{pct(density)}</span>
        <span className="text-slate-400">trust-adjusted</span>
        <span className="font-mono text-navy">{pct(trust)}</span>
        <span className={`font-mono ${arrowCls}`}>{arrow} {prev ? pct(Math.abs(delta)) : ""}</span>
        <span className="ml-auto flex items-center gap-3">
          <Sparkline points={series.map((s) => s.trust_adjusted_density ?? 0)} />
          <span className="text-slate-400">{open ? "▲" : "▼"}</span>
        </span>
      </button>
      {open && (
        <div className="max-w-7xl mx-auto px-6 pb-3 grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
          <Metric label="Insight density" value={pct(density)} hint={`${latest.auto_handled} auto / ${latest.needed_user} needed you`} />
          <Metric label="Trust-adjusted" value={pct(trust)} hint="density × (1 − override rate)" />
          <Metric label="Override rate" value={pct(override)} hint="auto-handled rows you later corrected" tone={override > 0.1 ? "bad" : "good"} />
          <Metric label="Jobs tracked" value={series.length} hint="last 12 shown" />
          <Link to="/metrics" className="sm:col-span-4 text-[11px] text-brand hover:underline">
            Full metrics & trend →
          </Link>
        </div>
      )}
    </div>
  );
}

function Metric({ label, value, hint, tone }) {
  const cls = tone === "bad" ? "text-bad" : tone === "good" ? "text-good" : "text-navy";
  return (
    <div className="bg-white border border-slate-200 rounded p-2">
      <div className="text-[10px] uppercase tracking-wide text-slate-400">{label}</div>
      <div className={`text-lg font-semibold ${cls}`}>{value}</div>
      {hint && <div className="text-[10px] text-slate-400 mt-0.5">{hint}</div>}
    </div>
  );
}

export default function Layout() {
  const linkCls = ({ isActive }) =>
    `px-3 py-1.5 rounded text-sm ${
      isActive ? "bg-white/15 text-white" : "text-blue-100 hover:bg-white/10"
    }`;

  const [me, setMe] = useState(null);
  useEffect(() => {
    ensureAccount().catch(() => {});
    getMe().then(setMe).catch(() => {});
  }, []);

  return (
    <div className="min-h-screen flex flex-col">
      <header className="bg-navy text-white">
        <div className="max-w-7xl mx-auto px-6 py-4 flex items-center justify-between">
          <Link to="/" className="flex items-baseline gap-3">
            <span className="text-xl font-semibold tracking-tight">ReconOps AI</span>
            <span className="text-xs text-blue-200 hidden lg:inline">
              Upload. Reconcile. Know where your money is.
            </span>
          </Link>
          <nav className="flex items-center gap-1 flex-wrap justify-end">
            <NavLink to="/" end className={linkCls}>New</NavLink>
            <NavLink to="/inbox" className={linkCls}>Inbox</NavLink>
            <NavLink to="/conversation" className={linkCls}>Conversation</NavLink>
            <NavLink to="/rules" className={linkCls}>Rules</NavLink>
            <NavLink to="/observations" className={linkCls}>Observations</NavLink>
            <NavLink to="/metrics" className={linkCls}>Metrics</NavLink>
            <NavLink to="/history" className={linkCls}>History</NavLink>
            {me && (
              <span className="ml-3 flex items-center gap-2">
                <span className="text-[11px] text-blue-200 hidden sm:inline">{me.user.email}</span>
                <button
                  onClick={async () => { try { await logout(); } finally { window.location.reload(); } }}
                  className="text-[10px] text-blue-200 hover:text-white border border-blue-400/40 rounded px-2 py-1"
                >
                  Sign out
                </button>
              </span>
            )}
          </nav>
        </div>
      </header>
      <DensityStrip />
      <main className="flex-1 max-w-7xl w-full mx-auto px-6 py-8">
        <Outlet />
      </main>
      <footer className="text-xs text-slate-400 py-4 text-center">
        ReconOps AI · Pilot
      </footer>
    </div>
  );
}
