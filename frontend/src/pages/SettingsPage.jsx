import { useEffect, useState } from "react";
import { getMe, getMyAccount, updateProfile, deleteAccount, getConnections, connectShopify, deleteConnection } from "../api/client.js";
import { currentAccountId, forgetAccount } from "../account.js";

export default function SettingsPage() {
  const [account, setAccount] = useState(null);
  const [accountError, setAccountError] = useState("");
  const [accountLoaded, setAccountLoaded] = useState(false);
  const [isOwner, setIsOwner] = useState(false);
  const [meLoaded, setMeLoaded] = useState(false);
  const [error, setError] = useState("");
  const [retentionDays, setRetentionDays] = useState(7);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [confirmText, setConfirmText] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const [connections, setConnections] = useState([]);
  const [shopDomain, setShopDomain] = useState("");
  const [accessToken, setAccessToken] = useState("");
  const [connecting, setConnecting] = useState(false);
  const [connectError, setConnectError] = useState("");
  const [disconnecting, setDisconnecting] = useState(false);

  useEffect(() => {
    // Fetched independently (not Promise.all) so a getMyAccount() failure —
    // e.g. a workspace left 404-ing after a partial purge — doesn't also
    // block getMe() from resolving isOwner, which is all the Danger Zone's
    // delete-retry flow needs.
    getMyAccount()
      .then((acc) => {
        setAccount(acc);
        setRetentionDays(acc.profile.retention_days);
      })
      .catch((e) => setAccountError(e.message))
      .finally(() => setAccountLoaded(true));

    getMe()
      .then((me) => {
        const accountId = currentAccountId();
        const membership = (me.accounts || []).find((a) => a.account_id === accountId);
        setIsOwner(membership?.role === "owner");
      })
      .catch((e) => setError(e.message))
      .finally(() => setMeLoaded(true));

    getConnections()
      .then(setConnections)
      .catch(() => setConnections([]));
  }, []);

  const saveRetention = async () => {
    setSaving(true);
    setSaved(false);
    try {
      const updated = await updateProfile({ retention_days: Number(retentionDays) });
      setAccount(updated);
      setSaved(true);
    } catch (e) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  };

  const doDelete = async () => {
    setDeleting(true);
    setDeleteError("");
    try {
      await deleteAccount();
      forgetAccount();
      window.location.reload();
    } catch (e) {
      setDeleteError(e.message);
      setDeleting(false);
    }
  };

  const doConnectShopify = async () => {
    setConnecting(true);
    setConnectError("");
    try {
      const conn = await connectShopify({ shop_domain: shopDomain, access_token: accessToken });
      setConnections((prev) => [...prev.filter((c) => c.provider !== "shopify"), conn]);
      setShopDomain(""); setAccessToken("");
    } catch (e) {
      setConnectError(e.message);
    } finally {
      setConnecting(false);
    }
  };

  const doDisconnectShopify = async () => {
    setDisconnecting(true);
    try {
      await deleteConnection("shopify");
      setConnections((prev) => prev.filter((c) => c.provider !== "shopify"));
    } finally {
      setDisconnecting(false);
    }
  };

  // getMe() failing means isOwner can't be determined at all (no membership
  // to act as owner of), so that's the one case left as a plain fatal error.
  if (error) return <div className="text-bad">Error: {error}</div>;
  if (!meLoaded || !accountLoaded) return <div className="text-slate-500">Loading…</div>;

  const shopifyConn = connections.find((c) => c.provider === "shopify");

  return (
    <div className="max-w-2xl">
      <h1 className="text-2xl font-semibold text-navy mb-1">Settings</h1>
      <p className="text-sm text-slate-600 mb-6">
        Workspace data retention and account deletion.
      </p>

      {account ? (
        <section className="mb-8 bg-white border border-slate-200 rounded-lg p-4">
          <h2 className="text-sm font-semibold text-slate-700 mb-2">Data retention</h2>
          <p className="text-xs text-slate-500 mb-3">
            Jobs and their uploaded files are permanently deleted after this many
            days (1–365). Runs hourly.
          </p>
          <div className="flex items-center gap-2">
            <input
              type="number"
              min={1}
              max={365}
              value={retentionDays}
              onChange={(e) => setRetentionDays(e.target.value)}
              disabled={!isOwner}
              className="w-24 border border-slate-300 rounded px-2 py-1 text-sm disabled:bg-slate-100"
            />
            <span className="text-sm text-slate-500">days</span>
            {isOwner && (
              <button
                onClick={saveRetention}
                disabled={saving}
                className="ml-2 text-xs px-3 py-1.5 rounded bg-brand text-white hover:opacity-90 disabled:opacity-50"
              >
                {saving ? "Saving…" : "Save"}
              </button>
            )}
            {saved && <span className="text-xs text-good">Saved</span>}
          </div>
          {!isOwner && (
            <p className="text-xs text-slate-400 mt-2">Only the workspace owner can change this.</p>
          )}
        </section>
      ) : (
        <div className="mb-8 bg-white border border-bad rounded-lg p-4 text-sm text-bad">
          Could not load workspace details: {accountError}
        </div>
      )}

            <section className="mb-8 bg-white border border-slate-200 rounded-lg p-4">
        <h2 className="text-sm font-semibold text-slate-700 mb-2">Integrations</h2>
        {shopifyConn ? (
          <div className="text-sm">
            <div className="text-slate-700">
              Connected to <span className="font-medium">{shopifyConn.shop_domain}</span>
              {shopifyConn.status === "error" && (
                <span className="text-bad ml-2">— needs reconnect</span>
              )}
            </div>
            <div className="text-xs text-slate-500 mt-1">
              Last synced: {shopifyConn.last_synced_at || "Never"}
            </div>
            {isOwner && (
              <button
                onClick={doDisconnectShopify}
                disabled={disconnecting}
                className="mt-2 text-xs px-3 py-1.5 rounded bg-bad text-white hover:opacity-90 disabled:opacity-50"
              >
                {disconnecting ? "Disconnecting…" : "Disconnect"}
              </button>
            )}
          </div>
        ) : isOwner ? (
          <div>
            <p className="text-xs text-slate-500 mb-3">
              Paste an Admin API access token from a Custom App in your Shopify
              Admin (Settings → Apps and sales channels → Develop apps).
            </p>
            <div className="flex flex-col gap-2 max-w-sm">
              <input
                type="text"
                placeholder="yourstore.myshopify.com"
                value={shopDomain}
                onChange={(e) => setShopDomain(e.target.value)}
                className="border border-slate-300 rounded px-2 py-1 text-sm"
              />
              <input
                type="password"
                placeholder="Admin API access token"
                value={accessToken}
                onChange={(e) => setAccessToken(e.target.value)}
                className="border border-slate-300 rounded px-2 py-1 text-sm"
              />
              <button
                onClick={doConnectShopify}
                disabled={connecting || !shopDomain || !accessToken}
                className="text-xs px-3 py-1.5 rounded bg-brand text-white hover:opacity-90 disabled:opacity-50 self-start"
              >
                {connecting ? "Connecting…" : "Connect"}
              </button>
            </div>
            {connectError && <p className="text-xs text-bad mt-2">{connectError}</p>}
          </div>
        ) : (
          <p className="text-xs text-slate-400">Not connected. Only the workspace owner can connect Shopify.</p>
        )}
      </section>

{isOwner && (
        <section className="bg-white border border-bad rounded-lg p-4">
          <h2 className="text-sm font-semibold text-bad mb-2">Danger zone</h2>
          <p className="text-xs text-slate-600 mb-3">
            Permanently deletes this workspace: all jobs, rules, decisions,
            metrics, uploaded files, and team membership. This cannot be
            undone. Type <span className="font-mono font-semibold">DELETE</span> to
            confirm.
          </p>
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={confirmText}
              onChange={(e) => setConfirmText(e.target.value)}
              placeholder="DELETE"
              className="border border-slate-300 rounded px-2 py-1 text-sm w-40"
            />
            <button
              onClick={doDelete}
              disabled={confirmText !== "DELETE" || deleting}
              className="text-xs px-3 py-1.5 rounded bg-bad text-white hover:opacity-90 disabled:opacity-50"
            >
              {deleting ? "Deleting…" : "Delete workspace"}
            </button>
          </div>
          {deleteError && <p className="text-xs text-bad mt-2">{deleteError}</p>}
        </section>
      )}
    </div>
  );
}
