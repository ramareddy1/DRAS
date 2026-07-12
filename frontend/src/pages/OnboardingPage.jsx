import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { addNote } from "../api/client.js";
import ProposalReview from "../components/ProposalReview.jsx";

const PROMPTS = [
  {
    key: "systems",
    q: "Which systems do you reconcile across?",
    placeholder: "e.g. Shopify orders vs. Stripe payouts; ShipBob 3PL vs. QuickBooks",
  },
  {
    key: "normal",
    q: "What kinds of differences are normal for you vs. surprising?",
    placeholder: "e.g. Stripe takes 2.9% + 30¢ — that's expected. A missing payout is not.",
  },
  {
    key: "owners",
    q: "Who handles refunds, chargebacks, or wholesale, and how do those show up?",
    placeholder: "e.g. Wholesale invoices are net-45 via Acme; refunds appear as negative Stripe rows.",
  },
  {
    key: "fees",
    q: "Any payment processors with custom fee rates we should know about?",
    placeholder: "e.g. PayPal is 3.49% + 49¢; our Stripe rate is negotiated to 2.6%.",
  },
];

export default function OnboardingPage() {
  const navigate = useNavigate();
  const [answers, setAnswers] = useState({});
  const [submitted, setSubmitted] = useState([]); // [{q, text, proposals}]
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const submitAll = async () => {
    setBusy(true); setError("");
    const results = [];
    try {
      for (const p of PROMPTS) {
        const text = (answers[p.key] || "").trim();
        if (!text) continue;
        const res = await addNote(text, "intake");
        results.push({ q: p.q, text, proposals: res.proposals });
      }
      setSubmitted(results);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const anyAnswered = PROMPTS.some((p) => (answers[p.key] || "").trim());

  if (submitted.length) {
    return (
      <div className="max-w-3xl">
        <h1 className="text-2xl font-semibold text-navy mb-1">Welcome — here's what I picked up</h1>
        <p className="text-sm text-slate-600 mb-6">
          Confirm what to remember. You can always teach me more later from the Conversation tab.
        </p>
        {submitted.map((s, i) => (
          <div key={i} className="mb-5 bg-white border border-slate-200 rounded-lg p-4">
            <div className="text-xs text-slate-500">{s.q}</div>
            <div className="text-sm text-slate-800 mb-1">{s.text}</div>
            <ProposalReview proposals={s.proposals} />
          </div>
        ))}
        <button
          onClick={() => navigate("/")}
          className="bg-navy text-white px-4 py-2 rounded text-sm font-medium hover:bg-brand"
        >
          Start reconciling →
        </button>
      </div>
    );
  }

  return (
    <div className="max-w-3xl">
      <h1 className="text-2xl font-semibold text-navy mb-1">Tell me about your business</h1>
      <p className="text-sm text-slate-600 mb-6">
        A few questions so I can reconcile the way you actually work. Skip any that don't apply —
        the more you tell me, the less you'll have to correct later.
      </p>

      {PROMPTS.map((p) => (
        <div key={p.key} className="mb-5">
          <label className="block text-sm font-medium text-slate-800 mb-1">{p.q}</label>
          <textarea
            value={answers[p.key] || ""}
            onChange={(e) => setAnswers((a) => ({ ...a, [p.key]: e.target.value }))}
            placeholder={p.placeholder}
            className="w-full border border-slate-300 rounded px-3 py-2 text-sm resize-y min-h-[64px] focus:border-brand focus:ring-1 focus:ring-brand outline-none"
          />
        </div>
      ))}

      {error && <p className="text-sm text-bad mb-3">{error}</p>}

      <div className="flex items-center gap-3">
        <button
          onClick={submitAll}
          disabled={busy || !anyAnswered}
          className="bg-navy text-white px-4 py-2 rounded text-sm font-medium hover:bg-brand disabled:opacity-50"
        >
          {busy ? "Reading your answers…" : "Continue"}
        </button>
        <button onClick={() => navigate("/")} className="text-sm text-slate-500 hover:text-slate-800">
          Skip for now
        </button>
      </div>
    </div>
  );
}
