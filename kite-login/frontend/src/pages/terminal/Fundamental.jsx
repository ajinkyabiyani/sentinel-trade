import { useMemo, useState } from "react";
import { api } from "../../lib/api.js";
import { inr, num, signed, tone } from "../../lib/format.js";
import { useTicks } from "../../lib/ticks.js";
import { ErrorNote, Segmented, useLoad } from "../../components/ui.jsx";
import StockView from "../StockView.jsx";
import { SourceNote } from "./shared.jsx";

// A stock's full page: the header, chart and technical read (StockView), then the fundamentals as a dashboard:
// at a glance (price ranges, returns, valuation and quality), quarterly results as a chart with growth, and who owns it beside
// the company profile. Non-NSE instruments and indices show only the chart page.
const cr = (v) => (v == null ? "–" : v >= 100000 ? `₹${num(v / 100000, 2)} L Cr` : `₹${num(v, 0)} Cr`);
const short = (p) => String(p).replace(/^(\w{3}) \d{2}(\d{2})$/, "$1 '$2");
const METRICS = [
  { id: "Sales", label: "Revenue", unit: "cr" },
  { id: "Operating Profit", label: "Operating profit", unit: "cr" },
  { id: "Net Profit", label: "Net profit", unit: "cr" },
  { id: "EPS in Rs", label: "EPS", unit: "₹" },
  { id: "OPM %", label: "Margin %", unit: "%" },
];
const HOLDERS = [["Promoters", "var(--accent)"], ["FIIs", "var(--blue)"], ["DIIs", "var(--violet)"], ["Government", "var(--teal)"], ["Public", "var(--muted)"]];
const fmtMetric = (v, unit) => (v == null ? "–" : unit === "%" ? `${num(v, 1)}%` : unit === "₹" ? `₹${num(v, 2)}` : `₹${num(v, 0)} cr`);

// ---------------------------------------------------------------- at a glance

function RangeBar({ label, lo, hi, at, sub }) {
  if (lo == null || hi == null || at == null || hi <= lo) return null;
  const pos = Math.max(0, Math.min(100, ((at - lo) / (hi - lo)) * 100));
  return (
    <div className="fx-range">
      <div className="fx-range-top"><span>{label}</span>{sub && <small>{sub}</small>}</div>
      <div className="fx-range-bar"><span className="fx-range-fill" style={{ width: `${pos}%` }} /><i style={{ left: `${pos}%` }} /></div>
      <div className="fx-range-ends"><span>{inr(lo)}</span><b>{inr(at)}</b><span>{inr(hi)}</span></div>
    </div>
  );
}

function PriceCard({ d, last }) {
  const s = d.live || {};
  const o = d.ohlc || {};
  const mas = [["20-day", s.vs_sma20], ["50-day", s.vs_sma50], ["200-day", s.vs_sma200]];
  return (
    <section className="card fx-card">
      <header className="fx-head"><h3>Price position</h3>{s.rsi != null && <span className={`fx-pill ${s.rsi >= 70 ? "warn" : s.rsi <= 30 ? "good" : ""}`}>RSI {num(s.rsi, 0)}</span>}</header>
      <RangeBar label="Today" lo={o.low} hi={o.high} at={last} sub={o.open ? `opened ${inr(o.open)}` : null} />
      <RangeBar label="52 weeks" lo={s.lo52} hi={s.hi52} at={last} sub={s.from_hi52 != null ? `${signed(s.from_hi52, 1)}% from high` : null} />
      <div className="fx-mas">
        {mas.map(([k, v]) => (
          <div key={k} className={v == null ? "" : v >= 0 ? "above" : "below"}>
            <small>vs {k} avg</small>
            <b className={tone(v)}>{v == null ? "–" : `${signed(v, 1)}%`}</b>
          </div>
        ))}
      </div>
    </section>
  );
}

function ReturnsCard({ s }) {
  const keys = ["1W", "1M", "3M", "6M", "YTD", "1Y"];
  const max = Math.max(1, ...keys.map((k) => Math.abs(s?.ret?.[k] ?? 0)));
  return (
    <section className="card fx-card">
      <header className="fx-head"><h3>Returns</h3>{s?.beta != null && <span className="fx-pill" title="Slope of daily returns against Nifty 50 over 250 sessions">Beta {num(s.beta, 2)}</span>}</header>
      <div className="fx-heat">
        {keys.map((k) => {
          const v = s?.ret?.[k];
          return (
            <div key={k} className={v == null ? "" : v >= 0 ? "pos" : "neg"} style={{ "--a": `${Math.round(10 + (Math.min(1, Math.abs(v ?? 0) / max)) * 40)}%` }}>
              <small>{k}</small><b>{v == null ? "–" : `${signed(v, 1)}%`}</b>
            </div>
          );
        })}
      </div>
      <div className="fx-mini">
        <div><small>Avg volume (20d)</small><b>{s?.avg_vol ? num(s.avg_vol, 0) : "–"}</b></div>
        <div><small>Relative volume</small><b>{s?.rvol != null ? `${num(s.rvol, 2)}×` : "–"}</b></div>
        <div><small>Daily range (ATR)</small><b>{s?.atr_pct != null ? `${num(s.atr_pct, 1)}%` : "–"}</b></div>
      </div>
    </section>
  );
}

