import { useState } from "react";
import { api } from "../lib/api.js";
import { num, signed, tone } from "../lib/format.js";
import { useTicks } from "../lib/ticks.js";
import { ErrorNote } from "./ui.jsx";
import { SymbolSearch } from "./watchlist.jsx";

// Your price alerts, in the News rail: what fired (unread first), what is waiting, and a compact form for a new one.
// The backend checks them every 10 s; a hit pops up in the app (and on the desktop if allowed). Nothing here sends an order.
const COND = [
  { id: "above", label: "Crosses above" },
  { id: "below", label: "Crosses below" },
  { id: "chg_above", label: "Day change above %" },
  { id: "chg_below", label: "Day change below %" },
];
const hm = (iso) => (iso ? new Date(iso).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false }) : "");

export default function PriceAlerts({ feed, reloadFeed, openStock }) {
  const [tab, setTab] = useState("fired");
  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState({ symbol: "", exchange: "NSE", cond: "above", value: "" });
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const { ticks } = useTicks(form.symbol ? [`${form.exchange}:${form.symbol}`] : []);
  const live = form.symbol ? ticks[`${form.exchange}:${form.symbol}`] : null;
  const alerts = feed?.alerts || [];
  const fired = alerts.filter((a) => a.status === "triggered").sort((a, b) => Number(a.read) - Number(b.read) || String(b.triggered_at).localeCompare(String(a.triggered_at)));
  const waiting = alerts.filter((a) => a.status === "active");
  const shown = tab === "fired" ? fired.slice(0, 12) : waiting;

  const act = async (fn) => {
    try { await fn(); reloadFeed(); } catch (x) { setErr(x.message); }
  };
  const save = async (e) => {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      await api.alertCreate({ symbol: form.symbol, exchange: form.exchange, cond: form.cond, value: Number(form.value) });
      setForm((f) => ({ ...f, symbol: "", value: "" }));
      setAdding(false);
      setTab("waiting");
      reloadFeed();
    } catch (x) {
      setErr(x.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card nw-box pa">
      <header className="nw-box-head">
        <h3>Price alerts</h3>
        {feed?.unread > 0 ? <button type="button" className="link-accent small" onClick={() => act(() => api.alertsRead())}>Mark {feed.unread} read</button>
          : <button type="button" className="link-accent small" onClick={() => setAdding((v) => !v)}>{adding ? "Cancel" : "+ New alert"}</button>}
      </header>
      <div className="pa-tabs">
        <button type="button" className={tab === "fired" ? "on" : ""} onClick={() => setTab("fired")}>Fired {feed?.unread ? <em>{feed.unread}</em> : null}</button>
        <button type="button" className={tab === "waiting" ? "on" : ""} onClick={() => setTab("waiting")}>Waiting <em>{waiting.length}</em></button>
      </div>
      <ul className="pa-list">
        {shown.map((a) => (
          <li key={a.id} className={`${a.status}${a.read ? "" : " unread"}`}>
            <button type="button" className="pa-main" onClick={() => a.symbol && openStock(a.symbol, a.exchange || "NSE")}>
              <i className={`pa-dot ${a.kind === "system" ? "sys" : a.status}`} />
              <span><b>{a.title}</b><small>{a.status === "active" ? `waiting · set at ${num(a.ref_price)}` : a.body}</small></span>
              <time>{hm(a.triggered_at || a.created)}</time>
            </button>
            <button type="button" className="pa-x" onClick={() => act(() => api.alertDelete(a.id))} aria-label={`Delete ${a.title}`}>×</button>
          </li>
        ))}
        {!shown.length && <li className="pa-empty">{tab === "fired" ? "No alert has fired." : "No alert is waiting."}</li>}
      </ul>
      {(adding || (!alerts.length)) && (
        <form className="pa-form" onSubmit={save}>
          {form.symbol ? (
            <span className="pa-picked"><b>{form.symbol}</b>{live && <small className={tone(live.change_pct)}>{num(live.last_price)} · {signed(live.change_pct)}%</small>}
              <button type="button" className="pa-x" onClick={() => setForm((f) => ({ ...f, symbol: "" }))} aria-label="Clear stock">×</button></span>
          ) : <SymbolSearch compact placeholder="Stock, e.g. TCS" onPick={(s, ex) => setForm((f) => ({ ...f, symbol: s, exchange: ex }))} />}
          <div className="pa-row">
            <select value={form.cond} onChange={(e) => setForm((f) => ({ ...f, cond: e.target.value }))} aria-label="Condition">
              {COND.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
            </select>
            <input type="number" step="any" required value={form.value} onChange={(e) => setForm((f) => ({ ...f, value: e.target.value }))} aria-label="Value"
              placeholder={live ? (form.cond.startsWith("chg") ? "2" : num(live.last_price)) : form.cond.startsWith("chg") ? "%" : "₹"} />
          </div>
          <button type="submit" className="primary" disabled={busy || !form.symbol || form.value === ""}>{busy ? "Saving…" : "Save alert"}</button>
        </form>
      )}
      <ErrorNote error={err} />
    </section>
  );
}
