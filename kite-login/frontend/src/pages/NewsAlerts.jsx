import { useMemo, useState } from "react";
import { api } from "../lib/api.js";
import { NEWS_SOURCES, ago } from "../components/news.jsx";
import { useTicks } from "../lib/ticks.js";
import { num, signed, tone } from "../lib/format.js";
import { Empty, ErrorNote } from "../components/ui.jsx";
import { SymbolSearch, useWatchlist } from "../components/watchlist.jsx";

// News & Smart Alerts: merged market headlines (all / watchlist), and in-app alerts on price or day change,
// checked every 10 s by the backend, plus paper stop / target hits. Nothing here sends a message or an order.
const COND = [
  { id: "above", label: "Price crosses above" },
  { id: "below", label: "Price crosses below" },
  { id: "chg_above", label: "Day change above (%)" },
  { id: "chg_below", label: "Day change below (%)" },
];
const hm = (iso) => (iso ? new Date(iso).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" }) : "");

export default function NewsAlerts({ news, feed, reloadFeed, openStock }) {
  const [tab, setTab] = useState("all");
  const [source, setSource] = useState("all");
  const [q, setQ] = useState("");
  const [atab, setAtab] = useState("unread");
  const [open, setOpen] = useState(null);
  const [form, setForm] = useState({ symbol: "", exchange: "NSE", cond: "above", value: "" });
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const wl = useWatchlist();
  const { ticks } = useTicks(form.symbol ? [`${form.exchange}:${form.symbol}`] : []);
  const live = form.symbol ? ticks[`${form.exchange}:${form.symbol}`] : null;

  const wlWords = wl.list.map((k) => k.split(":")[1].toLowerCase());
  const items = useMemo(() => (news.items || []).filter((n) => {
    if (source !== "all" && n.source !== source) return false;
    const t = `${n.title} ${n.summary || ""}`.toLowerCase();
    if (tab === "watch" && !wlWords.some((w) => t.includes(w))) return false;
    return !q || t.includes(q.toLowerCase());
  }), [news.items, source, tab, q, wlWords.join()]); // eslint-disable-line react-hooks/exhaustive-deps
  const tags = (n) => wlWords.filter((w) => n.title.toLowerCase().includes(w)).map((w) => w.toUpperCase());

  const alerts = feed.alerts || [];
  const shown = alerts.filter((a) => (atab === "unread" ? a.status === "triggered" && !a.read : atab === "active" ? a.status === "active" : atab === "history" ? a.status === "triggered" : true));

  const save = async (e) => {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      await api.alertCreate({ symbol: form.symbol, exchange: form.exchange, cond: form.cond, value: Number(form.value) });
      setForm((f) => ({ ...f, value: "" }));
      setAtab("active");
      reloadFeed();
    } catch (x) {
      setErr(x.message);
    } finally {
      setBusy(false);
    }
  };
  const act = async (fn) => {
    try {
      await fn();
      reloadFeed();
    } catch (x) {
      setErr(x.message);
    }
  };

  return (
    <div className="na fade-in">
      <div className="na-grid">
        <section className="card na-news">
          <header className="na-head">
            <div className="si-tabs flat" role="tablist">
              {[["all", "All news"], ["watch", "Watchlist"]].map(([id, l]) => (
                <button key={id} type="button" role="tab" aria-selected={tab === id} className={tab === id ? "on" : ""} onClick={() => setTab(id)}>{l}</button>
              ))}
            </div>
            <div className="row">
              <input className="na-search" placeholder="Search news, e.g. TCS, IT, RBI…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search news" />
              <select value={source} onChange={(e) => setSource(e.target.value)} aria-label="News source" className="select-pill">
                {NEWS_SOURCES.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
              </select>
            </div>
          </header>
          {items.length ? (
            <ul className="na-list">
              {items.slice(0, 80).map((n) => (
                <li key={n.id} className={news.fresh.has(n.id) ? "is-new" : ""}>
                  <i className="na-dot" aria-hidden="true" />
                  <a href={n.link} target="_blank" rel="noreferrer noopener">
                    <b>{n.title}</b>
                    {n.summary && <span className="muted">{n.summary}</span>}
                    <small className="muted">{n.publisher || n.source_label} · {ago(n.published)}</small>
                  </a>
                  <span className="na-tags">{tags(n).slice(0, 2).map((t) => <em key={t}>{t}</em>)}</span>
                </li>
              ))}
            </ul>
          ) : <Empty>{tab === "watch" ? "No headline mentions a watchlist stock right now." : "No headlines match."}</Empty>}
          <footer className="muted small na-foot">
            {news.fetchedAt ? `Feeds checked ${ago(news.fetchedAt)} · Yahoo Finance, Economic Times, Mint, Business Standard, Google News` : "Loading feeds…"}
            {news.fresh.size > 0 && <button type="button" className="news-new-btn" onClick={news.markRead}>{news.fresh.size} new · mark read</button>}
          </footer>
        </section>

        <aside className="card na-alerts">
          <header className="na-alerts-head">
            <h2>Alerts</h2>
            {feed.unread > 0 && <span className="unread-pill">Unread <b>{feed.unread}</b></span>}
            {feed.unread > 0 && <button type="button" className="ghost small" onClick={() => act(() => api.alertsRead())}>Mark all read</button>}
          </header>
          <div className="si-tabs flat small" role="tablist">
            {[["unread", `Unread (${feed.unread})`], ["active", `Active (${alerts.filter((a) => a.status === "active").length})`], ["history", "History"]].map(([id, l]) => (
              <button key={id} type="button" role="tab" aria-selected={atab === id} className={atab === id ? "on" : ""} onClick={() => setAtab(id)}>{l}</button>
            ))}
          </div>
          <ul className="al-list">
            {shown.map((a) => (
              <li key={a.id} className={`${a.status}${a.read ? " read" : ""}`}>
                <button type="button" className="al-row" onClick={() => setOpen(open === a.id ? null : a.id)} aria-expanded={open === a.id}>
                  <i className={`al-dot ${a.kind === "system" ? "sys" : a.status}`} />
                  <span>
                    <b>{a.title}</b>
                    <small className="muted">{a.status === "active" ? `waiting · set at ${num(a.ref_price)}` : a.body}</small>
                  </span>
                  <small className="muted">{hm(a.triggered_at || a.created)}</small>
                </button>
                {open === a.id && (
                  <div className="al-detail">
                    {a.kind === "price" && (
                      <div className="al-cond">
                        <span><small>Condition</small><b>{a.symbol} {COND.find((c) => c.id === a.cond)?.label.toLowerCase()} {a.value}</b></span>
                        <span><small>Delivery</small><b>In-app{"Notification" in window && Notification.permission === "granted" ? " + desktop" : ""}</b></span>
                      </div>
                    )}
                    <div className="row">
                      {a.symbol && <button type="button" className="ghost small" onClick={() => openStock(a.symbol, a.exchange || "NSE")}>Open {a.symbol}</button>}
                      {a.status === "triggered" && !a.read && <button type="button" className="ghost small" onClick={() => act(() => api.alertsRead(a.id))}>✓ Mark read</button>}
                      <button type="button" className="ghost small danger-text" onClick={() => act(() => api.alertDelete(a.id))}>Delete</button>
                    </div>
                  </div>
                )}
              </li>
            ))}
            {!shown.length && <li className="muted small al-empty">{atab === "unread" ? "No unread alerts." : atab === "active" ? "No active alerts. Create one below." : "Nothing yet."}</li>}
          </ul>
        </aside>
      </div>

      <section className="card na-create">
        <div>
          <h3>Create alert</h3>
          <span className="muted small">Get notified in the app (and on the desktop, if allowed) when the condition is met.</span>
        </div>
        <form onSubmit={save} className="na-form">
          <label className="field">
            <span>Ticker</span>
            {form.symbol ? (
              <span className="na-picked">
                <b>{form.symbol}</b>
                {live && <small className={tone(live.change_pct)}>{num(live.last_price)} · {signed(live.change_pct)}%</small>}
                <button type="button" className="leg-x" onClick={() => setForm((f) => ({ ...f, symbol: "" }))} aria-label="Clear ticker">×</button>
              </span>
            ) : <SymbolSearch compact placeholder="Search, e.g. TCS" onPick={(s, ex) => setForm((f) => ({ ...f, symbol: s, exchange: ex }))} />}
          </label>
          <label className="field">
            <span>Condition</span>
            <select value={form.cond} onChange={(e) => setForm((f) => ({ ...f, cond: e.target.value }))}>
              {COND.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
            </select>
          </label>
          <label className="field">
            <span>{form.cond.startsWith("chg") ? "Value (%)" : "Value (₹)"}</span>
            <input type="number" step="any" required value={form.value} onChange={(e) => setForm((f) => ({ ...f, value: e.target.value }))}
              placeholder={live ? (form.cond.startsWith("chg") ? "e.g. 2" : num(live.last_price)) : ""} />
          </label>
          <button type="submit" className="primary" disabled={busy || !form.symbol || form.value === ""}>{busy ? "Saving…" : "Save alert"}</button>
        </form>
        <ErrorNote error={err} />
      </section>
    </div>
  );
}
