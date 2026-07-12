import { useState } from "react";
import { confirmProposals } from "../api/client.js";

/**
 * Renders the proposals extracted from a free-text note and lets the user
 * confirm which ones to write into account memory. Nothing is auto-applied —
 * the user keeps the system honest about what it's about to memorize.
 */
export default function ProposalReview({ proposals, onConfirmed }) {
  const aliases = proposals?.alias_proposals || [];
  const rules = proposals?.rule_proposals || [];
  const facts = proposals?.brand_facts || [];
  const total = aliases.length + rules.length + facts.length;

  // selection sets keyed by index
  const [selAlias, setSelAlias] = useState(() => new Set(aliases.map((_, i) => i)));
  const [selRule, setSelRule] = useState(() => new Set(rules.map((_, i) => i)));
  const [selFact, setSelFact] = useState(() => new Set(facts.map((_, i) => i)));
  const [saving, setSaving] = useState(false);
  const [done, setDone] = useState(null);

  if (proposals && proposals.extracted === false) {
    return (
      <p className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded p-2">
        Couldn't parse this right now{proposals.error ? ` — ${proposals.error}` : ""}. It's saved as a note regardless.
      </p>
    );
  }
  if (total === 0) {
    return <p className="text-xs text-slate-400">No structured proposals found — saved as a plain note.</p>;
  }

  const toggle = (set, setter, i) => {
    const next = new Set(set);
    next.has(i) ? next.delete(i) : next.add(i);
    setter(next);
  };

  const confirm = async () => {
    setSaving(true);
    const payload = {
      alias_proposals: aliases.filter((_, i) => selAlias.has(i)),
      rule_proposals: rules.filter((_, i) => selRule.has(i)),
      brand_facts: facts.filter((_, i) => selFact.has(i)),
    };
    try {
      const res = await confirmProposals(payload, proposals.source_text);
      setDone(res.applied);
      onConfirmed && onConfirmed(res.applied);
    } catch (e) {
      setDone({ error: e.message });
    } finally {
      setSaving(false);
    }
  };

  if (done) {
    if (done.error) return <p className="text-xs text-bad">Save failed: {done.error}</p>;
    return (
      <p className="text-xs text-good">
        Saved: {done.aliases} alias{done.aliases === 1 ? "" : "es"}, {done.rules} rule{done.rules === 1 ? "" : "s"}, {done.facts} fact{done.facts === 1 ? "" : "s"}.
      </p>
    );
  }

  const Row = ({ checked, onChange, children }) => (
    <label className="flex items-start gap-2 text-sm py-1 cursor-pointer">
      <input type="checkbox" checked={checked} onChange={onChange} className="mt-1" />
      <span className="flex-1">{children}</span>
    </label>
  );

  return (
    <div className="border border-brand/30 bg-blue-50/40 rounded-lg p-3 mt-2">
      <div className="text-xs font-medium text-brand mb-1">Here's what I understood — confirm what to remember:</div>

      {aliases.length > 0 && (
        <div className="mb-2">
          <div className="text-[10px] uppercase tracking-wide text-slate-500 mb-0.5">Column meanings</div>
          {aliases.map((a, i) => (
            <Row key={i} checked={selAlias.has(i)} onChange={() => toggle(selAlias, setSelAlias, i)}>
              <span className="font-mono">{a.text}</span> → <span className="font-mono text-brand">{a.concept_id}</span>
              <span className="text-slate-400"> · {Math.round((a.confidence || 0) * 100)}%</span>
            </Row>
          ))}
        </div>
      )}

      {rules.length > 0 && (
        <div className="mb-2">
          <div className="text-[10px] uppercase tracking-wide text-slate-500 mb-0.5">Rules (saved as pending for review)</div>
          {rules.map((r, i) => (
            <Row key={i} checked={selRule.has(i)} onChange={() => toggle(selRule, setSelRule, i)}>
              {r.description}
              <span className="text-slate-400"> · {r.type}</span>
            </Row>
          ))}
        </div>
      )}

      {facts.length > 0 && (
        <div className="mb-2">
          <div className="text-[10px] uppercase tracking-wide text-slate-500 mb-0.5">Brand facts</div>
          {facts.map((f, i) => (
            <Row key={i} checked={selFact.has(i)} onChange={() => toggle(selFact, setSelFact, i)}>
              {f.fact}<span className="text-slate-400"> · {f.category}</span>
            </Row>
          ))}
        </div>
      )}

      <button
        onClick={confirm}
        disabled={saving}
        className="mt-1 bg-navy text-white text-xs px-3 py-1.5 rounded hover:bg-brand disabled:opacity-50"
      >
        {saving ? "Saving…" : "Confirm selected"}
      </button>
    </div>
  );
}
