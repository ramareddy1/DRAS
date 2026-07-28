import { useState } from "react";
import { fetchShopifyOrders } from "../api/client.js";

export default function ShopifyFetchPanel({ onFetched }) {
  const today = new Date().toISOString().slice(0, 10);
  const [startDate, setStartDate] = useState(today);
  const [endDate, setEndDate] = useState(today);
  const [fetching, setFetching] = useState(false);
  const [error, setError] = useState("");

  const doFetch = async () => {
    setFetching(true);
    setError("");
    try {
      const blob = await fetchShopifyOrders({ start_date: startDate, end_date: endDate });
      const filename = `shopify-orders-${startDate}_${endDate}.csv`;
      const file = new File([blob], filename, { type: "text/csv" });
      onFetched(file);
    } catch (e) {
      setError(e.message);
    } finally {
      setFetching(false);
    }
  };

  return (
    <div className="border-2 border-dashed border-slate-300 rounded-lg p-4 bg-white">
      <div className="flex items-center gap-2 mb-3">
        <input
          type="date"
          value={startDate}
          onChange={(e) => setStartDate(e.target.value)}
          className="border border-slate-300 rounded px-2 py-1 text-sm"
        />
        <span className="text-slate-400 text-sm">to</span>
        <input
          type="date"
          value={endDate}
          onChange={(e) => setEndDate(e.target.value)}
          className="border border-slate-300 rounded px-2 py-1 text-sm"
        />
      </div>
      <button
        onClick={doFetch}
        disabled={fetching || !startDate || !endDate}
        className="text-xs px-3 py-1.5 rounded bg-brand text-white hover:opacity-90 disabled:opacity-50"
      >
        {fetching ? "Fetching…" : "Fetch orders"}
      </button>
      {error && <p className="text-xs text-bad mt-2">{error}</p>}
    </div>
  );
}
