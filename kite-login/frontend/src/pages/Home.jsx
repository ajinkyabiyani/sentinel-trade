import { api } from "../lib/api.js";
import { Sparkline } from "../components/charts.jsx";
import { num, REGIME_TONE, signed, tone } from "../lib/format.js";
import { NewsList } from "../components/news.jsx";
import { useTicks } from "../lib/ticks.js";
import { ErrorNote, Spinner, useLoad } from "../components/ui.jsx";
import { Logo } from "./Insight.jsx";
import { NormalizedChart, SourceNote } from "./terminal/shared.jsx";
import SectorPanel from "../components/SectorPanel.jsx";
import { WithLogo } from "../components/Logo.jsx";
import { marketStatus } from "../components/live.jsx";
import ConfidenceWins from "../components/ConfidenceWins.jsx";

// Home: the market's pulse in one card (status, trend, breadth, mood, view, VIX), one ribbon of index and commodity tiles on live
// ticks, then the main column (index performance, sectors, pre-open) beside a rail (mood, money flows, what is coming up with the
// IPO Agent's signals, news). The confidence wins table closes the page.
const HEADLINE = ["NSE:NIFTY 50", "BSE:SENSEX", "NSE:NIFTY BANK", "NSE:NIFTY MIDCAP 100"];
const SHORT = { SENSEX: "Sensex", "NIFTY 50": "Nifty 50", "NIFTY BANK": "Nifty Bank", "NIFTY MIDCAP 100": "Midcap 100", "NIFTY SMLCAP 100": "Smallcap 100" };
const TILE_MARK = { GOLD: { text: "Au", hue: 42 }, SILVER: { text: "Ag", hue: 210 }, USDINR: { text: "$", hue: 160 } };
const NSE = { id: "NSE", pre: 540, open: 555, close: 930 };
const istToday = () => new Date(Date.now() + (330 + new Date().getTimezoneOffset()) * 60_000).toISOString().slice(0, 10);
const shortDate = (d) => new Date(`${d}T00:00:00Z`).toLocaleDateString("en-IN", { timeZone: "UTC", weekday: "short", day: "numeric", month: "short" });
const daysTo = (d) => Math.round((Date.parse(`${d}T00:00:00Z`) - Date.parse(`${istToday()}T00:00:00Z`)) / 86_400_000);

function IndexTile({ m, live, onOpen }) {
  if (!m.name) return <div className="card hx-tile"><Spinner small /></div>;
  const t = live.ticks[m.key];
  const last = t?.last_price ?? m.last;
  const pc = t?.change_pct ?? m.change_pct;
  const series = (m.series || []).slice(-40).map((p) => p[1]);
  if (t?.last_price && series.length) series[series.length - 1] = t.last_price;
  const fx = m.exchange === "CDS";
  const mark = TILE_MARK[m.name];
  return (
    <button type="button" className={`card hx-tile ${tone(pc) || "flat"}`} onClick={() => onOpen(m.symbol || m.name, m.exchange)}
      title={m.contract ? `${m.contract} (front-month future, expires ${m.expiry}) · ${m.unit}` : undefined}>
      <span className="hx-tile-top">
        <Logo symbol={m.name} size={20} text={mark?.text} hue={mark?.hue} />
        <b>{m.label || SHORT[m.name] || m.name}</b>
      </span>
      <b key={last} className={`hx-tile-last ${t ? (t.change >= 0 ? "flash-up" : "flash-down") : ""}`}>{num(last, fx ? 4 : last >= 10000 ? 0 : 2)}</b>
      <span className="hx-tile-px">
        <span className={`hx-chg ${tone(pc)}`}>{signed(pc)}%</span>
        {m.regime && <em className={`hx-regime ${REGIME_TONE[m.regime.regime]}`} title={`${m.regime.regime}: ${m.regime.note}`}>{({ Uptrend: "▲ Up", Downtrend: "▼ Down", "Range-bound": "◆ Range" })[m.regime.regime] || m.regime.regime}</em>}
      </span>
      <span className="hx-tile-spark"><Sparkline values={series} height={30} /></span>
    </button>
  );
}

