import { useEffect, useState } from "react";
import { getNotes, getObservations, addNote } from "../api/client.js";
import ProposalReview from "../components/ProposalReview.jsx";

function mergeTimeline(notes, observations) {
  const items = [];
  for (const n of notes) {
    items.push({ at: n.at, kind: "note", note: n });
  }
  for (const o of observations) {
    items.push({ at: o.at, kind: "observation", obs: o });
  }
  items.sort((a, b) => new Date(a.at) - new Date(b.at));
  return items;
}

export default function ConversationPage() {
  const [notes, setNotes] = useState([]);
  const [observations, setObservations] = useState([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [latest, setLatest] = useState(null); // freshly-posted note proposals
  const [error, setError] = useState("");

  const load = () => {
    getNotes().then((d) => setNotes(d.notes || [])).catch(() => {});
    getObservations().then((d) => setObservations(d.observations || [])).catch(() => {});
  };
  useEffect(load, []);

  const send = async () => {
    const text = draft.trim();
    if (!text) return;
    setBusy(true); setError("");
    try {
      const res = await addNote(text, "note");
      setLatest({ text, proposals: res.proposals });
      setDraft("");
      load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const timeline = mergeTimeline(notes, observations);

  return (
    <div className="max-w-3xl">
      <h1 className="text-2xl font-semibold text-navy mb-1">Conversation</h1>
      <p className="text-sm text-slate-600 mb-6">
        Everything you've told me, everything I've noticed, and the proposals in between.
        Drop a note any time — "by the way, we switched to net-45 with Acme."
      </p>

      <div className="space-y-3 mb-6">
        {timeline.length === 0 && (
          <p className="text-sm text-slate-400">Nothing here yet. Tell me something below, or run a reconciliation.</p>
        )}
        {timeline.map((it, i) => {
          if (it.kind === "note") {
            const n = it.note;
            return (
              <div key={i} className="flex justify-end">
                <div className="max-w-[85%] bg-blue-50 border border-blue-100 rounded-lg rounded-br-none px-3 py-2">
                  <div className="text-[10px] uppercase tracking-wide text-slate-400">
                    {n.kind} · {new Date(n.at).toLocaleString()}
                  </div>
                  <div className="text-sm text-slate-800">{n.text}</div>
                  {n.parsed_proposals && (
                    <ProposalSummary proposals={n.parsed_proposals} />
                  )}
                </div>
              </div>
            );
          }
          const o = it.obs;
          return (
            <div key={i} className="flex justify-start">
              <div className="max-w-[85%] bg-white border border-slate-200 rounded-lg rounded-bl-none px-3 py-2">
                <div className="text-[10px] uppercase tracking-wide text-slate-400">
                  observation · {o.category} · {new Date(o.at).toLocaleString()}
                </div>
                <div className="text-sm text-slate-700">{o.text}</div>
              </div>
            </div>
          );
        })}
      </div>

      {latest && (
        <div className="mb-4">
          <ProposalReview proposals={latest.proposals} onConfirmed={load} />
        </div>
      )}

      <div className="sticky bottom-4 bg-white border border-slate-300 rounded-lg p-2 shadow-sm">
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Tell me something about your business…"
          className="w-full text-sm px-2 py-1.5 resize-y min-h-[48px] outline-none"
          onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) send(); }}
        />
        <div className="flex items-center justify-between">
          <span className="text-[10px] text-slate-400">⌘/Ctrl + Enter to send</span>
          {error && <span className="text-xs text-bad">{error}</span>}
          <button
            onClick={send}
            disabled={busy || !draft.trim()}
            className="bg-navy text-white text-sm px-4 py-1.5 rounded hover:bg-brand disabled:opacity-50"
          >
            {busy ? "Sending…" : "Send"}
          </button>
        </div>
      </div>
    </div>
  );
}

function ProposalSummary({ proposals }) {
  const a = proposals.alias_proposals?.length || 0;
  const r = proposals.rule_proposals?.length || 0;
  const f = proposals.brand_facts?.length || 0;
  if (a + r + f === 0) return null;
  const parts = [];
  if (a) parts.push(`${a} alias`);
  if (r) parts.push(`${r} rule`);
  if (f) parts.push(`${f} fact`);
  return <div className="text-[10px] text-slate-400 mt-1">parsed: {parts.join(", ")}</div>;
}
