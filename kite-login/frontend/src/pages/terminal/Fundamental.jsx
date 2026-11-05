import { useState } from "react";
import { api } from "../../lib/api.js";
import { dateTime, inr, num, signed, tone } from "../../lib/format.js";
import { useTicks } from "../../lib/ticks.js";
import { ErrorNote, Segmented, Spinner, useLoad } from "../../components/ui.jsx";
import StockView from "../StockView.jsx";
import { SourceNote } from "./shared.jsx";

// A stock's full page: key numbers, the chart and technical read (StockView), then price and share statistics, the
// company profile, shareholding and quarterly results. Non-NSE instruments and indices show only the chart page.
const cr = (v) => (v == null ? "–" : v >= 100000 ? `₹${num(v / 100000, 2)} L Cr` : `₹${num(v, 0)} Cr`);

function Row({ label, value, tip }) {
  return <div className="fd-row" title={tip}><span>{label}</span><b>{value ?? "–"}</b></div>;
}

function History({ title, note, table, labels, unit = "" }) {
  if (!table?.periods?.length) return null;
  const take = 8;
  const periods = table.periods.slice(-take);
  const rows = labels.map((l) => [l, table.rows[l]]).filter(([, v]) => v);
  return (
    <section className="card tt-card">
      <header className="card-head"><h3>{title}</h3><span className="muted small">{note}</span></header>
      <div className="table-wrap">
        <table className="table fd-table">
          <thead><tr><th />{periods.map((p) => <th key={p} className="num">{p}</th>)}</tr></thead>
          <tbody>
            {rows.map(([l, v]) => (
              <tr key={l}><td>{l}</td>{v.slice(-take).map((x, i) => <td key={periods[i]} className="num">{x == null ? "–" : `${num(x, Math.abs(x) >= 100 ? 0 : 2)}${unit}`}</td>)}</tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

// The latest shareholding split, as bars.
function Ownership({ table }) {
  const rows = ["Promoters", "FIIs", "DIIs", "Public"].map((k) => [k, table?.rows?.[k]?.at(-1)]).filter(([, v]) => v != null);
  if (!rows.length) return null;
  return (
    <div className="fd-owner">
      <b>Who owns it <small className="muted">· {table.periods.at(-1)}</small></b>
      <ul>
        {rows.map(([k, v]) => <li key={k}><span className="muted">{k}</span><span><i style={{ width: `${Math.min(100, v)}%` }} /></span><em>{num(v)}%</em></li>)}
      </ul>
    </div>
  );
}

function Stats({ d, last }) {
  const [tab, setTab] = useState("price");
  const f = d.fundamentals;
  const s = d.live;
  const o = d.ohlc || {};
  return (
    <div className="fd-grid">
      <section className="card tt-card">
        <header className="card-head">
          <Segmented size="small" label="Statistics" value={tab} onChange={setTab} options={[{ id: "price", label: "Price highlights" }, { id: "share", label: "Share statistics" }]} />
          <SourceNote status={{ fetched_at: d.fetched_at, stale: d.stale, error: d.error }} source="Screener.in" />
        </header>
        {tab === "price" ? (
          <>
            <Row label="Open" value={o.open ? inr(o.open) : null} />
            <Row label="Today's range" value={o.low ? `${inr(o.low)} to ${inr(o.high)}` : null} />
            <Row label="Previous close" value={d.prev ? inr(d.prev) : null} />
            <Row label="52-week range" value={s ? `${inr(s.lo52)} to ${inr(s.hi52)}` : null} />
            <Row label="From 52-week high" value={s?.from_hi52 != null ? `${signed(s.from_hi52)}%` : null} />
            <Row label="Average volume (20 days)" value={s?.avg_vol ? num(s.avg_vol, 0) : null} />
            <Row label="Relative volume" value={s?.rvol != null ? num(s.rvol) : null} tip="Today's volume against the 20-day average, adjusted for the share of the session elapsed." />
            <Row label="Vs 20 / 50 / 200-DMA" value={s ? [s.vs_sma20, s.vs_sma50, s.vs_sma200].map((v) => (v == null ? "–" : `${signed(v, 1)}%`)).join(" · ") : null} />
            <Row label="1W / 1M / 3M / 1Y" value={s ? ["1W", "1M", "3M", "1Y"].map((k) => (s.ret[k] == null ? "–" : `${signed(s.ret[k], 1)}%`)).join(" · ") : null} />
          </>
        ) : (
          <>
            <Row label="Market cap" value={cr(f?.mcap_cr && f.price ? f.mcap_cr * (last / f.price) : f?.mcap_cr)} />
            <Row label="P/E" value={f?.pe != null ? num(f.pe) : null} />
            <Row label="Book value" value={f?.book_value != null ? inr(f.book_value) : null} />
            <Row label="Price to book" value={f?.book_value ? num(last / f.book_value) : null} />
            <Row label="Dividend yield" value={f?.div_yield != null ? `${num(f.div_yield)}%` : null} />
            <Row label="ROE" value={f?.roe != null ? `${num(f.roe)}%` : null} />
            <Row label="ROCE" value={f?.roce != null ? `${num(f.roce)}%` : null} />
            <Row label="Face value" value={f?.face_value != null ? inr(f.face_value) : null} />
            <Row label="Beta vs Nifty 50" value={s?.beta != null ? num(s.beta) : null} tip="Slope of daily returns against Nifty 50 over the last 250 sessions." />
          </>
        )}
      </section>
      <section className="card tt-card">
        <header className="card-head"><h3>Company profile</h3></header>
        <Row label="Sector" value={f?.sector || d.index_sector} />
        <Row label="Industry" value={f?.industry} />
        <Row label="Website" value={f?.website ? <a href={f.website} target="_blank" rel="noreferrer">{f.website.replace(/^https?:\/\/(www\.)?/, "").replace(/\/$/, "")}</a> : null} />
        {f?.about ? <p className="fd-about">{f.about}</p> : <p className="muted small">{d.error ? "Company details are unavailable right now." : "No company description published."}</p>}
        <Ownership table={f?.shareholding} />
      </section>
    </div>
  );
}

function Histories({ f }) {
  return (
    <>
      <History title="Quarterly results" note="₹ Crore, except per share" table={f?.quarters} labels={["Sales", "Expenses", "Operating Profit", "OPM %", "Profit before tax", "Net Profit", "EPS in Rs"]} />
      <History title="Shareholding" note="% of equity" table={f?.shareholding} labels={["Promoters", "FIIs", "DIIs", "Government", "Public"]} unit="%" />
    </>
  );
}

export default function Fundamental({ symbol, exchange = "NSE", ...view }) {
  const nse = exchange === "NSE" && !/^(NIFTY|SENSEX|INDIA VIX)/.test(symbol);
  const q = useLoad(() => (nse ? api.terminalFundamental(symbol) : Promise.resolve(null)), [symbol, nse], 60_000);
  const d = q.data;
  const { ticks } = useTicks(nse ? [`NSE:${symbol}`] : []);
  const last = ticks[`NSE:${symbol}`]?.last_price ?? d?.last;
  const f = d?.fundamentals;
  const s = d?.live;
  const kpis = d ? [
    ["Market cap", cr(f?.mcap_cr && f.price ? f.mcap_cr * (last / f.price) : f?.mcap_cr)],
    ["P/E", f?.pe != null ? num(f.pe) : "–"],
    ["ROE", f?.roe != null ? `${num(f.roe)}%` : "–"],
    ["Dividend yield", f?.div_yield != null ? `${num(f.div_yield)}%` : "–"],
    ["ATR", s?.atr_pct != null ? inr((s.atr_pct / 100) * s.last) : "–"],
    ["Beta", s?.beta != null ? num(s.beta) : "–"],
    ["RVOL", s?.rvol != null ? num(s.rvol) : "–"],
    ["Promoters", f?.shareholding?.rows?.Promoters ? `${num(f.shareholding.rows.Promoters.at(-1))}%` : "–"],
  ] : [];
  return (
    <div className="tt-page">
      {nse && (
        <section className="card fd-kpis" aria-label="Key numbers">
          {q.loading && !d ? <Spinner /> : kpis.map(([k, v]) => <span key={k}><small>{k}</small><b>{v}</b></span>)}
          {d?.last_trade_time && <em className="muted small">last trade {dateTime(d.last_trade_time, { dateStyle: "medium", timeStyle: "short" })}{d.prev && last ? <> · <span className={tone(last - d.prev)}>{signed((last / d.prev - 1) * 100)}% today</span></> : null}</em>}
        </section>
      )}
      <StockView symbol={symbol} exchange={exchange} {...view}>{nse && d ? <Stats d={d} last={last} /> : null}</StockView>
      {nse && d && <Histories f={d.fundamentals} />}
      {nse && !d && <ErrorNote error={q.error} />}
    </div>
  );
}
