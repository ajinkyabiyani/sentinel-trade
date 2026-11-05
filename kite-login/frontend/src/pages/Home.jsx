import { api } from "../lib/api.js";
import { QUADRANT_TONE, Sparkline } from "../components/charts.jsx";
import { inr, num, REGIME_TONE, rupee, shortSector, signed, tone } from "../lib/format.js";
import { NewsList } from "../components/news.jsx";
import { useTicks } from "../lib/ticks.js";
import { Badge, ErrorNote, Spinner, useLoad } from "../components/ui.jsx";
import { Logo } from "./Insight.jsx";
import { combinedCurve, liveDesks } from "./Paper.jsx";
import { HeatLegend, MoodGauge, NormalizedChart, SectorBars, SourceNote, Treemap, useStocks } from "./terminal/shared.jsx";

// Workspace: the day in four numbers, live index / commodity cards, the strategies' top calls beside market news, then
// the combined paper equity curve (₹1 lakh per strategy) beside sector strength. Prices, breadth and P&L move on ticks.
const HEADLINE = ["NSE:NIFTY 50", "BSE:SENSEX", "NSE:NIFTY BANK", "NSE:NIFTY MIDCAP 100"];
const SHORT = { SENSEX: "Sensex", "NIFTY 50": "Nifty 50", "NIFTY BANK": "Nifty Bank", "NIFTY MIDCAP 100": "Midcap 100", "NIFTY SMLCAP 100": "Smallcap 100" };
const TILE_MARK = { GOLD: { text: "Au", hue: 42 }, SILVER: { text: "Ag", hue: 210 }, USDINR: { text: "$", hue: 160 } };

function IndexTile({ m, live, onOpen }) {
  if (!m.name) return <div className="card idx-tile"><Spinner small /></div>;
  const t = live.ticks[m.key];
  const last = t?.last_price ?? m.last;
  const chg = t?.change ?? m.change;
  const pc = t?.change_pct ?? m.change_pct;
  const series = (m.series || []).slice(-40).map((p) => p[1]);
  if (t?.last_price && series.length) series[series.length - 1] = t.last_price;
  const fx = m.exchange === "CDS";
  const mark = TILE_MARK[m.name];
  return (
    <button type="button" className={`card idx-tile ${tone(chg) || "flat"}${m.kind === "commodity" ? " commodity" : ""}`} onClick={() => onOpen(m.symbol || m.name, m.exchange)}
      title={m.contract ? `${m.contract} (front-month future, expires ${m.expiry}) · ${m.unit}` : undefined}>
      <span className="idx-id">
        <Logo symbol={m.name} size={28} text={mark?.text} hue={mark?.hue} />
        <span>
          <small>{m.exchange} · {m.contract || m.name}</small>
          <b>{m.label || SHORT[m.name] || m.name}</b>
          {m.regime && <em className={`idx-regime ${REGIME_TONE[m.regime.regime]}`} title={`${m.regime.note} Score ${signed(m.regime.score, 0)}, ${m.regime.days} days in this regime.`}>{m.regime.regime}</em>}
        </span>
        {t && live.live && <span className="live-tag">LIVE</span>}
      </span>
      <span className="idx-bottom">
        <span>
          <b key={last} className={`idx-px ${t ? (t.change >= 0 ? "flash-up" : "flash-down") : ""}`}>{num(last, fx ? 4 : 2)}</b>
          <span className="idx-delta">
            <small className={tone(chg)}>{signed(chg, fx ? 4 : 2)}</small>
            <span className={`pill-chg ${tone(pc)}`}>{signed(pc)}%</span>
          </span>
        </span>
        <Sparkline values={series} height={46} />
      </span>
    </button>
  );
}

