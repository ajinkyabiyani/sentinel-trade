import { useMemo, useState } from "react";
import { api } from "../lib/api.js";
import { QUADRANT_TONE, RotationMap } from "../components/charts.jsx";
import { dateTime, num, shortSector, signed, tone } from "../lib/format.js";
import { liveRotation, sectorMoves, tickIsNew } from "../lib/live.js";
import { useTicks } from "../lib/ticks.js";
import { Badge, ErrorNote, Segmented, Spinner, useLoad } from "../components/ui.jsx";
import GroupCards from "./GroupCards.jsx";

const WINDOWS = [
  { id: "1w", label: "1W" },
  { id: "1m", label: "1M" },
  { id: "3m", label: "3M" },
  { id: "6m", label: "6M" },
  { id: "ytd", label: "YTD" },
];
const GROUPINGS = [
  { id: "sector", label: "Sectors" },
  { id: "segment", label: "Market cap" },
];
const INDEX_ORDER = ["SENSEX", "NIFTY 50", "NIFTY BANK", "NIFTY MIDCAP 100", "NIFTY SMLCAP 100"];
const INDEX_LABEL = { SENSEX: "Sensex", "NIFTY 50": "Nifty 50", "NIFTY BANK": "Nifty Bank", "NIFTY MIDCAP 100": "Nifty Midcap 100", "NIFTY SMLCAP 100": "Nifty Smallcap 100" };
const CAP_LABEL = { LARGE: "Large cap", MID: "Mid cap", SMALL: "Small cap", MICRO: "Micro cap" };

const mean = (xs) => {
  const v = xs.filter((x) => x !== null && x !== undefined && Number.isFinite(x));
  return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
};
const median = (xs) => {
  const v = xs.filter((x) => Number.isFinite(x)).sort((a, b) => a - b);
  if (!v.length) return null;
  const m = Math.floor(v.length / 2);
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
};
const label = (by, name) => (by === "segment" ? CAP_LABEL[name] || name : by === "index" ? INDEX_LABEL[name] || name : name);

// Equal-weight group returns, ranks now vs a week ago (1 = strongest), median RS and today's breadth.
function groupStats(stocks, keyOf) {
  const groups = {};
  for (const s of stocks) for (const k of keyOf(s)) if (k) (groups[k] ||= []).push(s);
  const rows = Object.entries(groups).map(([name, ms]) => {
    const r = { name, members: ms, n: ms.length, now: {}, prev: {} };
    for (const w of [...WINDOWS.map((x) => x.id), "1d"]) {
      r.now[w] = mean(ms.map((m) => m[`chg_${w}`]));
      r.prev[w] = mean(ms.map((m) => m[`prev_${w}`]));
    }
    r.medRs = median(ms.map((m) => m.rs_rating));
    r.up = ms.filter((m) => m.chg_1d > 0).length;
    return r;
  });
  for (const w of WINDOWS.map((x) => x.id)) {
    const rank = (key) =>
      [...rows].filter((r) => r[key][w] !== null).sort((a, b) => b[key][w] - a[key][w]).forEach((r, i) => ((r[`${key}Rank`] ||= {})[w] = i + 1));
    rank("now");
    rank("prev");
  }
  return rows;
}

function RankCell({ r, w }) {
  const now = r.nowRank?.[w];
  const prev = r.prevRank?.[w];
  const d = now && prev ? prev - now : 0;
  return (
    <td className="rank-cell">
      <b>{now ?? "–"}</b>
      {d !== 0 && <small className={d > 0 ? "up" : "down"}>{d > 0 ? "▲" : "▼"}{Math.abs(d)}</small>}
    </td>
  );
}

function RsPill({ v }) {
  if (v === null || v === undefined) return <span className="rs-pill">–</span>;
  return <span className={`rs-pill ${v >= 70 ? "hi" : v <= 30 ? "lo" : ""}`}>{Math.round(v)}</span>;
}

function ReturnBar({ v, max }) {
  if (v === null || v === undefined) return <span className="ret-bar" />;
  const w = Math.min(100, (Math.abs(v) / (max || 1)) * 100);
  return (
    <span className="ret-bar">
      <span className="ret-track">
        <span className={v >= 0 ? "pos" : "neg"} style={{ width: `${w / 2}%` }} />
      </span>
      <b className={tone(v)}>{signed(v, 1)}%</b>
    </span>
  );
}

