import { useEffect, useState } from "react";
import { useNavigate, Link } from "react-router-dom";
import DropZone from "../components/DropZone.jsx";
import ColumnMapper from "../components/ColumnMapper.jsx";
import { previewFile, submitReconciliation, getConcepts, getNotes } from "../api/client.js";
import { saveHistoryItem } from "../history.js";

const RECON_TYPES = [
  { id: "orders_vs_payments", title: "Orders vs. Payments", desc: "Shopify orders ↔ Stripe / PayPal settlements" },
  { id: "inventory_cross_check", title: "Inventory Cross-Check", desc: "Platform stock ↔ 3PL stock report" },
  { id: "po_vs_invoices", title: "POs vs. Invoices", desc: "Purchase orders ↔ supplier invoices" },
  { id: "custom", title: "Custom", desc: "Pick any two files and define the join" },
];

const DEFAULT_LABELS = {
  orders_vs_payments: ["Shopify", "Stripe"],
  inventory_cross_check: ["Platform", "3PL"],
  po_vs_invoices: ["POs", "Invoices"],
  custom: ["Source A", "Source B"],
};

export default function UploadPage() {
  const nav = useNavigate();
  const [reconType, setReconType] = useState("orders_vs_payments");
  const [fileA, setFileA] = useState(null);
  const [fileB, setFileB] = useState(null);
  const [previewA, setPreviewA] = useState(null);
  const [previewB, setPreviewB] = useState(null);
  const [bindingsA, setBindingsA] = useState([]);
  const [bindingsB, setBindingsB] = useState([]);
  const [errA, setErrA] = useState("");
  const [errB, setErrB] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [submitErr, setSubmitErr] = useState("");
  const [concepts, setConcepts] = useState([]);
  const [showOnboard, setShowOnboard] = useState(false);

  useEffect(() => {
    getConcepts().then(setConcepts).catch(() => setConcepts([]));
    getNotes()
      .then((d) => setShowOnboard((d.notes || []).length === 0))
      .catch(() => {});
  }, []);

  async function handleFile(which, file) {
    const setFile = which === "a" ? setFileA : setFileB;
    const setPrev = which === "a" ? setPreviewA : setPreviewB;
    const setBnd = which === "a" ? setBindingsA : setBindingsB;
    const setErr = which === "a" ? setErrA : setErrB;
    setFile(file); setErr(""); setPrev(null); setBnd([]);
    try {
      const p = await previewFile(file);
      setPrev(p);
      setBnd(p.bindings || []);
    } catch (e) {
      setErr(e.message);
    }
  }

  const [labelA, labelB] = DEFAULT_LABELS[reconType];

  function hasPrimaryKey(bindings) {
    return bindings.some((b) => {
      const c = concepts.find((x) => x.id === b.concept_id);
      return c && c.role === "primary_key";
    });
  }

  const canSubmit =
    fileA && fileB && hasPrimaryKey(bindingsA) && hasPrimaryKey(bindingsB) && !submitting;

  async function submit() {
    setSubmitting(true);
    setSubmitErr("");
    try {
      const config = {
        recon_type: reconType,
        source_a: { bindings: bindingsA },
        source_b: { bindings: bindingsB },
        label_a: labelA,
        label_b: labelB,
      };
      const { job_id } = await submitReconciliation({ fileA, fileB, config });
      saveHistoryItem({
        job_id,
        created_at: new Date().toISOString(),
        recon_type: reconType,
        file_a: fileA.name,
        file_b: fileB.name,
        label_a: labelA,
        label_b: labelB,
      });
      nav(`/results/${job_id}`);
    } catch (e) {
      setSubmitErr(e.message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div>
      {showOnboard && (
        <div className="mb-5 bg-blue-50 border border-blue-200 rounded-lg px-4 py-3 flex items-center justify-between gap-3">
          <div className="text-sm text-blue-900">
            New here? Tell me how your business works first — I'll reconcile the way you actually operate.
          </div>
          <div className="flex items-center gap-3 shrink-0">
            <Link to="/onboarding" className="bg-navy text-white text-sm px-3 py-1.5 rounded hover:bg-brand">
              Set me up
            </Link>
            <button onClick={() => setShowOnboard(false)} className="text-xs text-slate-500 hover:text-slate-800">
              Dismiss
            </button>
          </div>
        </div>
      )}

      <div className="mb-6">
        <h1 className="text-2xl font-semibold text-navy">New reconciliation</h1>
        <p className="text-sm text-slate-600 mt-1">
          Upload exports from any two systems. We'll infer what each column means and match across them.
        </p>
      </div>

      <section className="mb-6">
        <h2 className="text-sm font-medium text-slate-700 mb-2">1. Reconciliation type</h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          {RECON_TYPES.map((t) => (
            <button
              key={t.id}
              onClick={() => setReconType(t.id)}
              className={`text-left p-3 border rounded-lg transition ${
                reconType === t.id
                  ? "border-brand bg-blue-50 ring-1 ring-brand"
                  : "border-slate-300 bg-white hover:border-brand"
              }`}
            >
              <div className="font-medium text-slate-800 text-sm">{t.title}</div>
              <div className="text-xs text-slate-500 mt-1">{t.desc}</div>
            </button>
          ))}
        </div>
      </section>

      <section className="mb-6">
        <h2 className="text-sm font-medium text-slate-700 mb-2">2. Upload files</h2>
        <div className="flex flex-col md:flex-row gap-4">
          <DropZone label={`Source A — ${labelA}`} file={fileA} preview={previewA}
            onFile={(f) => handleFile("a", f)} error={errA} />
          <DropZone label={`Source B — ${labelB}`} file={fileB} preview={previewB}
            onFile={(f) => handleFile("b", f)} error={errB} />
        </div>
      </section>

      {(previewA || previewB) && (
        <section className="mb-6">
          <div className="flex items-baseline justify-between mb-2">
            <h2 className="text-sm font-medium text-slate-700">3. Confirm column meanings</h2>
            <p className="text-xs text-slate-500">
              We've inferred what each column means — confirm or override.
            </p>
          </div>
          <div className="space-y-4">
            {previewA && (
              <ColumnMapper label={`Source A — ${labelA}`} preview={previewA}
                bindings={bindingsA} concepts={concepts} onChange={setBindingsA} />
            )}
            {previewB && (
              <ColumnMapper label={`Source B — ${labelB}`} preview={previewB}
                bindings={bindingsB} concepts={concepts} onChange={setBindingsB} />
            )}
          </div>
        </section>
      )}

      <div className="flex items-center gap-4">
        <button
          disabled={!canSubmit}
          onClick={submit}
          className="bg-brand text-white px-6 py-2.5 rounded font-medium hover:bg-navy disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {submitting ? "Reconciling…" : "Reconcile"}
        </button>
        {!canSubmit && fileA && fileB && (
          <span className="text-xs text-slate-500">
            Bind at least one column on each side to a primary_key concept (e.g. order.id).
          </span>
        )}
        {submitting && (
          <span className="text-sm text-slate-500">Matching records, comparing amounts, generating insights…</span>
        )}
        {submitErr && <span className="text-sm text-bad">{submitErr}</span>}
      </div>
    </div>
  );
}
