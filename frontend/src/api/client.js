import { accountFetch } from "../account.js";

const BASE = import.meta.env.VITE_API_BASE || "";

async function handle(res) {
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.json();
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

/**
 * Export URL needs the account ID; since <a href> can't carry headers, we
 * include the account ID as a query param. (Pilot only; we'll switch to a
 * signed short-lived token at production.)
 */
export function exportUrl(id, accountId) {
  return `${BASE}/api/results/${id}/export?account_id=${encodeURIComponent(accountId || "")}`;
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
