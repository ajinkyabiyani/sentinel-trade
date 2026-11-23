import { useMemo, useState } from "react";
import { api } from "../lib/api.js";
import { dateTime, num, signed, tone, REGIME_TONE } from "../lib/format.js";
import { ago } from "../components/news.jsx";
import { StockChart, sma, emaS, weekly } from "../components/stockchart.jsx";
import { useTicks } from "../lib/ticks.js";
import { DataTable, Empty, ErrorNote, Spinner, useLoad } from "../components/ui.jsx";
import { useWatchlist } from "../components/watchlist.jsx";
import { INDUSTRY_OF, THEMES_OF } from "../lib/classify.js";
import { Logo } from "./Insight.jsx";
import { RegimeCard } from "./Regime.jsx";
import { WithLogo } from "../components/Logo.jsx";

// Stock Intelligence: price / RVOL / RS-line chart; RS rating, momentum rank, Weinstein stage, Minervini trend
// template, returns and range; peers and headlines. Everything from Kite daily candles.
const RANGES = [{ id: "3M", bars: 63 }, { id: "6M", bars: 126 }, { id: "1Y", bars: 252 }, { id: "2Y", bars: 520 }, { id: "Max", bars: 9999 }];
const vol = (v) => (v == null ? "–" : v >= 1e7 ? `${(v / 1e7).toFixed(2)} Cr` : v >= 1e5 ? `${(v / 1e5).toFixed(2)} L` : v.toLocaleString("en-IN"));
function analyse(c, bench, last) {
  if (!c || c.length < 60) return null;
  const closes = c.map((x) => x[4]);
  const px = last ?? closes.at(-1);
  const s50 = sma(closes, 50);
  const s150 = sma(closes, 150);
  const s200 = sma(closes, 200);
  const at = (a, back = 0) => a[a.length - 1 - back];
  const yr = c.slice(-252);
  const hi52 = Math.max(...yr.map((x) => x[2]));
  const lo52 = Math.min(...yr.map((x) => x[3]));
  const ret = (n) => (closes.length > n ? (px / closes[closes.length - 1 - n] - 1) * 100 : null);
  const bc = (bench || []).map((x) => x[4]);
  const bret = (n) => (bc.length > n ? (bc.at(-1) / bc[bc.length - 1 - n] - 1) * 100 : null);
  const tt = [
    [at(s150) && at(s200) && px > at(s150) && px > at(s200), "Price > 150 & 200 DMA"],
    [at(s150) && at(s200) && at(s150) > at(s200), "150 DMA > 200 DMA"],
    [at(s200) && at(s200, 21) && at(s200) > at(s200, 21), "200 DMA rising"],
    [at(s50) && at(s150) && at(s200) && at(s50) > at(s150) && at(s50) > at(s200), "50 DMA > 150 & 200"],
    [at(s50) && px > at(s50), "Price > 50 DMA"],
    [px >= lo52 * 1.3, "≥ 30% above 52w low"],
    [px >= hi52 * 0.75, "Within 25% of 52w high"],
  ].map(([ok, label]) => [Boolean(ok), label]);
  const slope = at(s150) && at(s150, 20) ? at(s150) / at(s150, 20) - 1 : 0;
  const stage = !at(s150) ? ["–", ""] : px > at(s150) && slope > 0.005 ? ["2 · Advancing", "up"] : px < at(s150) && slope < -0.005 ? ["4 · Declining", "down"]
    : px >= at(s150) && px >= hi52 * 0.85 ? ["3 · Topping", "amber"] : ["1 · Basing", ""];
  const last20 = c.slice(-21, -1);
  const adr = last20.reduce((a, x) => a + (x[2] / x[3] - 1), 0) / Math.max(1, last20.length) * 100;
  const v50 = c.slice(-51, -1).reduce((a, x) => a + x[5], 0) / 50;
  const tv = last20.reduce((a, x) => a + x[4] * x[5], 0) / Math.max(1, last20.length) / 1e7;
  return {
    px, hi52, lo52, tt, score: tt.filter(([ok]) => ok).length, stage,
    vsN3m: ret(63) != null && bret(63) != null ? ret(63) - bret(63) : null,
    r1w: ret(5), r1m: ret(21), r3m: ret(63), r6m: ret(126), r1y: ret(250),
    from50: at(s50) ? (px / at(s50) - 1) * 100 : null, fromHi: (px / hi52 - 1) * 100,
    adr, rvol: v50 ? c.at(-1)[5] / v50 : null, tv, e21: at(emaS(closes, 21)),
  };
}