function Pulse({ indices, movers, live, breadthTicks, mv, mood, idx, go }) {
  const st = marketStatus(NSE);
  const n = mv?.indices?.find((x) => x.key === "NSE:NIFTY 50")?.regime;
  const uni = movers?.universe;
  const b = uni ? Object.entries(uni).reduce((a, [s, [prev, last]]) => {
    const px = breadthTicks[`NSE:${s}`]?.last_price ?? last;
    if (px > prev) a.up += 1; else if (px < prev) a.down += 1;
    return a;
  }, { up: 0, down: 0, total: Object.keys(uni).length }) : movers?.breadth;
  const nifty = indices?.find((m) => m.key === "NSE:NIFTY 50");
  const niftyPct = live.ticks["NSE:NIFTY 50"]?.change_pct ?? nifty?.change_pct;
  const vix = (idx?.snapshot || []).find((x) => x.label === "INDIA VIX");
  const vixPx = vix ? (live.ticks[vix.key]?.last_price ?? vix.last) : null;
  const vixChg = vix && vix.prev && vixPx ? (vixPx / vix.prev - 1) * 100 : null;
  const upShare = b && b.up + b.down ? (b.up / (b.up + b.down)) * 100 : 50;
  return (
    <section className="card hx-pulse" aria-label="Market pulse">
      <div className={`hx-status ${st.state}`}>
        <span className="hx-k">NSE</span>
        <b><i />{st.text}</b>
        <small>{st.sub}</small>
      </div>
      <div>
        <span className="hx-k">Market trend</span>
        <b className={REGIME_TONE[n?.regime] || ""}>{n?.regime || "–"}</b>
        <small>Nifty 50 {niftyPct != null ? <span className={tone(niftyPct)}>{signed(niftyPct)}%</span> : "–"} today</small>
      </div>
      <div className="hx-breadth" title={b ? `${b.up} advancing, ${b.down} declining of ${b.total} index stocks` : "needs a Kite session"}>
        <span className="hx-k">Breadth</span>
        <b><span className="up">{b ? b.up : "–"}</span><span className="hx-sep">/</span><span className="down">{b ? b.down : "–"}</span></b>
        <span className="hx-split"><span className="up" style={{ width: `${upShare}%` }} /><span className="down" style={{ width: `${100 - upShare}%` }} /></span>
      </div>
      <div>
        <span className="hx-k">Mood</span>
        <b>{mood?.score ?? "–"}<small className="hx-mood">{mood?.label || ""}</small></b>
        <span className="hx-mood-track"><span style={{ left: `${mood?.score ?? 50}%` }} /></span>
      </div>
      <button type="button" onClick={() => go("regime")} title={[mv?.summary, ...(mv?.reasons || [])].filter(Boolean).join("\n")}>
        <span className="hx-k">Market view</span>
        <b className={mv?.tone || ""}>{mv?.view || "–"}{mv?.score != null && <small> {signed(mv.score, 0)}</small>}</b>
        <small className="hx-ellipsis">{mv?.summary || "loading"}</small>
      </button>
      <div>
        <span className="hx-k">India VIX</span>
        <b>{vixPx == null ? "–" : num(vixPx, 2)}</b>
        <small>{vixChg == null ? "–" : <span className={tone(-vixChg)}>{signed(vixChg)}%</span>} today</small>
      </div>
    </section>
  );
}

function Flows({ flows }) {
  const rows = flows?.rows || [];
  const max = Math.max(1, ...rows.map((f) => Math.abs(Number(f.netValue) || 0)));
  return (
    <section className="card hx-card">
      <header className="hx-head"><h3>Money flows</h3><span className="muted small">{rows[0]?.date ? `cash market · ${rows[0].date}` : "cash market"}</span></header>
      {rows.length ? rows.map((f) => {
        const v = Number(f.netValue) || 0;
        return (
          <div key={f.category} className="hx-flow">
            <span>{f.category === "FII/FPI" ? "Foreign (FII)" : f.category === "DII" ? "Domestic (DII)" : f.category}</span>
            <span className="hx-flow-bar"><span className={v >= 0 ? "pos" : "neg"} style={{ width: `${(Math.abs(v) / max) * 50}%` }} /></span>
            <b className={tone(v)}>{v >= 0 ? "+" : "−"}₹{num(Math.abs(v), 0)} cr</b>
          </div>
        );
      }) : <p className="muted small">{flows ? "No figures yet." : <Spinner small />}</p>}
      <p className="hx-note">Net buying minus selling in the cash market, from NSE. Bought is right of centre.</p>
    </section>
  );
}

function MoodCard({ mood }) {
  if (!mood?.parts) return <section className="card hx-card"><header className="hx-head"><h3>Market mood</h3></header><Spinner small /></section>;
  return (
    <section className="card hx-card">
      <header className="hx-head"><h3>Market mood</h3><span className="muted small" title="This app's own measure, not a published index">0 to 100, calculated here</span></header>
      <div className="hx-mood-big"><b>{mood.score}</b><span>{mood.label}</span></div>
      <ul className="hx-parts">
        {mood.parts.map((p) => (
          <li key={p.name} title={p.note}>
            <span>{p.name}</span>
            <span className="hx-part-bar"><span style={{ width: `${p.score}%` }} className={p.score >= 60 ? "hi" : p.score <= 40 ? "lo" : ""} /></span>
            <b>{p.score}</b>
          </li>
        ))}
      </ul>
    </section>
  );
}

