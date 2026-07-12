/**
 * Account lifecycle on the frontend.
 *
 * The pilot has no auth — every browser has its own account UUID, stored in
 * localStorage. On first visit (no UUID), we create one. All API calls go
 * through `accountFetch`, which adds the X-Account-Id header.
 *
 * URL escape hatches:
 *   ?reset            → clear localStorage and create a new account
 *   ?account=<uuid>   → adopt an existing account UUID (for sharing / debug)
 */
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
  if (!res.ok) throw new Error(`Could not create account (${res.status})`);
  return res.json();
}

let _initPromise = null;

/**
 * Lazy singleton: resolve to the current account ID, creating one if needed.
 * Honors ?reset and ?account=<uuid> from the URL on first call only.
 */
export function ensureAccount() {
  if (_initPromise) return _initPromise;
  _initPromise = (async () => {
    const url = new URL(window.location.href);
    if (url.searchParams.has("reset")) {
      clearStored();
      url.searchParams.delete("reset");
      window.history.replaceState({}, "", url.toString());
    }
    const fromUrl = url.searchParams.get("account");
    if (fromUrl) {
      writeStored(fromUrl);
      url.searchParams.delete("account");
      window.history.replaceState({}, "", url.toString());
    }
    let id = readStored();
    if (!id) {
      const acc = await createAccount();
      id = acc.id;
      writeStored(id);
    }
    return id;
  })();
  return _initPromise;
}

/** Drop-in replacement for fetch that adds X-Account-Id. */
export async function accountFetch(input, init = {}) {
  const id = await ensureAccount();
  const headers = new Headers(init.headers || {});
  headers.set("X-Account-Id", id);
  return fetch(input, { ...init, headers });
}

/** Force-reset: wipe local account and reload. */
export function resetAccount() {
  clearStored();
  _initPromise = null;
  window.location.href = "/";
}

/** Synchronous read for display purposes. May return null on very first render. */
export function currentAccountId() {
  return readStored();
}