function rate(v, good, fair, higherBetter = true) {
  if (v == null) return "";
  return higherBetter ? (v >= good ? "good" : v >= fair ? "fair" : "weak") : (v <= good ? "good" : v <= fair ? "fair" : "weak");
}

function ValueCard({ f, last }) {
  const pb = f?.book_value ? last / f.book_value : null;
  const tiles = [
    { k: "Market cap", v: cr(f?.mcap_cr && f.price ? f.mcap_cr * (last / f.price) : f?.mcap_cr), r: "" },
    { k: "P/E", v: f?.pe != null ? num(f.pe, 1) : "–", r: rate(f?.pe, 20, 40, false), why: "price per rupee of yearly profit" },
    { k: "Price to book", v: pb != null ? num(pb, 2) : "–", r: rate(pb, 3, 6, false), why: "price against net assets per share" },
    { k: "ROE", v: f?.roe != null ? `${num(f.roe, 1)}%` : "–", r: rate(f?.roe, 15, 10), why: "profit on shareholders' money" },
    { k: "ROCE", v: f?.roce != null ? `${num(f.roce, 1)}%` : "–", r: rate(f?.roce, 15, 10), why: "profit on all capital employed" },
    { k: "Dividend yield", v: f?.div_yield != null ? `${num(f.div_yield, 2)}%` : "–", r: rate(f?.div_yield, 2, 0.5), why: "yearly dividend against price" },
  ];
  const label = { good: "Strong", fair: "Fair", weak: "Weak" };
  return (
    <section className="card fx-card">
      <header className="fx-head"><h3>Valuation & quality</h3><span className="muted small">book value {f?.book_value != null ? inr(f.book_value) : "–"} · face value {f?.face_value != null ? inr(f.face_value) : "–"}</span></header>
      <div className="fx-tiles">
        {tiles.map((t) => (
          <div key={t.k} className={`fx-tile ${t.r}`} title={t.why}>
            <small>{t.k}</small>
            <b>{t.v}</b>
            {t.r && <em>{label[t.r]}</em>}
          </div>
        ))}
      </div>
      <p className="fx-note">Strong / fair / weak use simple fixed cut-offs (P/E 20 and 40, ROE and ROCE 15% and 10%), not a sector comparison.</p>
    </section>
  );
}

// ---------------------------------------------------------------- quarterly results