function AlertForm({ symbol, exchange, last, onDone }) {
  const [cond, setCond] = useState("above");
  const [value, setValue] = useState(last ? String(Math.round(last * 1.01 * 20) / 20) : "");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const save = async (e) => {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      await api.alertCreate({ symbol, exchange, cond, value: Number(value) });
      onDone(true);
    } catch (x) {
      setErr(x.message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="alert-pop fade-in" onSubmit={save}>
      <b>Alert on {symbol}</b>
      <select value={cond} onChange={(e) => setCond(e.target.value)} aria-label="Condition">
        <option value="above">Price crosses above</option>
        <option value="below">Price crosses below</option>
        <option value="chg_above">Day change above (%)</option>
        <option value="chg_below">Day change below (%)</option>
      </select>
      <input type="number" step="any" value={value} onChange={(e) => setValue(e.target.value)} aria-label="Value" required />
      <div className="row">
        <button type="button" className="ghost small" onClick={() => onDone(false)}>Cancel</button>
        <button type="submit" className="primary small" disabled={busy || !value}>Save alert</button>
      </div>
      <ErrorNote error={err} />
    </form>
  );
}

const Seg = ({ value, onChange, options }) => (
  <span className="sc-seg">
    {options.map((o) => <button key={o} type="button" className={value === o ? "on" : ""} onClick={() => onChange(o)}>{o}</button>)}
  </span>
);

export default function StockView({ children, symbol, exchange = "NSE", news, reloadAlerts, openStock, kpis }) {
  const [range, setRange] = useState("1Y");
  const [tf, setTf] = useState("D");
  const [kind, setKind] = useState("Candles");
  const [log, setLog] = useState(false);
  const [ind, setInd] = useState({ ema21: true, sma50: true, sma200: true });
  const [side, setSide] = useState("tech");
  const [alertOpen, setAlertOpen] = useState(false);
  const [note, setNote] = useState("");
  const wl = useWatchlist();
  const big = range === "Max";
  // analysis always uses two years of daily candles; "Max" only swaps the chart to five years (every 5th day)
  const q = useLoad(() => api.stock(symbol, exchange, "2Y"), [symbol, exchange]);
  const bq = useLoad(() => api.stock("NIFTY 50", "NSE", "2Y"), []);
  const qMax = useLoad(() => (big ? api.stock(symbol, exchange, "5Y") : Promise.resolve(null)), [symbol, exchange, big]);
  const bMax = useLoad(() => (big ? api.stock("NIFTY 50", "NSE", "5Y") : Promise.resolve(null)), [big]);
  const peers = useLoad(() => (exchange === "NSE" ? api.peers(symbol) : Promise.resolve(null)), [symbol, exchange]);
  const { ticks } = useTicks([`${exchange}:${symbol}`]);
  const t = ticks[`${exchange}:${symbol}`];
  const d = q.data?.symbol === symbol ? q.data : null;
  const last = t?.last_price ?? d?.last;
  const chg = t?.change ?? d?.change;
  const pc = t?.change_pct ?? d?.change_pct;
  // the daily series ends with today's candle moved by the live tick
  const series = useMemo(() => {
    if (!d?.candles?.length) return null;
    const c = d.candles.map((x) => [...x]);
    if (t?.last_price) {
      const lc = c.at(-1);
      lc[4] = t.last_price;
      lc[2] = Math.max(lc[2], t.last_price);
      lc[3] = Math.min(lc[3], t.last_price);
    }
    return c;
  }, [d, t]);
  const ta = useMemo(() => analyse(series, bq.data?.candles, last), [series, bq.data, last]);
  const shown = big ? qMax.data?.candles : tf === "W" ? weekly(series || []) : series;
  const bench = big ? bMax.data?.candles : tf === "W" ? weekly(bq.data?.candles || []) : bq.data?.candles;
  const rdef = RANGES.find((r) => r.id === range);
  const bars = tf === "W" || big ? Math.ceil(rdef.bars / 5) : rdef.bars;
  const words = [symbol, ...(d?.name || "").split(" ").filter((w) => w.length > 3).slice(0, 1)].map((w) => w.toLowerCase());
  const headlines = (news?.items || []).filter((n) => words.some((w) => n.title.toLowerCase().includes(w))).slice(0, 20);
  const industry = INDUSTRY_OF[symbol] || d?.industry;

  return (
    <div className="si2 fade-in">
      <header className="si2-head">
        <div className="si2-id">
          <Logo symbol={symbol} size={48} />
          <div>
            <div className="row">
              <h1>{d?.name?.replace(/ Limited$/, " Ltd.") || symbol}</h1>
              <span className="muted">{exchange}: {symbol}</span>
              <button type="button" className={`star${wl.has(symbol, exchange) ? " on" : ""}`} onClick={() => wl.toggle(symbol, exchange)} aria-pressed={wl.has(symbol, exchange)}
                title={wl.has(symbol, exchange) ? "Remove from watchlist" : "Add to watchlist"}>{wl.has(symbol, exchange) ? "★" : "☆"}</button>
            </div>
            <div className="si2-tags">
              {industry && <span className="tag">{industry}</span>}
              {(THEMES_OF[symbol] || []).slice(0, 3).map((x) => <span key={x} className="tag theme">{x}</span>)}
              {q.data?.regime && <span className={`tag ${REGIME_TONE[q.data.regime.regime]}`} title={q.data.regime.note}>{q.data.regime.regime}</span>}
              {q.data?.regime && <span className={`tag ${q.data.regime.tone}`}>{q.data.regime.view}</span>}
              {ta && <span className={`tag ${ta.stage[1]}`}>Stage {ta.stage[0]}</span>}
              {ta && <span className={`tag ${ta.score >= 7 ? "up" : ta.score <= 3 ? "down" : ""}`}>Trend {ta.score}/8</span>}
            </div>
          </div>
        </div>
        {kpis && (
          <dl className="si2-kpis" aria-label="Key numbers">
            {kpis === "loading" ? <Spinner small /> : kpis.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}
          </dl>
        )}
        <div className="si2-price">
          <div>
            <b key={last} className={t ? (t.change >= 0 ? "flash-up" : "flash-down") : ""}>{num(last)}</b>
            <span className={tone(pc)}>{signed(chg)} ({signed(pc)}%)</span>
          </div>
          <small className="muted">{ta ? `from high ${signed(ta.fromHi, 1)}% · ` : ""}{d?.last_trade_time ? `as of ${dateTime(d.last_trade_time, { dateStyle: "medium", timeStyle: "short" })}` : ""}</small>
          <div className="si-actions">
            <div className="alert-anchor">
              <button type="button" className="ghost" onClick={() => setAlertOpen((v) => !v)}>🔔 Alert</button>
              {alertOpen && <AlertForm symbol={symbol} exchange={exchange} last={last} onDone={(ok) => { setAlertOpen(false); if (ok) { setNote("Alert saved. Checked every 10 s while the backend runs; see News & Alerts."); reloadAlerts?.(); } }} />}
            </div>
          </div>
        </div>
      </header>
      {note && <p className="ok-note">{note}</p>}

      <div className={`si2-grid${children ? "" : " solo"}`}>
        <div className="si2-left">
          <section className="card si2-chart">
            <div className="sc-tools">
              <Seg value={tf} onChange={setTf} options={["D", "W"]} />
              <Seg value={range} onChange={setRange} options={RANGES.map((r) => r.id)} />
              <Seg value={kind} onChange={setKind} options={["Candles", "Bars", "Line"]} />
              <button type="button" className={`sc-tog${log ? " on" : ""}`} onClick={() => setLog((v) => !v)} aria-pressed={log}>Log</button>
              <span className="sc-inds">
                {[["ema21", "EMA 21", "e21"], ["sma50", "SMA 50", "s50"], ["sma200", "SMA 200", "s200"]].map(([k, l, c]) => (
                  <button key={k} type="button" className={`sc-tog ${c}${ind[k] ? " on" : ""}`} aria-pressed={ind[k]} onClick={() => setInd((v) => ({ ...v, [k]: !v[k] }))}>{l}</button>
                ))}
              </span>
            </div>
            {shown ? <StockChart candles={shown} bench={bench} bars={bars} kind={kind === "Candles" ? "candle" : kind === "Bars" ? "bar" : "line"} log={log} ind={ind} hi52={ta?.hi52} />
              : <div className="chart-empty" style={{ height: 540 }}>{q.loading || qMax.loading ? <Spinner /> : <ErrorNote error={q.error || qMax.error || "No data"} />}</div>}
            {ta && (
              <footer className="si2-strip">
                <span>3M <b className={tone(ta.r3m)}>{signed(ta.r3m, 1)}%</b></span>
                <span>6M <b className={tone(ta.r6m)}>{signed(ta.r6m, 1)}%</b></span>
                <span>1Y <b className={tone(ta.r1y)}>{signed(ta.r1y, 1)}%</b></span>
                <span>RS <b>{ta.rs ?? "–"}</b></span>
                <span>RS 3M <b>{ta.rs3m ?? "–"}</b></span>
                <span>Stage <b className={ta.stage[1]}>{ta.stage[0]}</b></span>
                <span>Trend <b>{ta.score}/8</b></span>
                <span className="muted">Avg traded value {num(ta.tv, 0)} cr/day</span>
              </footer>
            )}
          </section>
          {children}
        </div>

        <aside className="card si2-side">
          <div className="cc-tabs" role="tablist">
            {[["tech", "Technical"], ["peers", "Peers"], ["news", `News${headlines.length ? ` (${headlines.length})` : ""}`]].map(([id, l]) => (
              <button key={id} type="button" role="tab" aria-selected={side === id} className={side === id ? "on" : ""} onClick={() => setSide(id)}>{l}</button>
            ))}
          </div>
          {side === "tech" && (!ta ? <div className="chart-empty">{q.loading ? <Spinner /> : "Not enough history"}</div> : (
            <div className="tp">
              <RegimeCard r={q.data?.regime} title="Market regime" />
              <div className="tp-tiles">
                <div><small>Stage</small><b className={ta.stage[1]}>{ta.stage[0]}</b><em>Weinstein, 150-day trend</em></div>
                <div><small>vs Nifty 50 · 3M</small><b className={tone(ta.vsN3m)}>{ta.vsN3m == null ? "–" : `${signed(ta.vsN3m, 1)}%`}</b><em>return difference</em></div>
              </div>
              <h4>Trend template <small className="muted">({ta.score}/{ta.tt.length})</small></h4>
              <ul className="tp-tt">
                {ta.tt.map(([ok, l]) => <li key={l} className={ok ? "ok" : "no"}><i>{ok ? "✓" : "✗"}</i>{l}</li>)}
              </ul>
              <h4>Returns</h4>
              <div className="tp-row3">
                <span><small>1 day</small><b className={tone(pc)}>{signed(pc)}%</b></span>
                <span><small>1 week</small><b className={tone(ta.r1w)}>{signed(ta.r1w, 1)}%</b></span>
                <span><small>From 50-day avg</small><b className={tone(ta.from50)}>{signed(ta.from50, 1)}%</b></span>
                <span><small>From 52w high</small><b className={tone(ta.fromHi)}>{signed(ta.fromHi, 1)}%</b></span>
              </div>
              <h4>Range & volume</h4>
              <div className="tp-row3">
                <span><small>ADR %</small><b>{num(ta.adr, 1)}%</b></span>
                <span><small>Rel. volume</small><b className={ta.rvol >= 1.5 ? "up" : ""}>{ta.rvol ? `${num(ta.rvol, 1)}×` : "–"}</b></span>
                <span><small>Volume today</small><b>{vol(d?.volume || null)}</b></span>
              </div>
              <button type="button" className="ghost block tp-news" onClick={() => setSide("news")}>📰 Company news</button>
            </div>
          ))}
          {side === "peers" && (peers.loading && !peers.data ? <div className="chart-empty"><Spinner /></div> : peers.data?.peers?.length ? (
            <DataTable rowKey={(r) => r.symbol} rows={peers.data.peers} rowClass={(r) => (r.symbol === symbol ? "sel" : "")}
              columns={[
                { key: "symbol", label: "Company", render: (r) => <button type="button" className="linkish strong" onClick={() => openStock?.(r.symbol)}><WithLogo symbol={r.symbol} size={20}>{r.symbol}</WithLogo></button> },
                { key: "last", label: "Price", align: "num", render: (r) => num(r.last) },
                { key: "change_pct", label: "Today", align: "num", render: (r) => <span className={tone(r.change_pct)}>{signed(r.change_pct)}%</span> },
              ]} />
          ) : <Empty>No same-industry peers in the tracked indices.</Empty>)}
          {side === "news" && (headlines.length ? (
            <ul className="news-list">
              {headlines.map((n) => (
                <li key={n.id}>
                  <a href={n.link} target="_blank" rel="noreferrer noopener"><b>{n.title}</b></a>
                  <small className="muted">{n.publisher || n.source_label} · {ago(n.published)}</small>
                </li>
              ))}
            </ul>
          ) : <Empty>No headline mentions {symbol} in the latest feeds.</Empty>)}
        </aside>
      </div>
    </div>
  );
}
