import { useMemo, useState } from "react";
import { api } from "../../lib/api.js";
import { useTicks } from "../../lib/ticks.js";
import { Segmented, Spinner, useLoad } from "../../components/ui.jsx";
import NewsAlerts from "../NewsAlerts.jsx";
import { HeatLegend, SectorBars, Treemap, useStocks } from "./shared.jsx";

// Breaking news as a table with the stocks it mentions, beside sector performance and the heatmap. Smart alerts are the
// second view.
const SHORT_NAME = /\b(ltd|limited|india|industries|the|company|corporation|corp|co)\b\.?/gi;

// Stocks named in a headline: the NSE symbol as a capitalised word, or the company's full short name.
export function tickersIn(title, matchers) {
  const found = [];
  for (const m of matchers) {
    if (m.re.test(title)) found.push(m.symbol);
    if (found.length === 3) break;
  }
  return found;
}

export function buildMatchers(rows) {
  return rows.map((r) => {
    const name = (r.name || "").replace(SHORT_NAME, " ").replace(/[^\w&\s-]/g, " ").replace(/\s+/g, " ").trim();
    const parts = [r.symbol.length >= 4 ? r.symbol.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") : null,
      name.length >= 5 ? name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") : null].filter(Boolean);
    return parts.length ? { symbol: r.symbol, re: new RegExp(`(^|[^\\w])(${parts.join("|")})($|[^\\w])`) } : null;
  }).filter(Boolean);
}

const clock = (iso) => (iso ? new Date(iso).toLocaleTimeString("en-IN", { hour: "numeric", minute: "2-digit" }) : "–");
const sameDay = (iso) => new Date(iso).toDateString() === new Date().toDateString();

function Headlines({ news, openStock }) {
  const { rows, loading } = useStocks();
  const idx = useLoad(api.terminalIndices, [], 60_000);
  const { ticks } = useTicks((idx.data?.sectors || []).map((s) => s.key));
  const [search, setSearch] = useState("");
  const matchers = useMemo(() => buildMatchers(rows), [rows.length]); // eslint-disable-line react-hooks/exhaustive-deps
  const items = useMemo(() => {
    const words = search.trim().toLowerCase();
    return (news.items || []).map((n) => ({ ...n, tickers: tickersIn(n.title, matchers) }))
      .filter((n) => !words || n.title.toLowerCase().includes(words) || n.tickers.some((t) => t.toLowerCase().includes(words)));
  }, [news.items, matchers, search]);
  return (
    <div className="news-grid">
      <section className="card tt-card">
        <header className="card-head">
          <div className="row">
            <h3>Breaking news</h3>
            <span className="count-chip">{items.length}</span>
            <span className="live-chip"><span className="live-dot" /> Live</span>
          </div>
          <input className="tt-search" type="search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search headlines or ticker" aria-label="Search headlines" />
        </header>
        {news.loading ? <Spinner /> : (
          <div className="table-wrap news-table">
            <table className="table">
              <thead><tr><th>Time</th><th>Tickers</th><th>Headline</th><th>Source</th></tr></thead>
              <tbody>
                {items.map((n) => (
                  <tr key={n.id} className={news.fresh?.has(n.id) ? "fresh" : ""}>
                    <td className="muted small nowrap">{n.published ? (sameDay(n.published) ? clock(n.published) : new Date(n.published).toLocaleDateString("en-IN", { day: "numeric", month: "short" })) : "–"}</td>
                    <td className="nowrap">{n.tickers.map((t) => <button type="button" key={t} className="tick-tag" onClick={() => openStock(t)}>{t}</button>)}</td>
                    <td><a href={n.link} target="_blank" rel="noreferrer">{n.title}</a></td>
                    <td className="muted small nowrap">{n.source_name || n.source}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <div className="news-side">
        <SectorBars sectors={idx.data?.sectors} ticks={ticks} />
        <section className="card tt-card">
          <header className="card-head"><h3>Heatmap</h3><span className="muted small">today, Nifty 500</span></header>
          {loading ? <Spinner /> : <Treemap rows={rows} onPick={openStock} height={340} compact />}
          <footer className="tt-foot"><HeatLegend /></footer>
        </section>
      </div>
    </div>
  );
}

export default function News({ news, feed, reloadFeed, openStock }) {
  const [view, setView] = useState("news");
  return (
    <div className="tt-page">
      <Segmented value={view} onChange={setView} label="News view"
        options={[{ id: "news", label: "Breaking news" }, { id: "alerts", label: `News & smart alerts${feed?.unread ? ` (${feed.unread})` : ""}` }]} />
      {view === "news" ? <Headlines news={news} openStock={openStock} /> : <NewsAlerts news={news} feed={feed} reloadFeed={reloadFeed} openStock={openStock} />}
    </div>
  );
}