function StatStrip({ darvas, indices, movers, live, breadthTicks, mv, mvError, go }) {
  const n = darvas?.indices?.find((r) => r.index === "NIFTY 50");
  const trend = !n ? null : n.above_50dma && n.above_200dma ? ["Uptrend", "up", "above its 50 and 200-day averages"]
    : !n.above_50dma && !n.above_200dma ? ["Downtrend", "down", "below its 50 and 200-day averages"]
      : n.above_200dma ? ["Pullback", "amber", "above the 200-day, below the 50-day"] : ["Recovering", "amber", "above the 50-day, below the 200-day"];
  // breadth recounted on every tick of the index stocks: up / down against the previous close
  const uni = movers?.universe;
  const b = uni ? Object.entries(uni).reduce((a, [s, [prev, last]]) => {
    const px = breadthTicks[`NSE:${s}`]?.last_price ?? last;
    if (px > prev) a.up += 1; else if (px < prev) a.down += 1;
    return a;
  }, { up: 0, down: 0, total: Object.keys(uni).length }) : movers?.breadth;
  const secs = [...(darvas?.sectors || [])].sort((a, b2) => b2.rs_ratio - a.rs_ratio);
  // a sector is "leading" only in the Leading quadrant (RS ratio and RS momentum both above 100); when none is, the
  // card says so and names the strongest sector (highest RS ratio) and the fastest riser (best momentum, Improving)
  const leader = secs.find((s) => s.quadrant === "Leading");
  const lead = leader || secs[0];
  const riser = !leader ? [...secs].filter((s) => s.quadrant === "Improving").sort((a, b2) => b2.rs_momentum - a.rs_momentum)[0] : null;
  const nifty = indices?.find((m) => m.key === "NSE:NIFTY 50");
  const niftyPct = live.ticks["NSE:NIFTY 50"]?.change_pct ?? nifty?.change_pct;
  return (
    <section className="cc-stats">
      <div>
        <NavGlyph d="M3 17l6-6 4 4 8-8M15 7h6v6" />
        <span>
          <small>Market trend</small>
          <b className={trend?.[1] || ""}>{trend?.[0] || "–"}</b>
          <em className="muted">Nifty 50 {trend?.[2] || ""}{niftyPct != null ? ` · ${signed(niftyPct)}% today` : ""}</em>
        </span>
      </div>
      <div>
        <NavGlyph d="M17 20v-2a4 4 0 00-4-4H7a4 4 0 00-4 4v2M10 10a4 4 0 100-8 4 4 0 000 8zM21 20v-2a4 4 0 00-3-3.9M16 2.1a4 4 0 010 7.8" />
        <span>
          <small>Breadth</small>
          <b>{b ? <><span className="up">{b.up} advancing</span> <span className="muted">·</span> <span className="down">{b.down} declining</span></> : "–"}</b>
          <em className="muted">{b ? `of ${b.total} index stocks · A/D ${b.down ? num(b.up / b.down, 2) : "–"}` : "needs a Kite session"}</em>
        </span>
      </div>
      <div>
        <NavGlyph d="M5 20V10M10 20V4M15 20v-8M20 20v-5" />
        <span>
          <small>{!lead || leader ? "Leading sector" : "No sector leading · strongest"}</small>
          <b className={lead ? QUADRANT_TONE[lead.quadrant] : ""}>{lead ? shortSector(lead.sector) : "–"}</b>
          <em className="muted">
            {lead ? `${lead.quadrant} · RS ${num(lead.rs_ratio, 1)} · 1M ${signed(lead.chg_1m_pct)}%` : "after the daily scan"}
            {riser ? ` · rising: ${shortSector(riser.sector)}` : ""}
          </em>
        </span>
      </div>
      <button type="button" className="cc-kpi-btn" onClick={() => go("regime")} title={mv?.reasons?.join("\n")}>
        <NavGlyph d="M12 3a9 9 0 100 18 9 9 0 000-18zM15.5 8.5l-2 5-5 2 2-5z" />
        <span>
          <small>Market view</small>
          <b className={mv?.tone || ""}>{mv?.view || "–"}{mv?.score != null ? <small className="cc-score"> {signed(mv.score, 0)}</small> : null}</b>
          <em className="muted">{mv?.summary || (mvError ? "needs a Kite session" : "loading")}</em>
        </span>
      </button>
    </section>
  );
}

function NavGlyph({ d }) {
  return (
    <svg className="cc-glyph" viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={d} />
    </svg>
  );
}