function Quarters({ table }) {
  const [metric, setMetric] = useState("Sales");
  const [showTable, setShowTable] = useState(false);
  const [hover, setHover] = useState(null);
  const m = METRICS.find((x) => x.id === metric);
  const all = table?.periods || [];
  const vals = table?.rows?.[metric] || [];
  const take = Math.min(12, all.length);
  const periods = all.slice(-take);
  const series = vals.slice(-take);
  const offset = all.length - take;
  const yoy = (i) => { const a = vals[offset + i], b = vals[offset + i - 4]; return a == null || b == null || b === 0 || metric === "OPM %" ? null : ((a - b) / Math.abs(b)) * 100; };
  const ppt = (i) => { const a = vals[offset + i], b = vals[offset + i - 4]; return a == null || b == null ? null : a - b; };
  const max = Math.max(1, ...series.map((v) => Math.abs(v ?? 0)));
  const W = 760, H = 220, P = { l: 8, r: 8, t: 26, b: 26 };
  const bw = (W - P.l - P.r) / Math.max(1, series.length);
  const y0 = H - P.b;
  const scale = (v) => ((Math.abs(v) / max) * (H - P.t - P.b));
  const li = series.length - 1;
  const focus = hover ?? li;
  const latestYoY = metric === "OPM %" ? ppt(focus) : yoy(focus);
  if (!periods.length) return null;
  const rowsForTable = ["Sales", "YoY Sales growth", "Expenses", "Operating Profit", "OPM %", "Other Income", "Interest", "Depreciation", "Profit before tax", "Tax %", "Net Profit", "EPS in Rs"].filter((r) => table.rows[r]);
  return (
    <section className="card fx-card fx-quarters">
      <header className="fx-head">
        <div><h3>Quarterly results</h3><span className="muted small">₹ crore, except per share · from Screener.in</span></div>
        <Segmented size="small" label="Metric" value={metric} onChange={(v) => { setMetric(v); setHover(null); }} options={METRICS.map((x) => ({ id: x.id, label: x.label }))} />
      </header>
      <div className="fx-q-summary">
        <div><small>{periods[focus]}</small><b>{fmtMetric(series[focus], m.unit)}</b></div>
        <div><small>vs same quarter last year</small><b className={tone(latestYoY)}>{latestYoY == null ? "–" : metric === "OPM %" ? `${signed(latestYoY, 1)} pts` : `${signed(latestYoY, 1)}%`}</b></div>
        <div><small>Highest in {take} quarters</small><b>{fmtMetric(Math.max(...series.filter((v) => v != null)), m.unit)}</b></div>
      </div>
      <svg className="fx-qchart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${m.label} by quarter`} onMouseLeave={() => setHover(null)}>
        <line x1={P.l} x2={W - P.r} y1={y0} y2={y0} className="fx-axis" />
        {series.map((v, i) => {
          const g = metric === "OPM %" ? ppt(i) : yoy(i);
          const h = v == null ? 0 : scale(v);
          const x = P.l + i * bw + bw * 0.18;
          const w = bw * 0.64;
          return (
            <g key={periods[i]} className={`fx-q ${i === focus ? "on" : ""} ${v != null && v < 0 ? "neg" : ""}`} onMouseEnter={() => setHover(i)}>
              <rect x={P.l + i * bw} y={P.t - 20} width={bw} height={H - P.t + 20} className="fx-q-hit" />
              <rect x={x} y={v != null && v < 0 ? y0 : y0 - h} width={w} height={Math.max(1, h)} rx="3" className="fx-q-bar" />
              {g != null && <text x={x + w / 2} y={(v != null && v < 0 ? y0 : y0 - h) - 6} textAnchor="middle" className={`fx-q-g ${g >= 0 ? "up" : "down"}`}>{g >= 0 ? "+" : ""}{num(g, Math.abs(g) < 10 ? 1 : 0)}{metric === "OPM %" ? "" : "%"}</text>}
              <text x={x + w / 2} y={H - 8} textAnchor="middle" className="fx-q-x">{short(periods[i])}</text>
              <title>{`${periods[i]}: ${fmtMetric(v, m.unit)}${g == null ? "" : ` · ${signed(g, 1)}${metric === "OPM %" ? " pts" : "%"} year on year`}`}</title>
            </g>
          );
        })}
      </svg>
      <p className="fx-note">The figure above each bar is the change from the same quarter a year earlier{metric === "OPM %" ? ", in percentage points" : ""}.</p>
      <button type="button" className="fx-more" aria-expanded={showTable} onClick={() => setShowTable(!showTable)}>{showTable ? "Hide the full table" : "Show the full table"}</button>
      {showTable && (
        <div className="table-wrap">
          <table className="table fd-table">
            <thead><tr><th />{periods.map((p) => <th key={p} className="num">{p}</th>)}</tr></thead>
            <tbody>
              {rowsForTable.map((l) => (
                <tr key={l}><td>{l}</td>{table.rows[l].slice(-take).map((x, i) => <td key={periods[i]} className="num">{x == null ? "–" : `${num(x, Math.abs(x) >= 100 ? 0 : 2)}${/%|growth/.test(l) ? "%" : ""}`}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- ownership and profile

function Donut({ parts }) {
  const R = 54, C = 2 * Math.PI * R;
  let acc = 0;
  return (
    <svg viewBox="0 0 140 140" className="fx-donut" role="img" aria-label="Shareholding split">
      <circle cx="70" cy="70" r={R} className="fx-donut-bg" />
      {parts.map(([k, v, c]) => {
        const len = (v / 100) * C;
        const el = <circle key={k} cx="70" cy="70" r={R} stroke={c} strokeDasharray={`${Math.max(0, len - 1.5)} ${C}`} strokeDashoffset={-acc} className="fx-donut-seg" />;
        acc += len;
        return el;
      })}
    </svg>
  );
}

function Spark({ values, color }) {
  const v = values.filter((x) => x != null);
  if (v.length < 2) return <span className="fx-spark" />;
  const lo = Math.min(...v), hi = Math.max(...v);
  const pts = v.map((x, i) => `${(i / (v.length - 1)) * 100},${hi === lo ? 12 : 22 - ((x - lo) / (hi - lo)) * 20}`).join(" ");
  return <svg className="fx-spark" viewBox="0 0 100 24" preserveAspectRatio="none"><polyline points={pts} stroke={color} /></svg>;
}

function Ownership({ table }) {
  const rows = table?.rows || {};
  const parts = HOLDERS.map(([k, c]) => [k, rows[k]?.at(-1), c]).filter(([, v]) => v != null && v > 0);
  if (!parts.length) return null;
  const top = parts[0];
  const holders = rows["No. of Shareholders"];
  return (
    <section className="card fx-card">
      <header className="fx-head"><h3>Who owns it</h3><span className="muted small">{table.periods.at(-1)} · change over the quarter and the year</span></header>
      <div className="fx-own">
        <div className="fx-donut-wrap">
          <Donut parts={parts} />
          <div className="fx-donut-mid"><b>{num(top[1], 1)}%</b><small>{top[0]}</small></div>
        </div>
        <ul className="fx-holders">
          {parts.map(([k, v, c]) => {
            const series = rows[k] || [];
            const q = series.length > 1 ? v - series.at(-2) : null;
            const y = series.length > 4 ? v - series.at(-5) : null;
            return (
              <li key={k}>
                <span className="fx-h-name"><i style={{ background: c }} />{k}</span>
                <b>{num(v, 2)}%</b>
                <Spark values={series.slice(-12)} color={c} />
                <span className={`fx-h-chg ${tone(q)}`} title="change over the last quarter">{q == null ? "–" : `${signed(q, 2)}`}</span>
                <span className={`fx-h-chg ${tone(y)}`} title="change over the last year">{y == null ? "–" : `${signed(y, 2)}`}</span>
              </li>
            );
          })}
        </ul>
      </div>
      {holders?.length > 1 && <p className="fx-note">{num(holders.at(-1), 0)} shareholders, {signed(((holders.at(-1) / holders.at(-2)) - 1) * 100, 1)}% over the quarter. Changes are in percentage points.</p>}
    </section>
  );
}

function Profile({ d }) {
  const f = d.fundamentals;
  const [more, setMore] = useState(false);
  const about = f?.about || "";
  const long = about.length > 360;
  return (
    <section className="card fx-card">
      <header className="fx-head"><h3>Company profile</h3><SourceNote status={{ fetched_at: d.fetched_at, stale: d.stale, error: d.error }} source="Screener.in" /></header>
      <div className="fx-facts">
        <div><small>Sector</small><b>{f?.sector || d.index_sector || "–"}</b></div>
        <div><small>Industry</small><b>{f?.industry || "–"}</b></div>
        <div><small>Website</small><b>{f?.website ? <a href={f.website} target="_blank" rel="noreferrer">{f.website.replace(/^https?:\/\/(www\.)?/, "").replace(/\/$/, "")}</a> : "–"}</b></div>
      </div>
      {about ? (
        <>
          <p className="fx-about">{long && !more ? `${about.slice(0, 360).replace(/\s+\S*$/, "")}…` : about}</p>
          {long && <button type="button" className="fx-more" onClick={() => setMore(!more)}>{more ? "Show less" : "Read more"}</button>}
        </>
      ) : <p className="muted small">{d.error ? "Company details are unavailable right now." : "No company description published."}</p>}
    </section>
  );
}

// ---------------------------------------------------------------- page

function Dashboard({ d, last }) {
  const f = d.fundamentals;
  return (
    <div className="fx">
      <div className="fx-section-title"><h2>Fundamentals</h2><span className="muted small">company figures from Screener.in · price statistics from Kite</span></div>
      <div className="fx-glance">
        <PriceCard d={d} last={last} />
        <ReturnsCard s={d.live} />
        <ValueCard f={f} last={last} />
      </div>
      <Quarters table={f?.quarters} />
      <div className="fx-pair">
        <Ownership table={f?.shareholding} />
        <Profile d={d} />
      </div>
    </div>
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
  const kpis = useMemo(() => (d ? [
    ["Market cap", cr(f?.mcap_cr && f.price ? f.mcap_cr * (last / f.price) : f?.mcap_cr)],
    ["P/E", f?.pe != null ? num(f.pe) : "–"],
    ["ROE", f?.roe != null ? `${num(f.roe)}%` : "–"],
    ["Dividend yield", f?.div_yield != null ? `${num(f.div_yield)}%` : "–"],
    ["ATR", s?.atr_pct != null ? inr((s.atr_pct / 100) * s.last) : "–"],
    ["Beta", s?.beta != null ? num(s.beta) : "–"],
    ["RVOL", s?.rvol != null ? num(s.rvol) : "–"],
    ["Promoters", f?.shareholding?.rows?.Promoters ? `${num(f.shareholding.rows.Promoters.at(-1))}%` : "–"],
  ] : []), [d, f, s, last]);
  return (
    <div className="tt-page">
      <StockView symbol={symbol} exchange={exchange} kpis={nse ? (d ? kpis : "loading") : null} {...view} />
      {nse && d && <Dashboard d={d} last={last} />}
      {nse && !d && <ErrorNote error={q.error} />}
    </div>
  );
}