// A number and its unit, always: "1 day", "18 days"; today is the one word, set small enough to fit the tile.
function Countdown({ n }) {
  if (n <= 0) return <span className="hx-when"><b className="word">Today</b></span>;
  return <span className="hx-when"><b>{n}</b><small>{n === 1 ? "day" : "days"}</small></span>;
}

function ComingUp({ cal, ipo, go }) {
  const today = istToday();
  const eco = (cal?.economic || []).filter((e) => e.date >= today && e.group !== "Flows").sort((a, b) => a.date.localeCompare(b.date));
  const nextOf = (g) => eco.find((e) => e.group === g);
  const items = [nextOf("F&O expiry"), nextOf("RBI"), nextOf("Holiday")].filter(Boolean);
  const results = (cal?.earnings || []).filter((e) => e.date >= today);
  const resDay = results.map((e) => e.date).sort()[0];
  const ipos = (ipo?.active || []).slice(0, 4);
  return (
    <section className="card hx-card">
      <header className="hx-head"><h3>Coming up</h3><button type="button" className="link-accent" onClick={() => go("calendar")}>Calendar →</button></header>
      {!cal ? <Spinner small /> : (
        <ul className="hx-events">
          {items.map((e) => (
            <li key={e.date + e.event} className={e.group === "Holiday" ? "hol" : e.group === "RBI" ? "rbi" : "exp"}>
              <Countdown n={daysTo(e.date)} />
              <span><b>{e.group === "Holiday" ? "Market holiday" : e.event}</b><small>{shortDate(e.date)}{e.group === "Holiday" ? ` · ${e.event.replace(/^Market holiday:?\s*/i, "")}` : ""}</small></span>
            </li>
          ))}
          {resDay && (
            <li className="res">
              <span className="hx-when"><b>{results.filter((r) => r.date === resDay).length}</b><small>{results.filter((r) => r.date === resDay).length === 1 ? "result" : "results"}</small></span>
              <span><b>Results on {shortDate(resDay)}</b><small>{results.filter((r) => r.date === resDay).slice(0, 4).map((r) => r.symbol).join(", ")}</small></span>
            </li>
          )}
        </ul>
      )}
      <div className="hx-ipo-head"><span className="hx-k">IPO Agent</span><button type="button" className="link-accent" onClick={() => go("insiders")}>All IPOs →</button></div>
      {ipos.length ? (
        <ul className="hx-ipos">
          {ipos.map((a) => (
            <li key={a.id}>
              <button type="button" onClick={() => go("insiders")}>
                <span><b>{a.name}</b><small>{a.board} · {a.status_label}{a.open ? ` · opens ${shortDate(a.open)}` : ""}</small></span>
                <span className="hx-ipo-r">
                  <em className={`sig-${(a.signal || "").split(" ")[0].toLowerCase()}`}>{a.signal}</em>
                  {a.odds && <small>{num(a.odds.win_pct, 0)}% listed up</small>}
                </span>
              </button>
            </li>
          ))}
        </ul>
      ) : <p className="muted small">{ipo ? "No IPO in the pipeline right now." : "Reading IPOs…"}</p>}
    </section>
  );
}

// Today's biggest gainers and losers among the tracked index stocks, re-ranked on live ticks.
function TopMovers({ movers, ticks, openStock }) {
  const rows = [...(movers?.gainers || []), ...(movers?.losers || [])].map((r) => {
    const px = ticks[`NSE:${r.symbol}`]?.last_price ?? r.last;
    return { ...r, px, pc: r.prev_close ? (px / r.prev_close - 1) * 100 : r.change_pct };
  });
  const uniq = Object.values(Object.fromEntries(rows.map((r) => [r.symbol, r])));
  const up = uniq.filter((r) => r.pc > 0).sort((a, b) => b.pc - a.pc).slice(0, 5);
  const down = uniq.filter((r) => r.pc < 0).sort((a, b) => a.pc - b.pc).slice(0, 5);
  const max = Math.max(0.5, ...uniq.map((r) => Math.abs(r.pc)));
  const col = (list, title, cls) => (
    <div className="hx-mv-col">
      <span className="hx-k">{title}</span>
      {list.length ? list.map((r) => (
        <button key={r.symbol} type="button" className="hx-mv" onClick={() => openStock(r.symbol)} title={r.name || r.symbol}>
          <span className="hx-mv-id"><Logo symbol={r.symbol} size={22} /><span><b>{r.symbol}</b><small>{num(r.px)}</small></span></span>
          <span className="hx-mv-bar"><span className={cls} style={{ width: `${(Math.abs(r.pc) / max) * 100}%` }} /></span>
          <b className={tone(r.pc)}>{signed(r.pc)}%</b>
        </button>
      )) : <p className="muted small">None yet.</p>}
    </div>
  );
  return (
    <section className="card hx-card hx-movers">
      <header className="hx-head"><h3>Top movers</h3><span className="muted small">Nifty 50, Midcap 100 and Smallcap 100 stocks · live</span></header>
      {!movers ? <Spinner small /> : <div className="hx-mv-grid">{col(up, "Gainers", "up")}{col(down, "Losers", "down")}</div>}
    </section>
  );
}