// The strategies' top calls only: at most two each, best first (a live or finished trade, then an order about to
// trigger). ORB from its live engine, Darvas from the latest scan (fresh breakouts, then the nearest
// buy-ready boxes passing every filter).
const TOP = 2;
function topCalls(orb, darvas) {
  const out = [];
  const ot = [...(orb?.trades || [])].sort((a, b) => (a.status === "open" ? -1 : 0) - (b.status === "open" ? -1 : 0) || b.net - a.net);
  for (const t of ot.slice(0, TOP)) {
    out.push({ strat: "ORB", symbol: t.symbol, side: "LONG", to: "orb", pnl: t.net, open: t.status === "open",
      label: t.status === "open" ? "Open" : t.exit_reason || "Closed", tone: t.status === "open" ? "buy" : t.net >= 0 ? "buy" : "sell",
      note: `entry ${num(t.entry_ref)} · stop ${num(t.sl)}${t.target ? ` · target ${num(t.target)}` : ""}` });
  }
  for (const s of (orb?.signals || []).filter((x) => x.status === "pending")) {
    if (out.filter((c) => c.strat === "ORB").length >= TOP || out.some((c) => c.symbol === s.symbol)) continue;
    out.push({ strat: "ORB", symbol: s.symbol, side: "LONG", to: "orb", label: "Signal", tone: "buy", note: "breakout signal, enters on the next bar" });
  }
  const fresh = (darvas?.breakouts || []).filter((r) => r.signal === "BUY" && r.filters === "PASS" && r.bars_ago <= 1)
    .map((r) => ({ strat: "Darvas", symbol: r.symbol, side: "LONG", to: "darvas", label: "Breakout", tone: "buy",
      note: `entry ${num(r.entry || r.last_close)} · stop ${num(r.stop)}` }));
  const near = [...(darvas?.ready || [])].filter((r) => r.filters === "PASS").sort((a, b) => a.distance_pct - b.distance_pct)
    .map((r) => ({ strat: "Darvas", symbol: r.symbol, side: "LONG", to: "darvas", label: "Buy-ready", tone: "",
      note: `trigger ${num(r.trigger)} · ${num(r.distance_pct)}% away` }));
  out.push(...[...fresh, ...near].slice(0, TOP));
  return out;
}

function Signals({ orb, darvas, openStock, go }) {
  const calls = topCalls(orb, darvas);
  const { ticks } = useTicks(calls.map((c) => `NSE:${c.symbol}`));
  return (
    <section className="card ws-signals">
      <header className="card-head">
        <div>
          <h3>Top strategy calls</h3>
          <span className="muted small">the best {TOP} from each of ORB and Darvas · live prices</span>
        </div>
        <button type="button" className="link-accent" onClick={() => go("orb")}>All signals →</button>
      </header>
      <ul className="sig-list">
        {calls.map((c) => {
          const tk = ticks[`NSE:${c.symbol}`];
          return (
            <li key={c.strat + c.symbol}>
              <button type="button" onClick={() => openStock(c.symbol)}>
                <Logo symbol={c.symbol} size={28} />
                <span>
                  <b>{c.symbol} <small className="muted">· {c.strat}{c.side === "SHORT" ? " · short" : ""}</small></b>
                  <small className="muted">{c.note}{c.pnl != null ? <> · P&L <span className={tone(c.pnl)}>{rupee(c.pnl)}</span></> : ""}</small>
                </span>
                <span className="sig-right">
                  {tk && <b key={tk.last_price} className={`small ${tk.change >= 0 ? "flash-up" : "flash-down"}`}>{num(tk.last_price)} <span className={tone(tk.change_pct)}>{signed(tk.change_pct)}%</span></b>}
                  <Badge tone={c.tone}>{c.label}</Badge>
                </span>
              </button>
            </li>
          );
        })}
        {!calls.length && <li className="muted small">No strategy has a call right now.</li>}
      </ul>
    </section>
  );
}

// Paper equity of the three strategies together (₹1 lakh each), today's point marked live.
function EquityCurve({ pfData, pf, go }) {
  const curve = pfData ? combinedCurve(pf.desks) : [];
  const t0 = new Date(Date.now() + 330 * 60_000).toISOString().slice(0, 10);
  const pts = curve.length ? [...curve.filter((c) => c[0] === "start" || c[0] < t0), [t0, pf.equity]] : [];
  const chg = pf.equity - pf.capital;
  const short = (d) => (d === "start" ? "start" : new Date(d).toLocaleDateString("en-IN", { day: "numeric", month: "short" }));
  return (
    <section className="card ws-equity">
      <header className="card-head">
        <div>
          <h3>Equity curve</h3>
          <span className="muted small">ORB + Darvas paper desks · {inr(pf.capital, 0)} · after charges</span>
        </div>
        <button type="button" className="link-accent" onClick={() => go("paper")}>Portfolio →</button>
      </header>
      {!pfData ? <div className="chart-empty" style={{ height: 160 }}><Spinner /></div> : (
        <>
          <div className="row eq-head">
            <b className="eq-now">{inr(pf.equity, 0)}</b>
            <span className={`pill-chg ${tone(chg)}`}>{rupee(chg)} · {signed(pf.capital ? (chg / pf.capital) * 100 : 0)}%</span>
          </div>
          <div className="paper-curve">
            <Sparkline values={pts.map((c) => c[1])} height={150} />
            <div className="paper-curve-axis">{pts.map((c) => <span key={c[0]}>{short(c[0])}</span>)}</div>
          </div>
        </>
      )}
    </section>
  );
}

