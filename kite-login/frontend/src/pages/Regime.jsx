import { useMemo, useState } from "react";
import { api } from "../lib/api.js";
import { num, REGIME_TONE, signed, tone } from "../lib/format.js";
import { Badge, DataTable, Empty, ErrorNote, Spinner, useLoad } from "../components/ui.jsx";
import { Logo } from "./Insight.jsx";

// Market Regime: the overall market view, each core index, and every liquid stock read against its own history
// (backend regime.py). Directional momentum, range-bound, squeeze or choppy, with the readings behind each call.
const REGIMES = ["Uptrend", "Downtrend", "Range-bound", "Squeeze", "Choppy"];
const REGIME_INFO = {
  Uptrend: "ADX above 25, averages fanned upward, RSI above 60, higher highs and lows",
  Downtrend: "ADX above 25, averages fanned downward, RSI below 40, lower highs and lows",
  "Range-bound": "ADX below 20, flat averages, RSI 30–70, normal volatility",
  Squeeze: "Bollinger width and ATR low for this instrument, ATR declining, inside bars",
  Choppy: "ATR high for this instrument, flat averages, repeated 20-day crossings, long wicks",
};
const SHORT = { "NIFTY 50": "Nifty 50", "NIFTY BANK": "Nifty Bank", "NIFTY MIDCAP 100": "Midcap 100", "NIFTY SMLCAP 100": "Smallcap 100", SENSEX: "Sensex" };
const when = (iso) => (iso ? new Date(iso).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" }) : "–");
const day = (iso) => (iso ? new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short" }) : "–");

function ScoreBar({ score }) {
  const s = score ?? 0;
  return (
    <span className="rg-score" title={`Direction score ${signed(s, 0)} (−100 bearish to +100 bullish)`}>
      <i className={tone(s)} style={{ left: s >= 0 ? "50%" : `${50 + s / 2}%`, width: `${Math.abs(s) / 2}%` }} />
    </span>
  );
}

// One instrument's regime with the readings behind it (index cards here, the Stock Intelligence panel)
export function RegimeCard({ r, title, onOpen }) {
  if (!r) return null;
  const head = (
    <>
      <div className="rg-head">
        <div>
          {title && <small className="muted">{title}</small>}
          <b className={REGIME_TONE[r.regime]}>{r.regime}</b>
        </div>
        <Badge tone={r.tone === "up" ? "buy" : r.tone === "down" ? "sell" : "amber"}>{r.view}</Badge>
      </div>
      <ScoreBar score={r.score} />
      <small className="muted">
        score {signed(r.score, 0)} · {r.days} day{r.days === 1 ? "" : "s"} in this regime · ADX {num(r.adx, 0)} · RSI {num(r.rsi, 0)}
        {r.forming ? <> · <span className={REGIME_TONE[r.forming]}>turning {r.forming}</span></> : null}
      </small>
    </>
  );
  return (
    <section className="card rg-card">
      {onOpen ? <button type="button" className="rg-open" onClick={onOpen}>{head}</button> : head}
      {r.note && <p className="small">{r.note}</p>}
      {r.signs && (
        <ul className="checks">
          {r.signs.map(([ok, text]) => <li key={text} className={ok ? "ok" : "no"}><i>{ok ? "✓" : "✗"}</i>{text}</li>)}
        </ul>
      )}
    </section>
  );
}

export default function Regime({ openStock }) {
  const mq = useLoad(api.regimeMarket, [], 60_000);
  const sq = useLoad(api.regimeStocks, [], 60_000);
  const [only, setOnly] = useState(null);
  const [view, setView] = useState("liquid");
  const [find, setFind] = useState("");
  const mv = mq.data;
  const all = sq.data?.stocks;
  const stocks = useMemo(() => all || [], [all]);
  const counts = useMemo(() => {
    const c = {};
    for (const s of stocks) if (view === "all" || s.liquid) c[s.regime] = (c[s.regime] || 0) + 1;
    return c;
  }, [stocks, view]);
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const rows = useMemo(() => stocks.filter((s) => (view === "all" || s.liquid) && (!only || s.regime === only)
    && (!find || s.symbol.includes(find.toUpperCase()) || (s.name || "").toUpperCase().includes(find.toUpperCase()))), [stocks, view, only, find]);
  const prog = sq.data?.progress;
  const hasIlliquid = stocks.some((s) => !s.liquid);

  return (
    <div className="rg fade-in">
      <section className="card rg-hero">
        {!mv ? (mq.error ? <ErrorNote error={mq.error} /> : <Spinner />) : (
          <>
            <div className="rg-hero-main">
              <small className="muted">Overall market view</small>
              <b className={mv.tone}>{mv.view}</b>
              <ScoreBar score={mv.score} />
              <small className="muted">score {mv.score == null ? "–" : signed(mv.score, 0)} of ±100 · updated {when(mv.asof)}</small>
            </div>
            <ul className="rg-reasons">{mv.reasons.map((x) => <li key={x}>{x}</li>)}</ul>
          </>
        )}
      </section>

      <div className="rg-grid">
        {(mv?.indices || []).map((m) => (
          <RegimeCard key={m.key} r={m.regime} title={m.label || SHORT[m.name] || m.name} onOpen={() => openStock(m.symbol || m.name, m.exchange)} />
        ))}
      </div>

      <section className="card">
        <header className="card-head">
          <div>
            <h3>Stocks by regime</h3>
            <span className="muted small">
              {sq.data?.built ? `Updated ${when(sq.data.built)}` : "Not built yet"}
              {prog?.running ? ` · refreshing ${prog.done}/${prog.total}` : ""}
              {sq.data?.partial ? " · liquid stocks refreshed hourly in market hours, every NSE stock after the close" : ""}
            </span>
          </div>
          {hasIlliquid && (
            <label className="field-inline small">
              <input type="checkbox" checked={view === "all"} onChange={(e) => setView(e.target.checked ? "all" : "liquid")} /> include illiquid
            </label>
          )}
        </header>
        <div className="rg-dist">
          {REGIMES.map((k) => (
            <button key={k} type="button" className={`rg-pick${only === k ? " on" : ""}`} title={REGIME_INFO[k]} onClick={() => setOnly(only === k ? null : k)}>
              <small>{k}</small>
              <b className={REGIME_TONE[k]}>{counts[k] || 0}</b>
              <span className="rg-share"><i className={REGIME_TONE[k]} style={{ width: `${total ? ((counts[k] || 0) / total) * 100 : 0}%` }} /></span>
            </button>
          ))}
        </div>
        <input className="rg-find" type="search" placeholder="Find a stock…" value={find} onChange={(e) => setFind(e.target.value)} aria-label="Find a stock" />
        {!stocks.length ? <Empty>{prog?.running ? `Reading daily candles ${prog.done}/${prog.total}…` : "Stock regimes appear a few minutes after login."}</Empty> : (
          <DataTable rowKey={(s) => s.symbol} rows={rows} limit={100} initialSort={{ key: "score", dir: "desc" }}
            columns={[
              { key: "symbol", label: "Stock", render: (s) => (
                <button type="button" className="rg-sym" onClick={() => openStock(s.symbol)}><Logo symbol={s.symbol} size={24} /><b>{s.symbol}</b></button>) },
              { key: "regime", label: "Regime", render: (s) => (
                <span><b className={REGIME_TONE[s.regime]}>{s.regime}</b>{s.forming ? <small className="muted"> → {s.forming}</small> : null}</span>) },
              { key: "view", label: "View", render: (s) => <Badge tone={s.tone === "up" ? "buy" : s.tone === "down" ? "sell" : "amber"}>{s.view}</Badge> },
              { key: "score", label: "Score", align: "num", render: (s) => <b className={tone(s.score)}>{signed(s.score, 0)}</b> },
              { key: "fit", label: "Readings met", align: "num", render: (s) => `${s.fit}%` },
              { key: "days", label: "Days", align: "num", render: (s) => <span title={`since ${day(s.since)}`}>{s.days}</span> },
              { key: "adx", label: "ADX", align: "num", render: (s) => num(s.adx, 0) },
              { key: "rsi", label: "RSI", align: "num", render: (s) => num(s.rsi, 0) },
              { key: "vol_pct", label: "ATR rank", align: "num", render: (s) => num(s.vol_pct, 0) },
              { key: "close", label: "Close", align: "num", render: (s) => num(s.close) },
            ]} />
        )}
        <ErrorNote error={sq.error || prog?.error} />
      </section>
      <p className="muted small rg-foot">
        Describes current conditions from price and volatility; it is not a forecast. High and low volatility are ranked against each instrument's own last year; a new regime needs two closes to take over.
      </p>
    </div>
  );
}
