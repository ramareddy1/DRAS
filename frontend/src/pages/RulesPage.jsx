import { useEffect, useState } from "react";
import { getRules, acceptRule, revokeRule, previewRule } from "../api/client.js";

/** Blast-radius preview for a pending force_status rule. */
function RulePreview({ ruleId }) {
  const [p, setP] = useState(null);
  useEffect(() => {
    previewRule(ruleId).then(setP).catch(() => setP(null));
  }, [ruleId]);
  if (!p) return null;
  return (
    <div className="mt-2 text-xs text-slate-600 bg-amber-50 border border-amber-200 rounded px-2 py-1.5">
      Would have affected <span className="font-semibold">{p.affected_count} row{p.affected_count === 1 ? "" : "s"}</span>
      {" "}totaling <span className="font-semibold">${Number(p.total_abs_diff).toLocaleString()}</span> across your last 10 jobs
      {p.max_abs_diff_ceiling != null && (
        <> · never applies when |diff| exceeds <span className="font-semibold">${Number(p.max_abs_diff_ceiling).toLocaleString()}</span></>
      )}
    </div>
  );
}

function RuleCard({ rule, children }) {
  return (
    <div className="bg-white border border-slate-200 rounded-lg p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-sm font-medium text-slate-800">{rule.description || rule.kind}</div>
          <div className="text-[10px] uppercase tracking-wide text-slate-400 mt-0.5">
            {rule.kind} · {rule.origin}
          </div>
        </div>
        <div className="flex gap-2 shrink-0">{children}</div>
      </div>
      {rule.user_origin_text && (
        <div className="mt-2 text-xs text-slate-600 bg-slate-50 border-l-2 border-brand pl-2 py-1 italic">
          You told us: "{rule.user_origin_text}"
        </div>
      )}
    </div>
  );
}

export default function RulesPage() {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(null);

  const load = () => getRules().then(setData).catch((e) => setError(e.message));
  useEffect(() => { load(); }, []);

  const doAccept = async (id) => { setBusy(id); try { await acceptRule(id); load(); } finally { setBusy(null); } };
  const doRevoke = async (id) => { setBusy(id); try { await revokeRule(id); load(); } finally { setBusy(null); } };

  if (error) return <div className="text-bad">Error: {error}</div>;
  if (!data) return <div className="text-slate-500">Loading…</div>;

  const { active = [], pending = [], revoked = [] } = data;

  return (
    <div className="max-w-3xl">
      <h1 className="text-2xl font-semibold text-navy mb-1">Rules</h1>
      <p className="text-sm text-slate-600 mb-6">
        What I apply before asking you. Pending rules were proposed from your repeated corrections —
        accept to make them stick, or reject if I got it wrong.
      </p>

      {pending.length > 0 && (
        <section className="mb-6">
          <h2 className="text-sm font-semibold text-amber-700 mb-2">Proposed ({pending.length})</h2>
          <div className="space-y-2">
            {pending.map((r) => (
              <div key={r.id}>
                <RuleCard rule={r}>
                  <button disabled={busy === r.id} onClick={() => doAccept(r.id)}
                    className="text-xs px-3 py-1.5 rounded bg-good text-white hover:opacity-90 disabled:opacity-50">Accept</button>
                  <button disabled={busy === r.id} onClick={() => doRevoke(r.id)}
                    className="text-xs px-3 py-1.5 rounded bg-slate-200 text-slate-700 hover:bg-slate-300 disabled:opacity-50">Reject</button>
                </RuleCard>
                {r.kind === "force_status" && <RulePreview ruleId={r.id} />}
              </div>
            ))}
          </div>
        </section>
      )}

      <section className="mb-6">
        <h2 className="text-sm font-semibold text-slate-700 mb-2">Active ({active.length})</h2>
        {active.length === 0 ? (
          <p className="text-sm text-slate-400">No active rules.</p>
        ) : (
          <div className="space-y-2">
            {active.map((r) => (
              <RuleCard key={r.id} rule={r}>
                <button disabled={busy === r.id} onClick={() => doRevoke(r.id)}
                  className="text-xs px-3 py-1.5 rounded border border-bad text-bad hover:bg-red-50 disabled:opacity-50">Revoke</button>
              </RuleCard>
            ))}
          </div>
        )}
      </section>

      {revoked.length > 0 && (
        <section>
          <h2 className="text-sm font-semibold text-slate-400 mb-2">Revoked ({revoked.length})</h2>
          <div className="space-y-2 opacity-60">
            {revoked.map((r) => <RuleCard key={r.id} rule={r} />)}
          </div>
        </section>
      )}
    </div>
  );
}