function PreOpen({ data, openStock }) {
  return (
    <section className="card tt-card">
      <header className="card-head"><h3>Biggest pre-open volume</h3><SourceNote status={data} source="NSE" /></header>
      <div className="tt-list-head"><span>Stock</span><span>Change</span><span>Price</span></div>
      <ul className="tt-list">
        {(data?.rows || []).map((r) => (
          <li key={r.symbol}>
            <button type="button" onClick={() => openStock(r.symbol)}><b>{r.symbol}</b><small>{r.name}</small></button>
            <span className={tone(r.chg)}>{signed(r.chg)}%</span>
            <span>{num(r.price)}</span>
          </li>
        ))}
        {data && !data.rows.length && <li className="muted small">No pre-open data yet today.</li>}
      </ul>
      {!data && <Spinner />}
    </section>
  );
}

function Snapshot({ market, live, indices, flows }) {
  const rows = [
    ...(indices?.snapshot || []).filter((x) => ["INDIA VIX", "Nifty Smallcap 100"].includes(x.label)).map((x) => ({ key: x.key, label: x.label, last: x.last, prev: x.prev })),
    ...(market || []).filter((m) => m.kind === "commodity" || m.exchange === "CDS").map((m) => ({ key: m.key, label: m.label || m.name, last: m.last, prev: m.last != null && m.change != null ? m.last - m.change : null, fx: m.exchange === "CDS" })),
  ];
  return (
    <section className="card tt-card">
      <header className="card-head"><h3>Market snapshot</h3></header>
      <div className="tt-list-head"><span>Instrument</span><span>Change</span><span>Value</span></div>
      <ul className="tt-list">
        {rows.map((r) => {
          const px = live.ticks[r.key]?.last_price ?? r.last;
          const chg = r.prev && px ? (px / r.prev - 1) * 100 : null;
          return <li key={r.key}><span>{r.label}</span><span className={tone(chg)}>{chg == null ? "–" : `${signed(chg)}%`}</span><b key={px}>{num(px, r.fx ? 4 : 2)}</b></li>;
        })}
        {(flows?.rows || []).map((f) => (
          <li key={f.category}><span>{f.category} net cash <small>{f.date}</small></span><span /><b className={tone(Number(f.netValue))}>₹{num(Number(f.netValue), 0)} Cr</b></li>
        ))}
      </ul>
    </section>
  );
}

function MarketCaps({ rows, openStock }) {
  // a ranking from part of the list would name the wrong leaders, so it waits for nearly all of them
  const known = rows.filter((r) => r.mcap_cr);
  const ready = rows.length > 0 && known.length >= rows.length * 0.9;
  const top = ready ? [...known].sort((a, b) => b.mcap_cr - a.mcap_cr).slice(0, 7) : [];
  return (
    <section className="card tt-card">
      <header className="card-head"><h3>Market cap leaders</h3><span className="muted small">₹ lakh crore</span></header>
      <div className="tt-list-head"><span>Stock</span><span>Mkt cap</span><span>Price</span></div>
      <ul className="tt-list">
        {top.map((r) => (
          <li key={r.symbol}>
            <button type="button" onClick={() => openStock(r.symbol)}><b>{r.symbol}</b><small>{r.name}</small></button>
            <span>{num(r.mcap_cr / 100000, 2)}</span>
            <span key={r.last} className={tone(r.chg)}>{num(r.last)}</span>
          </li>
        ))}
        {!ready && <li className="muted small">Ranking appears when market caps finish loading from Screener.in: {known.length} of {rows.length} so far.</li>}
      </ul>
    </section>
  );
}

