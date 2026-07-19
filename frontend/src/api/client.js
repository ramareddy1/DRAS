import { accountFetch } from "../account.js";

const BASE = import.meta.env.VITE_API_BASE || "";

async function handle(res) {
  if (res.status === 401) {
    // Session gone — tell the AuthGate to show the login screen.
    window.dispatchEvent(new Event("reconops:unauthenticated"));
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.json();
}

// --- Auth ------------------------------------------------------------------

export async function requestCode(email) {
  return handle(await fetch(`${BASE}/api/auth/request-code`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email }),
  }));
}
export async function verifyCode(email, code) {
  return handle(await fetch(`${BASE}/api/auth/verify`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, code }),
  }));
}
export async function getMe() {
  return handle(await fetch(`${BASE}/api/auth/me`));
}
export async function logout() {
  return handle(await fetch(`${BASE}/api/auth/logout`, { method: "POST" }));
}
export async function claimAccount(accountId) {
  return handle(await fetch(`${BASE}/api/accounts/claim`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ account_id: accountId }),
  }));
}

export async function previewFile(file) {
  const fd = new FormData();
  fd.append("file", file);
  return handle(await accountFetch(`${BASE}/api/preview`, { method: "POST", body: fd }));
}

export async function getConcepts() {
  return handle(await accountFetch(`${BASE}/api/concepts`));
}

export async function submitReconciliation({ fileA, fileB, config }) {
  const fd = new FormData();
  fd.append("file_a", fileA);
  fd.append("file_b", fileB);
  fd.append("config", JSON.stringify(config));
  return handle(await accountFetch(`${BASE}/api/upload`, { method: "POST", body: fd }));
}

export async function getResults(id) {
  return handle(await accountFetch(`${BASE}/api/results/${id}`));
}

export async function getMyAccount() {
  return handle(await accountFetch(`${BASE}/api/accounts/me`));
}

export async function getJobs() {
  return handle(await accountFetch(`${BASE}/api/jobs`));
}

/** Mint a short-lived signed download token for a job (5-minute expiry). */
export async function getExportToken(jobId) {
  return post(`/api/results/${jobId}/export-token`);
}

// --- Phase 5: HITL endpoints ----------------------------------------------

async function post(path, body) {
  return handle(await accountFetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  }));
}

// Inbox / triage
export async function getInbox(jobId) {
  const q = jobId ? `?job_id=${encodeURIComponent(jobId)}` : "";
  return handle(await accountFetch(`${BASE}/api/inbox${q}`));
}
export async function resolveTriage(itemId, payload) {
  return post(`/api/triage/${itemId}/resolve`, payload);
}

// Rules
export async function getRules() {
  return handle(await accountFetch(`${BASE}/api/rules`));
}
export async function acceptRule(ruleId) {
  return post(`/api/rules/${ruleId}/accept`);
}
export async function previewRule(ruleId) {
  return handle(await accountFetch(`${BASE}/api/rules/${ruleId}/preview`));
}
export async function revokeRule(ruleId) {
  return post(`/api/rules/${ruleId}/revoke`);
}

// Decisions (rationale drawer)
export async function recordDecision(payload) {
  return post(`/api/decisions`, payload);
}

// Notes / onboarding / conversation
export async function getNotes() {
  return handle(await accountFetch(`${BASE}/api/accounts/me/notes`));
}
export async function addNote(text, kind = "note") {
  return post(`/api/accounts/me/notes`, { text, kind });
}
export async function confirmProposals(proposals, sourceText) {
  return post(`/api/accounts/me/notes/confirm`, { proposals, source_text: sourceText });
}

// Observations
export async function getObservations() {
  return handle(await accountFetch(`${BASE}/api/observations`));
}
export async function flagObservation(text) {
  return post(`/api/observations/feedback`, { text });
}

// Metrics
export async function getMetricsSeries(limit = 12) {
  return handle(await accountFetch(`${BASE}/api/metrics/series?limit=${limit}`));
}

// Compare
export async function compareJobs(jobId, prevJobId) {
  return handle(await accountFetch(`${BASE}/api/compare/${jobId}/${prevJobId}`));
}
