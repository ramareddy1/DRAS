const KEY = "reconops_history_v1";

export function loadHistory() {
  try { return JSON.parse(localStorage.getItem(KEY)) || []; }
  catch { return []; }
}

export function saveHistoryItem(item) {
  const list = loadHistory().filter((x) => x.job_id !== item.job_id);
  list.unshift(item);
  localStorage.setItem(KEY, JSON.stringify(list.slice(0, 50)));
}

export function clearHistory() {
  localStorage.removeItem(KEY);
}