function Upcoming({ cal, go }) {
  const today = new Date(Date.now() + (330 + new Date().getTimezoneOffset()) * 60_000).toISOString().slice(0, 10);
  const events = (cal?.economic || []).filter((e) => e.date >= today && e.group !== "Flows").sort((a, b) => a.date.localeCompare(b.date)).slice(0, 5);
  const upcoming = (cal?.earnings || []).filter((e) => e.date >= today);
  const nextDay = upcoming.map((e) => e.date).sort()[0];
  const results = upcoming.filter((e) => e.date === nextDay);
  const label = (d) => new Date(`${d}T00:00:00Z`).toLocaleDateString("en-IN", { timeZone: "UTC", day: "numeric", month: "short" });
  return (
    <section className="card tt-card">
      <header className="card-head"><h3>Upcoming</h3><button type="button" className="link-accent" onClick={() => go("calendar")}>Calendar →</button></header>
      {!cal ? <Spinner /> : (
        <ul className="tt-list">
          {nextDay && <li><span><b>{results.length}</b> companies with results board meetings <small>{results.slice(0, 4).map((r) => r.symbol).join(", ")}</small></span><span /><span>{label(nextDay)}</span></li>}
          {events.map((e) => <li key={e.date + e.event}><span>{e.event}</span><span /><span>{label(e.date)}</span></li>)}
        </ul>
      )}
    </section>
  );
}

export default function Home({ go, openStock, darvas, orb, news }) {
  const market = useLoad(api.marketIndices, [], 120_000);
  const movers = useLoad(api.movers, [], 30_000);
  const port = useLoad(api.portfolio, [], 20_000);
  const mv = useLoad(api.regimeMarket, [], 60_000);
  const live = useTicks([...(market.data || []).map((m) => m.key), "NSE:NIFTY 50"]);
  const breadth = useTicks(Object.keys(movers.data?.universe || {}).map((s) => `NSE:${s}`));
  const pfTicks = useTicks((port.data?.desks || []).flatMap((d) => (d.open || []).map((t) => `NSE:${t.symbol}`)));
  const pf = liveDesks(port.data, pfTicks.ticks);
  const stocks = useStocks();
  const idx = useLoad(api.terminalIndices, [], 30_000);
  const sectorTicks = useTicks([...(idx.data?.sectors || []).map((x) => x.key), ...(idx.data?.snapshot || []).map((x) => x.key)]);
  const mood = useLoad(api.terminalMood, [], 60_000);
  const preopen = useLoad(api.terminalPreopen, [], 120_000);
  const flows = useLoad(api.terminalFlows, [], 900_000);
  const cal = useLoad(api.terminalCalendar, [], 900_000);
  const headline = (market.data || []).filter((m) => HEADLINE.includes(m.key));
  const snapshotLive = { ticks: { ...live.ticks, ...sectorTicks.ticks } };
  // read top-down: the day in numbers, headline indices, today's market in tables, the heatmap, then strategies and news
  return (
    <div className="cc ws tt-page fade-in">
      <StatStrip darvas={darvas} indices={market.data} movers={movers.data} live={live} breadthTicks={breadth.ticks} mv={mv.data} mvError={mv.error} go={go} />
      <div className="tt-headline">
        {(headline.length ? headline : Array.from({ length: 4 }, (_, i) => ({ key: i }))).map((m) => <IndexTile key={m.key} m={m} live={live} onOpen={openStock} />)}
      </div>
      <ErrorNote error={market.error} />
      <div className="tt-grid3">
        <PreOpen data={preopen.data} openStock={openStock} />
        <MoodGauge mood={mood.data} />
        <SectorBars sectors={idx.data?.sectors} ticks={sectorTicks.ticks} />
        <Snapshot market={market.data} live={snapshotLive} indices={idx.data} flows={flows.data} />
        <MarketCaps rows={stocks.rows} openStock={openStock} />
        <NormalizedChart />
      </div>
      <div className="tt-grid2">
        <Upcoming cal={cal.data} go={go} />
        <section className="card tt-card">
          <header className="card-head">
            <h3>Heatmap</h3>
            <button type="button" className="link-accent" onClick={() => go("heatmap")}>Full heatmap →</button>
          </header>
          {stocks.loading ? <Spinner /> : <Treemap rows={stocks.rows} onPick={openStock} height={300} compact />}
          <footer className="tt-foot"><HeatLegend /></footer>
        </section>
      </div>
      <div className="ws-pair">
        <Signals orb={orb} darvas={darvas} openStock={openStock} go={go} />
        <section className="card ws-news">
          <header className="card-head">
            <div className="row">
              <h3>Market news</h3>
              <span className="live-chip"><span className="live-dot" /> Live</span>
            </div>
            <button type="button" className="link-accent" onClick={() => go("news")}>All news →</button>
          </header>
          <NewsList news={news} source="all" limit={8} compact />
        </section>
      </div>
      <EquityCurve pfData={port.data} pf={pf} go={go} />
    </div>
  );
}