// Sector score 0-100: RS 35% (median RS rating), breadth 20% (% above the 50-day average), trend 20% (3M return
// percentile), participation 15% (% up today), acceleration 10% (1M pace vs 3M pace, percentile).
function sectorScores(stocks) {
  const g = groupStats(stocks, (s) => [s.sector]).filter((x) => x.n >= 2);
  const pctile = (vals, v) => (v == null ? 50 : (vals.filter((x) => x != null && x < v).length / Math.max(1, vals.filter((x) => x != null).length - 1)) * 100);
  const r3 = g.map((x) => x.now["3m"]);
  const acc = g.map((x) => (x.now["1m"] != null && x.now["3m"] != null ? x.now["1m"] - x.now["3m"] / 3 : null));
  return g.map((x, i) => {
    const breadth = (x.members.filter((m) => m.above_50dma).length / x.n) * 100;
    const part = (x.up / x.n) * 100;
    const score = 0.35 * (x.medRs ?? 50) + 0.2 * breadth + 0.2 * pctile(r3, x.now["3m"]) + 0.15 * part + 0.1 * pctile(acc, acc[i]);
    return { ...x, breadth, part, score };
  }).sort((a, b) => b.score - a.score);
}

export default function Rotation({ openTicket, scanVersion, openStock }) {
  const res = useLoad(api.darvasResults, [scanVersion]);
  const [by, setBy] = useState("sector");
  const [win, setWin] = useState("3m");
  const [mapBy, setMapBy] = useState("sector");
  const [focus, setFocus] = useState(null); // { by, name }

  const d = res.data;
  const L = d?.leadership;
  const baseStocks = L?.stocks || [];
  const keys = useMemo(() => [...baseStocks.map((s) => `${s.exchange}:${s.symbol}`), "NSE:NIFTY 50"], [baseStocks]);
  const { ticks, live } = useTicks(keys);
  // today's move from live ticks replaces the last close's 1D figure during market hours
  const stocks = useMemo(() => baseStocks.map((s) => {
    const t = ticks[`${s.exchange}:${s.symbol}`];
    return tickIsNew(t, L?.asof) ? { ...s, chg_1d: t.change_pct, last_close: t.last_price, _live: true } : s;
  }), [baseStocks, ticks, L]);
  const anyLive = live && stocks.some((s) => s._live);
  const liveGroups = useMemo(() => ({
    sector: liveRotation(L?.rotation?.sector || d?.sectors || [], ticks, L?.asof),
    segment: liveRotation(L?.rotation?.segment || [], ticks, L?.asof),
  }), [L, d, ticks]);
  const moves = useMemo(() => sectorMoves(L?.rotation?.sector || [], ticks, L?.asof), [L, ticks]);
  const maxMove = Math.max(...moves.map((m) => Math.abs(m.chg)), 0.5);
  const signal = useMemo(() => {
    const m = {};
    for (const r of d?.ready || []) m[r.symbol] = "READY";
    for (const r of d?.breakouts || []) m[r.symbol] = r.signal;
    return m;
  }, [d]);

  const groups = useMemo(
    () => groupStats(stocks, (s) => [by === "sector" ? s.sector : s.segment]).sort((a, b) => (a.nowRank?.[win] ?? 99) - (b.nowRank?.[win] ?? 99)),
    [stocks, by, win],
  );
  const indexRows = useMemo(
    () => groupStats(stocks, (s) => (s.indices || "").split("|")).sort((a, b) => INDEX_ORDER.indexOf(a.name) - INDEX_ORDER.indexOf(b.name)),
    [stocks],
  );
  const maxRet = Math.max(...groups.map((g) => Math.abs(g.now[win] ?? 0)), 1);

  const sel = focus || (groups[0] && { by, name: groups[0].name });
  const selRows = sel?.by === "index" ? indexRows : sel?.by === by ? groups : groupStats(stocks, (s) => [sel?.by === "sector" ? s.sector : s.segment]);
  const inside = selRows.find((g) => g.name === sel?.name);
  const members = inside ? [...inside.members].sort((a, b) => (b.rs_rating ?? 0) - (a.rs_rating ?? 0)) : [];
  const rotation = liveGroups[mapBy] || [];

  const scores = useMemo(() => sectorScores(stocks), [stocks]);
  const [pickSector, setPickSector] = useState(null);
  const top = scores.find((x) => x.name === pickSector) || scores[0];
  const quad = Object.fromEntries((liveGroups.sector || []).map((r) => [r.sector, r.quadrant]));

  if (res.loading && !d) return <div className="chart-empty"><Spinner /></div>;
  if (d && !d.leadership) {
    return (
      <section className="card">
        <p className="muted">This scan predates the leadership data. The next daily scan (automatic after the market close) adds it.</p>
      </section>
    );
  }

  return (
    <div className="stack rotation">
      <p className="muted rotation-note">
        Equal-weight returns of the {stocks.length} scanned stocks, as of {d?.generated_at ? dateTime(d.generated_at) : "–"}. Rank 1 = strongest over
        that window; arrows show how the rank moved versus a week earlier. RS = 1–99 relative-strength rating across the universe.
      </p>
      <GroupCards stocks={stocks} sectors={d?.sectors} openStock={openStock || (() => {})} />

      <div className="rot-hero">
        <section className="card rot-map">
          <header className="card-head">
            <div>
              <h3>Rotation map {rotation.some((r) => r.live) && <span className="live-tag">LIVE</span>}</h3>
              <span className="muted">Relative-strength trend (x) and momentum (y) vs Nifty 50, weekly · groups orbit clockwise</span>
            </div>
            <Segmented size="small" label="Map grouping" value={mapBy} onChange={setMapBy} options={GROUPINGS} />
          </header>
          {rotation.length ? (
            <RotationMap
              sectors={rotation}
              tailLen={mapBy === "segment" ? 8 : 4}
              selected={focus?.by === mapBy ? focus.name : null}
              onSelect={(name) => name && (setFocus({ by: mapBy, name }), mapBy === "sector" && setPickSector(name))}
            />
          ) : (
            <div className="chart-empty">No rotation data in this scan.</div>
          )}
          <div className="quad-legend">
            {["Leading", "Weakening", "Lagging", "Improving"].map((q) => (
              <span key={q}><i className={`qdot ${QUADRANT_TONE[q]}`} /> {q} {rotation.filter((r) => r.quadrant === q).length}</span>
            ))}
          </div>
        </section>
        <section className="card rot-rank">
          <table className="table rot-table">
            <thead>
              <tr><th>Sector</th><th className="num">Score</th><th className="num">RS</th><th className="num">Breadth</th><th /></tr>
            </thead>
            <tbody>
              {scores.map((x) => (
                <tr key={x.name} className={`clickable${top?.name === x.name ? " sel" : ""}`} onClick={() => { setPickSector(x.name); setFocus({ by: "sector", name: x.name }); }}>
                  <td><i className={`qdot ${QUADRANT_TONE[quad[x.name]] || ""}`} /> <b>{shortSector(x.name)}</b></td>
                  <td className="num"><b className={x.score >= 65 ? "up" : x.score <= 40 ? "down" : ""}>{Math.round(x.score)}</b></td>
                  <td className="num">{Math.round(x.medRs ?? 0)}</td>
                  <td className="num">{Math.round(x.breadth)}%</td>
                  <td className="num muted">›</td>
                </tr>
              ))}
            </tbody>
          </table>
          <details className="rot-method">
            <summary>Score methodology</summary>
            <p className="muted small">RS 35% (median RS rating) · Breadth 20% (% above 50 DMA) · Trend 20% (3M return percentile) · Participation 15% (% up today) · Acceleration 10% (1M pace vs 3M).</p>
          </details>
          {top && (
            <div className="rot-pick">
              <b>{shortSector(top.name)}</b> <span className="muted">· {Math.round(top.breadth)}% above 50 DMA · A/D {top.up}:{top.n - top.up}{quad[top.name] ? ` · ${quad[top.name]}` : ""}</span>
              <button type="button" className="link-accent" onClick={() => document.getElementById("rot-inside")?.scrollIntoView({ behavior: "smooth" })}>Explore constituents →</button>
            </div>
          )}
        </section>
      </div>

      <section className="card">
        <header className="card-head">
          <div>
            <h3>Sector moves today {anyLive ? <span className="live-tag">LIVE</span> : <Badge>market closed</Badge>}</h3>
            <span className="muted">Equal-weight % change of each sector's stocks from live Kite ticks · bar colour = rotation quadrant · click to look inside</span>
          </div>
        </header>
        {moves.length ? (
          <div className="moves" style={{ gridTemplateRows: `repeat(${Math.ceil(moves.length / 3)}, auto)` }}>
            {moves.map((m) => (
              <button key={m.sector} type="button" className="move" onClick={() => setFocus({ by: "sector", name: m.sector })} title={`${m.up} up · ${m.down} down of ${m.live} trading`}>
                <span className="move-label"><i className={`qdot ${QUADRANT_TONE[m.quadrant]}`} />{shortSector(m.sector)}</span>
                <span className="move-track">
                  <span className={m.chg >= 0 ? "pos" : "neg"} style={{ width: `${(Math.abs(m.chg) / maxMove) * 50}%` }} />
                </span>
                <b className={tone(m.chg)}>{signed(m.chg, 2)}%</b>
                <small className="muted">{m.up}↑ {m.down}↓</small>
              </button>
            ))}
          </div>
        ) : (
          <div className="empty">No live ticks yet. During market hours (09:15–15:30) this fills in tick by tick.</div>
        )}
      </section>

      <div className="dash-grid">
        <section className="card span-7">
          <header className="card-head">
            <div>
              <h3>Group ranks</h3>
              <span className="muted">Ranked by {WINDOWS.find((w) => w.id === win).label} return · click a row to look inside</span>
            </div>
            <Segmented size="small" label="Grouping" value={by} onChange={(v) => { setBy(v); setFocus(null); }} options={GROUPINGS} />
          </header>
          <div className="row" style={{ marginBottom: 12 }}>
            <span className="eyebrow" style={{ margin: 0 }}>Return window</span>
            <Segmented size="small" label="Return window" value={win} onChange={setWin} options={WINDOWS} />
          </div>
          <div className="table-wrap flat">
            <table className="table ranks">
              <thead>
                <tr>
                  <th>{by === "sector" ? "Sector" : "Market cap"}</th>
                  <th className="num">Stocks</th>
                  {WINDOWS.map((w) => (
                    <th key={w.id} className={`num${w.id === win ? " hl" : ""}`}>{w.label}</th>
                  ))}
                  <th>{WINDOWS.find((w) => w.id === win).label} return</th>
                  <th className="num">Med RS</th>
                </tr>
              </thead>
              <tbody>
                {groups.map((g) => (
                  <tr key={g.name} className={`clickable${sel?.by === by && sel?.name === g.name ? " selected" : ""}`} onClick={() => setFocus({ by, name: g.name })}>
                    <td><b>{label(by, g.name)}</b></td>
                    <td className="num muted">{g.n}</td>
                    {WINDOWS.map((w) => <RankCell key={w.id} r={g} w={w.id} />)}
                    <td><ReturnBar v={g.now[win]} max={maxRet} /></td>
                    <td className="num"><RsPill v={g.medRs} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <section className="card span-5 inside-card" id="rot-inside">
          <header className="card-head">
            <div>
              <h3>Inside: {inside ? (sel.by === "sector" ? shortSector(inside.name) : label(sel.by, inside.name)) : "–"}</h3>
              {inside && (
                <span className="muted">
                  <span className={tone(inside.now["1d"])}>{signed(inside.now["1d"], 1)}%</span> today, {inside.up} of {inside.n} up · median RS {Math.round(inside.medRs ?? 0)}
                </span>
              )}
            </div>
          </header>
          <span className="eyebrow">Strongest members by RS</span>
          <div className="table-wrap flat inside-list">
            <table className="table">
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th className="num">1D</th>
                  <th className="num">1M</th>
                  <th className="num">3M</th>
                  <th className="num">RS</th>
                </tr>
              </thead>
              <tbody>
                {members.map((m) => (
                  <tr key={m.symbol} className="clickable" title="Open in Stock Intelligence"
                    onClick={() => (openStock ? openStock(m.symbol, m.exchange) : openTicket({ exchange: m.exchange, tradingsymbol: m.symbol, transaction_type: "BUY", product: "CNC", order_type: "LIMIT", price: m.last_close }))}>
                    <td>
                      <span className="sym"><b>{m.symbol}</b><small>{CAP_LABEL[m.segment] || m.segment} · {num(m.last_close)}</small></span>
                    </td>
                    <td className={`num ${tone(m.chg_1d)}`}>{signed(m.chg_1d, 1)}%</td>
                    <td className={`num ${tone(m.chg_1m)}`}>{signed(m.chg_1m, 1)}%</td>
                    <td className={`num ${tone(m.chg_3m)}`}>{signed(m.chg_3m, 1)}%</td>
                    <td className="num"><RsPill v={m.rs_rating} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </div>

      <div className="dash-grid">
        <section className="card span-12">
          <header className="card-head">
            <div>
              <h3>Indices</h3>
              <span className="muted">Each index as an equal-weight basket of its scanned stocks</span>
            </div>
          </header>
          <div className="table-wrap flat">
            <table className="table">
              <thead>
                <tr>
                  <th>Index</th>
                  <th className="num">1D</th>
                  <th className="num">1W</th>
                  <th className="num">1M</th>
                  <th className="num">3M</th>
                  <th className="num">YTD</th>
                  <th>Breadth today</th>
                  <th className="num">Med RS</th>
                </tr>
              </thead>
              <tbody>
                {indexRows.map((g) => (
                  <tr key={g.name} className={`clickable${sel?.by === "index" && sel?.name === g.name ? " selected" : ""}`} onClick={() => setFocus({ by: "index", name: g.name })}>
                    <td><b>{label("index", g.name)}</b> <small className="muted">{g.n}</small></td>
                    {["1d", "1w", "1m", "3m", "ytd"].map((w) => (
                      <td key={w} className={`num ${tone(g.now[w])}`}>{signed(g.now[w], 1)}%</td>
                    ))}
                    <td>
                      <span className={`breadth-pill ${g.up / g.n >= 0.5 ? "up" : "down"}`}>
                        <span style={{ width: `${(100 * g.up) / g.n}%` }} />
                        <b>{g.up}/{g.n} up</b>
                      </span>
                    </td>
                    <td className="num"><RsPill v={g.medRs} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

      </div>
      <ErrorNote error={res.error} />
    </div>
  );
}
