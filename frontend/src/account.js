/**
 * Workspace selection in the cookie-session era.
 *
 * The httpOnly session cookie is the credential; localStorage only remembers
 * WHICH workspace is selected (membership is enforced server-side on every
 * request). A stored pre-auth UUID is claimed once after first login — the
 * one-time migration path off the localStorage-UUID-as-password pilot.
 */
import { claimAccount, getMe } from "./api/client.js";

const STORAGE_KEY = "reconops_account_id";
const BASE = import.meta.env.VITE_API_BASE || "";

function readStored() {
  try { return localStorage.getItem(STORAGE_KEY) || null; } catch { return null; }
}
function writeStored(id) {
  try { localStorage.setItem(STORAGE_KEY, id); } catch {}
}
function clearStored() {
  try { localStorage.removeItem(STORAGE_KEY); } catch {}
}

async function createAccount() {
  const res = await fetch(`${BASE}/api/accounts`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  if (!res.ok) throw new Error(`Could not create workspace (${res.status})`);
  return res.json();
}

let _initPromise = null;

/** Resolve the selected workspace id, claiming/creating one if needed. */
export function ensureAccount() {
  if (_initPromise) return _initPromise;
  _initPromise = (async () => {
    const me = await getMe(); // throws on 401 -> AuthGate shows login
    const memberships = me.accounts || [];
    const stored = readStored();
    if (stored && memberships.some((m) => m.account_id === stored)) {
      return stored;
    }
    if (memberships.length > 0) {
      writeStored(memberships[0].account_id);
      return memberships[0].account_id;
    }
    if (stored) {
      // Legacy pre-auth workspace: claim it with its UUID, once.
      try {
        await claimAccount(stored);
        return stored;
      } catch {
        clearStored();
      }
    }
    const acc = await createAccount();
    writeStored(acc.id);
    return acc.id;
  })();
  // Failed init (e.g. signed out) must not stick — retry after next login.
  _initPromise.catch(() => { _initPromise = null; });
  return _initPromise;
}

/** Drop-in replacement for fetch that adds X-Account-Id. */
export async function accountFetch(input, init = {}) {
  const id = await ensureAccount();
  const headers = new Headers(init.headers || {});
  headers.set("X-Account-Id", id);
  return fetch(input, { ...init, headers });
}

/** Synchronous read for display purposes. May return null on very first render. */
export function currentAccountId() {
  return readStored();
}
