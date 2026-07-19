import { useEffect, useState } from "react";
import { getMe, requestCode, verifyCode } from "./api/client.js";

/** Gate the whole app behind a session; shows LoginPage when signed out. */
export function AuthGate({ children }) {
  const [state, setState] = useState("loading"); // loading | in | out
  useEffect(() => {
    getMe().then(() => setState("in")).catch(() => setState("out"));
    const onOut = () => setState("out");
    window.addEventListener("reconops:unauthenticated", onOut);
    return () => window.removeEventListener("reconops:unauthenticated", onOut);
  }, []);
  if (state === "loading") {
    return <div className="text-center py-16 text-slate-400">Loading…</div>;
  }
  if (state === "out") {
    return <LoginPage onSignedIn={() => setState("in")} />;
  }
  return children;
}

function LoginPage({ onSignedIn }) {
  const [email, setEmail] = useState("");
  const [phase, setPhase] = useState("email"); // email | code
  const [code, setCode] = useState("");
  const [devCode, setDevCode] = useState("");
  const [err, setErr] = useState("");

  const submitEmail = async (e) => {
    e.preventDefault(); setErr("");
    try {
      const r = await requestCode(email);
      setDevCode(r.dev_code || "");
      setPhase("code");
    } catch (ex) { setErr(ex.message); }
  };
  const submitCode = async (e) => {
    e.preventDefault(); setErr("");
    try { await verifyCode(email, code); onSignedIn(); }
    catch (ex) { setErr(ex.message); }
  };

  return (
    <div className="max-w-sm mx-auto mt-24 bg-white border border-slate-200 rounded-lg p-6">
      <h1 className="text-xl font-semibold text-navy mb-1">Sign in to ReconOps</h1>
      <p className="text-sm text-slate-500 mb-4">We'll email you a 6-digit code.</p>
      {phase === "email" ? (
        <form onSubmit={submitEmail} className="space-y-3">
          <input autoFocus type="email" required value={email} placeholder="you@company.com"
                 onChange={(e) => setEmail(e.target.value)}
                 className="w-full border border-slate-300 rounded px-3 py-2 text-sm" />
          <button className="w-full bg-navy text-white rounded px-3 py-2 text-sm font-medium hover:bg-brand">
            Email me a code</button>
        </form>
      ) : (
        <form onSubmit={submitCode} className="space-y-3">
          <p className="text-xs text-slate-500">Code sent to {email}.</p>
          {devCode && (
            <p className="text-xs text-amber-700">
              Dev mode — your code: <b>{devCode}</b>
            </p>
          )}
          <input autoFocus inputMode="numeric" pattern="[0-9]*" maxLength={6} required value={code}
                 onChange={(e) => setCode(e.target.value)} placeholder="123456"
                 className="w-full border border-slate-300 rounded px-3 py-2 text-sm tracking-widest" />
          <button className="w-full bg-navy text-white rounded px-3 py-2 text-sm font-medium hover:bg-brand">
            Sign in</button>
          <button type="button" onClick={() => setPhase("email")}
                  className="w-full text-xs text-slate-500 hover:underline">Different email</button>
        </form>
      )}
      {err && <p className="mt-3 text-xs text-bad">{err}</p>}
    </div>
  );
}
