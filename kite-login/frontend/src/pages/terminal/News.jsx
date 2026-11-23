import { useMemo, useState } from "react";
import { num, signed, tone } from "../../lib/format.js";
import { useTicks } from "../../lib/ticks.js";
import { Segmented, Spinner } from "../../components/ui.jsx";
import { Logo } from "../../components/Logo.jsx";
import PriceAlerts from "../../components/PriceAlerts.jsx";
import { useWatchlist } from "../../components/watchlist.jsx";

// Market news on one page. Priority first: stock alerts (a named stock with a material trigger, and NSE's own filings) and global
// events (war, central banks, tariffs, crude, crashes...), classified by the backend as each headline arrives. Then the lead story and
// every headline by recency, with the stocks it names and their live move. The rail holds the most-mentioned stocks, your price
// alerts and the outlets.
const RANGES = [{ id: "today", label: "Today" }, { id: "24h", label: "24 hours" }, { id: "all", label: "All" }];
const MAX_TICKS = 40;

const outlet = (n) => n.publisher || n.source_label || n.source;
const hhmm = (d) => d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false });
function ago(d, now) {
  const m = Math.max(0, Math.round((now - d) / 60000));
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  return h < 24 ? `${h} h ago` : `${Math.round(h / 24)} d ago`;
}
function bucket(d, now) {
  const mins = (now - d) / 60000;
  if (mins <= 60) return "Last hour";
  if (d.toDateString() === now.toDateString()) return "Earlier today";
  const y = new Date(now); y.setDate(y.getDate() - 1);
  return d.toDateString() === y.toDateString() ? "Yesterday" : "Older";
}

function Tickers({ list, ticks, openStock }) {
  return list.map((t) => {
    const pc = ticks[`NSE:${t}`]?.change_pct;
    return (
      <button key={t} type="button" className="nw-tick" onClick={(e) => { e.preventDefault(); e.stopPropagation(); openStock(t); }} title={`Open ${t}`}>
        <Logo symbol={t} size={14} /><b>{t}</b>{pc != null && <span className={tone(pc)}>{signed(pc)}%</span>}
      </button>
    );
  });
}

function Story({ n, now, ticks, openStock, fresh }) {
  return (
    <li className={`nw-item${fresh ? " fresh" : ""}`}>
      <time className="nw-time">{hhmm(n.when)}</time>
      <a className="nw-body" href={n.link} target="_blank" rel="noreferrer">
        <span className="nw-title">{fresh && <i className="nw-new" aria-label="new" />}{n.title}</span>
        <span className="nw-meta">
          <span className="nw-outlet">{outlet(n)}</span><span className="nw-dot">·</span><span>{ago(n.when, now)}</span>
          {n.tickers.length > 0 && <span className="nw-ticks"><Tickers list={n.tickers} ticks={ticks} openStock={openStock} /></span>}
        </span>
      </a>
    </li>
  );
}

function PriorityLane({ title, sub, items, now, ticks, openStock, watched, kind }) {
  return (
    <section className={`card nw-lane ${kind}`}>
      <header className="nw-lane-head">
        <div><h3><i className={`nw-lane-dot ${kind}`} />{title}</h3><span className="muted small">{sub}</span></div>
        <span className="nw-lane-count">{items.length}<small> in 24 h</small></span>
      </header>
      {items.length ? (
        <ul className="nw-lane-list">
          {items.map((n) => {
            const mine = n.tickers.some((t) => watched.has(t));
            return (
              <li key={n.id} className={`${n.priority.level}${mine ? " mine" : ""}`}>
                <a href={n.link} target="_blank" rel="noreferrer">
                  <span className="nw-lane-tags">
                    {mine && <em className="watch">Watchlist</em>}
                    {n.priority.tags.map((t) => <em key={t} className={n.priority.level}>{t}</em>)}
                    <time>{hhmm(n.when)} · {ago(n.when, now)}</time>
                  </span>
                  <span className="nw-lane-title">{n.title}</span>
                  <span className="nw-meta"><span className="nw-outlet">{outlet(n)}</span>{n.tickers.length > 0 && <span className="nw-ticks"><Tickers list={n.tickers} ticks={ticks} openStock={openStock} /></span>}</span>
                </a>
              </li>
            );
          })}
        </ul>
      ) : <p className="nw-empty small">Nothing in the last 24 hours.</p>}
    </section>
  );
}

