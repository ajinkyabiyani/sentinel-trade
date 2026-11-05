// Thin wrapper over the FastAPI backend. Responses never contain the access token.
export const LOGGED_OUT_EVENT = "kite:logged-out";

async function request(path, options = {}) {
  let res;
  try {
    res = await fetch(path, {
      ...options,
      headers: { "Content-Type": "application/json", "X-Kite-App": "1", ...(options.headers || {}) },
    });
  } catch {
    throw new Error("Cannot reach the backend. Is FastAPI running on port 8000?");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data.detail;
    const msg = Array.isArray(detail)
      ? detail.map((d) => (d.loc?.length > 1 ? `${d.loc.at(-1)}: ` : "") + d.msg.replace(/^Value error, /, "")).join("; ")
      : detail || `Request failed (${res.status})`;
    if (res.status === 401 && !path.startsWith("/api/login")) {
      window.dispatchEvent(new CustomEvent(LOGGED_OUT_EVENT, { detail: msg }));
    }
    throw new Error(msg);
  }
  return data;
}

const post = (path, body) => request(path, { method: "POST", body: JSON.stringify(body ?? {}) });
const qs = (params) => new URLSearchParams(params).toString();

export const api = {
  session: () => request("/api/session"),
  loginConfig: () => request("/api/login/config"),
  loginUrl: (apiKey) => request(`/api/login-url?${qs({ api_key: apiKey })}`),
  login: (body) => post("/api/login", body),
  logout: () => post("/api/logout"),

  profile: () => request("/api/profile"),
  margins: () => request("/api/margins"),

  marketIndices: () => request("/api/market/indices"),
  terminalStocks: () => request("/api/t/stocks"),
  terminalIndices: () => request("/api/t/indices"),
  terminalNormalized: (range) => request(`/api/t/normalized?${qs({ range })}`),
  terminalMood: () => request("/api/t/mood"),
  terminalPreopen: () => request("/api/t/preopen"),
  terminalFlows: () => request("/api/t/flows"),
  terminalCalendar: () => request("/api/t/calendar"),
  terminalFundamental: (symbol) => request(`/api/t/fundamental/${encodeURIComponent(symbol)}`),
  terminalInsiders: () => request("/api/t/insiders"),
  search: (q, exchange) => request(`/api/instruments/search?${qs({ q, exchange })}`),
  quote: (keys) => request(`/api/quote?${keys.map((k) => `i=${encodeURIComponent(k)}`).join("&")}`),

  orderMargin: (order) => post("/api/orders/margin", order),
  placeOrder: (order) => post("/api/orders", order),
  orders: () => request("/api/orders"),
  modifyOrder: (variety, id, body) =>
    request(`/api/orders/${variety}/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  cancelOrder: (variety, id, parent) =>
    request(`/api/orders/${variety}/${id}${parent ? `?${qs({ parent_order_id: parent })}` : ""}`, { method: "DELETE" }),
  trades: () => request("/api/trades"),
  positions: () => request("/api/positions"),
  convertPosition: (body) => post("/api/positions/convert", body),
  holdings: () => request("/api/holdings"),

  optionUnderlyings: () => request("/api/options/underlyings"),
  optionChain: (underlying, expiry) => request(`/api/options/chain?${qs({ underlying, ...(expiry ? { expiry } : {}) })}`),

  gtts: () => request("/api/gtt"),
  gttProtect: (body) => post("/api/gtt/protect", body),
  gttBuyStop: (body) => post("/api/gtt/buy-stop", body),
  gttDelete: (id) => request(`/api/gtt/${id}`, { method: "DELETE" }),

  darvasResults: (stamp) => request(`/api/darvas/results${stamp ? `?${qs({ stamp })}` : ""}`),
  darvasUniverses: () => request("/api/darvas/universes"),
  darvasRun: (body) => post("/api/darvas/run", body),
  darvasStatus: () => request("/api/darvas/status"),
  darvasStop: () => post("/api/darvas/stop"),

  orbState: () => request("/api/orb/state"),
  orbSettings: (body) => post("/api/orb/settings", body),
  orbChart: (symbol) => request(`/api/orb/chart?${qs({ symbol })}`),
  orbPaper: () => request("/api/orb/paper"),
  portfolio: () => request("/api/portfolio"),
  backtestOrb: () => request("/api/backtest/orb"),
  backtestOrbRun: (body) => post("/api/backtest/orb", body),
  backtestDarvas: () => request("/api/backtest/darvas"),
  backtestDarvasRun: (body) => post("/api/backtest/darvas", body),
  momentum: () => request("/api/momentum"),
  regimeMarket: () => request("/api/regime/market"),
  regimeStocks: () => request("/api/regime/stocks"),
  improve: () => request("/api/improve"),
  improveRun: (strategy) => post(`/api/improve/run${strategy ? `?strategy=${strategy}` : ""}`),
  improveApprove: (k) => post(`/api/improve/${k}/approve`),
  improveDismiss: (k) => post(`/api/improve/${k}/dismiss`),
  improveRollback: (id) => post(`/api/improve/rollback/${encodeURIComponent(id)}`),
  orbPause: (paused) => post("/api/orb/pause", { paused }),
  orbKill: () => post("/api/orb/kill"),
  paperOptions: () => request("/api/paper/options"),
  paperOptionsOpen: (body) => post("/api/paper/options", body),
  paperOptionsExit: (id) => post(`/api/paper/options/${id}/exit`),
  paperOptionsDelete: (id) => request(`/api/paper/options/${id}`, { method: "DELETE" }),
  paperEquity: (body) => post("/api/paper/equity", body),
  peers: (symbol) => request(`/api/stock/peers?${qs({ symbol })}`),
  alerts: () => request("/api/alerts"),
  alertCreate: (body) => post("/api/alerts", body),
  alertDelete: (id) => request(`/api/alerts/${id}`, { method: "DELETE" }),
  alertsRead: (id) => post(`/api/alerts/read${id ? `?${qs({ ids: id })}` : ""}`),

  news: (source = "all", limit = 120) => request(`/api/news?${qs({ source, limit })}`),
  movers: () => request("/api/market/movers"),
  stock: (symbol, exchange = "NSE", range = "1D") => request(`/api/stock?${qs({ symbol, exchange, range })}`),
};