function PreOpen({ data, openStock }) {
  return (
    <section className="card hx-card">
      <header className="hx-head"><h3>Biggest pre-open volume</h3><SourceNote status={data} source="NSE" /></header>
      <ul className="hx-rows">
        {(data?.rows || []).slice(0, 7).map((r) => (
          <li key={r.symbol}>
            <button type="button" onClick={() => openStock(r.symbol)}><WithLogo symbol={r.symbol} size={20}><b>{r.symbol}</b></WithLogo></button>
            <span>{num(r.price)}</span>
            <span className={`hx-chg ${tone(r.chg)}`}>{signed(r.chg)}%</span>
          </li>
        ))}
        {data && !data.rows.length && <li className="muted small">No pre-open data yet today.</li>}
      </ul>
      {!data && <Spinner small />}
    </section>
  );
}


export default function Home({ go, openStock, news }) {
  const market = useLoad(api.marketIndices, [], 120_000);
  const movers = useLoad(api.movers, [], 30_000);
  const mv = useLoad(api.regimeMarket, [], 60_000);
  const live = useTicks([...(market.data || []).map((m) => m.key), "NSE:NIFTY 50"]);
  const breadth = useTicks(Object.keys(movers.data?.universe || {}).map((s) => `NSE:${s}`));
  const idx = useLoad(api.terminalIndices, [], 30_000);
  const sectorTicks = useTicks([...(idx.data?.sectors || []).map((x) => x.key), ...(idx.data?.snapshot || []).map((x) => x.key)]);
  const mood = useLoad(api.terminalMood, [], 60_000);
  const preopen = useLoad(api.terminalPreopen, [], 120_000);
  const flows = useLoad(api.terminalFlows, [], 900_000);
  const cal = useLoad(api.terminalCalendar, [], 900_000);
  const ipo = useLoad(api.ipoAgent, [], 300_000);
  const headline = (market.data || []).filter((m) => HEADLINE.includes(m.key));
  const commodities = (market.data || []).filter((m) => m.kind === "commodity");
  const tiles = [...headline, ...commodities];
  const allTicks = { ticks: { ...live.ticks, ...sectorTicks.ticks } };
  return (
    <div className="hx tt-page fade-in">
      <Pulse indices={market.data} movers={movers.data} live={live} breadthTicks={breadth.ticks} mv={mv.data} mood={mood.data} idx={idx.data} go={go} />
      <div className="hx-ribbon">
        {(tiles.length ? tiles : Array.from({ length: 7 }, (_, i) => ({ key: i }))).map((m) => <IndexTile key={m.key} m={m} live={allTicks} onOpen={openStock} />)}
      </div>
      <ErrorNote error={market.error} />
      <div className="hx-main">
        <div className="hx-col">
          <SectorPanel sectors={idx.data?.sectors} ticks={sectorTicks.ticks} movers={movers.data} stockTicks={breadth.ticks} openStock={openStock} go={go} />
          <div className="hx-pair">
            <PreOpen data={preopen.data} openStock={openStock} />
            <TopMovers movers={movers.data} ticks={breadth.ticks} openStock={openStock} />
          </div>
          <section className="card hx-card hx-news">
            <header className="hx-head">
              <div className="row"><h3>Market news</h3><span className="live-chip"><span className="live-dot" /> Live</span></div>
              <button type="button" className="link-accent" onClick={() => go("news")}>All news →</button>
            </header>
            <div className="hx-news-body"><NewsList news={news} source="all" limit={14} compact /></div>
          </section>
        </div>
        <aside className="hx-rail">
          <ComingUp cal={cal.data} ipo={ipo.data} go={go} />
          <NormalizedChart compact />
          <MoodCard mood={mood.data} />
          <Flows flows={flows.data} />
        </aside>
      </div>
      <ConfidenceWins openStock={openStock} />
    </div>
  );
}