function Headlines({ news, openStock, feed, reloadFeed }) {
  const wl = useWatchlist();
  const watched = new Set(wl.list.map((k) => k.split(":")[1]));
  const [search, setSearch] = useState("");
  const [range, setRange] = useState("today");
  const [src, setSrc] = useState("all");
  const now = new Date();
  const all = useMemo(() => (news.items || []).map((n) => ({ ...n, when: new Date(n.published || Date.now()), tickers: n.tickers || [] })), [news.items]);
  const inRange = all.filter((n) => range === "all" || (range === "today" ? n.when.toDateString() === now.toDateString() : now - n.when <= 86_400_000));
  const outlets = useMemo(() => Object.entries(inRange.reduce((a, n) => ({ ...a, [outlet(n)]: (a[outlet(n)] || 0) + 1 }), {})).sort((a, b) => b[1] - a[1]), [inRange]);
  const words = search.trim().toLowerCase();
  const recent = all.filter((n) => now - n.when <= 86_400_000);
  const rank = (n) => (n.tickers.some((t) => watched.has(t)) ? 0 : 1) * 2 + (n.priority?.level === "high" ? 0 : 1);
  const pri = (lane) => recent.filter((n) => n.priority?.lane === lane).sort((x, y) => rank(x) - rank(y) || y.when - x.when).slice(0, 25);
  const stockLane = pri("stock"), globalLane = pri("global");
  const inLanes = new Set([...stockLane, ...globalLane].map((n) => n.id));          // shown above: not repeated in the feed
  const items = inRange.filter((n) => !inLanes.has(n.id) && (src === "all" || outlet(n) === src) && (!words || n.title.toLowerCase().includes(words) || n.tickers.some((t) => t.toLowerCase().includes(words))));
  const mentions = useMemo(() => Object.entries(inRange.flatMap((n) => n.tickers).reduce((a, t) => ({ ...a, [t]: (a[t] || 0) + 1 }), {})).sort((a, b) => b[1] - a[1]).slice(0, 10), [inRange]);
  const keys = [...new Set([...all.filter((n) => n.priority?.lane === "stock" && now - n.when <= 86_400_000).slice(0, 16).flatMap((n) => n.tickers), ...mentions.map(([t]) => t), ...items.slice(0, 40).flatMap((n) => n.tickers)])].slice(0, MAX_TICKS).map((t) => `NSE:${t}`);
  const { ticks, live } = useTicks(keys);
  const [lead, ...rest] = items;
  const groups = rest.reduce((a, n) => { const b = bucket(n.when, now); (a[b] ||= []).push(n); return a; }, {});

  return (
    <div className="nw-page">
      <div className="nw-priority">
        <PriorityLane title="Stock alerts" sub="named stocks with a material trigger · NSE filings" items={stockLane} now={now} ticks={ticks} openStock={openStock} watched={watched} kind="stock" />
        <PriorityLane title="Global events" sub="war, central banks, tariffs, crude, crashes" items={globalLane} now={now} ticks={ticks} openStock={openStock} watched={watched} kind="global" />
      </div>
      <section className="card nw-feed">
        <header className="nw-head">
          <div className="nw-head-l">
            <h3>Market news</h3>
            <span className="live-chip"><span className="live-dot" /> Live</span>
            <span className="muted small">{items.length} headlines</span>
          </div>
          <input className="nw-search" type="search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search headlines or a stock" aria-label="Search headlines" />
        </header>
        <div className="nw-filters">
          <Segmented size="small" label="Time range" value={range} onChange={setRange} options={RANGES} />
          <div className="nw-chips">
            <button type="button" className={`nw-chip${src === "all" ? " on" : ""}`} onClick={() => setSrc("all")}>All outlets</button>
            {outlets.slice(0, 7).map(([o, c]) => <button key={o} type="button" className={`nw-chip${src === o ? " on" : ""}`} onClick={() => setSrc(src === o ? "all" : o)}>{o} <em>{c}</em></button>)}
          </div>
        </div>

        {news.loading && !news.items?.length ? <div className="nw-empty"><Spinner /></div> : !lead ? (
          <p className="nw-empty">No headlines {range === "today" ? "today yet" : "in this range"}{words ? ` for "${search}"` : ""}.</p>
        ) : (
          <>
            <a className={`nw-lead${news.fresh?.has(lead.id) ? " fresh" : ""}`} href={lead.link} target="_blank" rel="noreferrer">
              <span className="nw-kicker">{bucket(lead.when, now)} · {outlet(lead)}</span>
              <span className="nw-lead-title">{lead.title}</span>
              {lead.summary && <span className="nw-lead-sum">{lead.summary}</span>}
              <span className="nw-meta"><span>{hhmm(lead.when)} · {ago(lead.when, now)}</span>{lead.tickers.length > 0 && <span className="nw-ticks"><Tickers list={lead.tickers} ticks={ticks} openStock={openStock} /></span>}</span>
            </a>
            {["Last hour", "Earlier today", "Yesterday", "Older"].filter((g) => groups[g]?.length).map((g) => (
              <div key={g} className="nw-group">
                <div className="nw-group-head"><span>{g}</span><em>{groups[g].length}</em></div>
                <ul className="nw-list">{groups[g].map((n) => <Story key={n.id} n={n} now={now} ticks={ticks} openStock={openStock} fresh={news.fresh?.has(n.id)} />)}</ul>
              </div>
            ))}
          </>
        )}
      </section>

      <aside className="nw-rail">
        <section className="card nw-box">
          <header className="nw-box-head"><h3>Most mentioned</h3><span className="muted small">{range === "today" ? "today" : range === "24h" ? "24 hours" : "all headlines"}</span></header>
          {mentions.length ? (
            <ol className="nw-rank">
              {mentions.map(([t, c], i) => {
                const tk = ticks[`NSE:${t}`];
                return (
                  <li key={t}>
                    <span className="nw-rank-n">{i + 1}</span>
                    <button type="button" className="nw-rank-name" onClick={() => openStock(t)}><Logo symbol={t} size={20} /><b>{t}</b></button>
                    <span className="nw-rank-c">{c} {c === 1 ? "story" : "stories"}</span>
                    <span className="nw-rank-q">
                      <b className={tone(tk?.change_pct)}>{tk ? `${signed(tk.change_pct)}%` : "–"}</b>
                      <small>{tk ? num(tk.last_price) : live ? "no quote" : "–"}</small>
                    </span>
                  </li>
                );
              })}
            </ol>
          ) : <p className="nw-empty small">No stock named in these headlines yet.</p>}
        </section>
        <PriceAlerts feed={feed} reloadFeed={reloadFeed} openStock={openStock} />
        <section className="card nw-box">
          <header className="nw-box-head"><h3>Outlets</h3><span className="muted small">{inRange.length} headlines</span></header>
          <ul className="nw-outlets">
            {outlets.map(([o, c]) => (
              <li key={o}>
                <button type="button" className={src === o ? "on" : ""} onClick={() => setSrc(src === o ? "all" : o)} title={`${o}: ${c} headline${c === 1 ? "" : "s"}`}>
                  <span>{o}</span><span className="nw-bar"><span style={{ width: `${(c / (outlets[0]?.[1] || 1)) * 100}%` }} /></span><b>{c}</b>
                </button>
              </li>
            ))}
          </ul>
        </section>
      </aside>
    </div>
  );
}

export default function News({ news, feed, reloadFeed, openStock }) {
  return (
    <div className="tt-page">
      <Headlines news={news} openStock={openStock} feed={feed} reloadFeed={reloadFeed} />
    </div>
  );
}
