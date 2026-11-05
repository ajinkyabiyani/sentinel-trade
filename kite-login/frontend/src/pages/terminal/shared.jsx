import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../lib/api.js";
import { num, signed, tone } from "../../lib/format.js";
import { useTicks } from "../../lib/ticks.js";
import { Segmented, Spinner, useLoad } from "../../components/ui.jsx";

// Shared pieces of the market terminal: the live Nifty 500 feed, treemap, sector bars, mood gauge and index chart.
export const PERIODS = ["1D", "1W", "1M", "3M", "6M", "YTD", "1Y"];
export const periodOptions = (list = PERIODS) => list.map((id) => ({ id, label: id }));

// A stock's return for a period, on the live price when a tick has arrived.
export const periodReturn = (r, period) => (period === "1D" ? r.chg : r.ret?.[period] ?? null);

// Every Nifty 500 stock, refreshed from the backend and marked to live ticks in between.
export function useStocks() {
  const q = useLoad(api.terminalStocks, [], 20_000);
  const rows = q.data?.rows;
  const { ticks, live } = useTicks((rows || []).map((r) => `NSE:${r.symbol}`));
  const marked = useMemo(() => (rows || []).map((r) => {
    const px = ticks[`NSE:${r.symbol}`]?.last_price;
    if (!px || px === r.last) return r;
    const move = px / r.last;
    const ret = Object.fromEntries(Object.entries(r.ret || {}).map(([k, v]) => [k, v == null ? v : (1 + v / 100) * move * 100 - 100]));
    return { ...r, last: px, chg: r.prev ? (px / r.prev - 1) * 100 : r.chg, ret, mcap_cr: r.mcap_cr ? r.mcap_cr * move : r.mcap_cr };
  }), [rows, ticks]);
  return { rows: marked, live, meta: q.data, loading: q.loading && !q.data, error: q.error, reload: q.reload };
}

// Green / red scale of the heatmap, saturated at ±3%.
export function heatColor(v) {
  if (v == null) return "var(--hm-flat)";
  const t = Math.min(1, Math.abs(v) / 3);
  const from = [64, 72, 88];
  const to = v >= 0 ? [18, 148, 92] : [212, 52, 74];
  return `rgb(${from.map((c, i) => Math.round(c + (to[i] - c) * t)).join(",")})`;
}

// Squarified treemap (Bruls, Huizing, van Wijk): items {size}, laid out in the box; returns items with x, y, w, h.
export function squarify(items, box) {
  const total = items.reduce((a, i) => a + i.size, 0);
  if (!total || box.w <= 0 || box.h <= 0) return [];
  const scale = (box.w * box.h) / total;
  const queue = [...items].sort((a, b) => b.size - a.size).map((i) => ({ ...i, area: i.size * scale }));
  const out = [];
  let { x, y, w, h } = box;
  const worst = (row, side) => {
    const s = row.reduce((a, r) => a + r.area, 0);
    const big = Math.max(...row.map((r) => r.area));
    const small = Math.min(...row.map((r) => r.area));
    return Math.max((side * side * big) / (s * s), (s * s) / (side * side * small));
  };
  while (queue.length) {
    const side = Math.min(w, h);
    const row = [queue.shift()];
    while (queue.length && worst([...row, queue[0]], side) <= worst(row, side)) row.push(queue.shift());
    const s = row.reduce((a, r) => a + r.area, 0);
    const thick = s / side;
    let at = 0;
    for (const r of row) {
      const len = r.area / thick;
      out.push(w >= h ? { ...r, x, y: y + at, w: thick, h: len } : { ...r, x: x + at, y, w: len, h: thick });
      at += len;
    }
    if (w >= h) { x += thick; w -= thick; } else { y += thick; h -= thick; }
  }
  return out;
}

const SECTOR_HEAD = 16;

// Stocks grouped by sector, sized by market cap (traded value until market caps have loaded), coloured by return.
export function Treemap({ rows, period = "1D", onPick, height = 620, compact }) {
  const ref = useRef(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    if (!ref.current) return undefined;
    const ro = new ResizeObserver(([e]) => setWidth(e.contentRect.width));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  const byCap = rows.filter((r) => r.mcap_cr).length >= rows.length * 0.9;
  const boxes = useMemo(() => {
    if (!width) return [];
    const groups = {};
    for (const r of rows) {
      const size = byCap ? r.mcap_cr : r.value_cr;
      if (size > 0) (groups[r.sector || "Other"] ||= []).push({ ...r, size });
    }
    const sectors = Object.entries(groups).map(([name, items]) => ({ name, items, size: items.reduce((a, i) => a + i.size, 0) }));
    return squarify(sectors, { x: 0, y: 0, w: width, h: height }).map((s) => ({
      ...s, cells: squarify(s.items, { x: s.x + 1, y: s.y + SECTOR_HEAD, w: Math.max(0, s.w - 2), h: Math.max(0, s.h - SECTOR_HEAD - 1) }),
    }));
  }, [rows, width, height, byCap]);
  return (
    <div className="hm" ref={ref} style={{ height }}>
      {boxes.map((s) => (
        <div key={s.name}>
          <span className="hm-sector" style={{ left: s.x + 3, top: s.y + 1, width: Math.max(0, s.w - 6) }} title={s.name}>{s.name}</span>
          {s.cells.map((c) => {
            const v = periodReturn(c, period);
            const big = c.w > (compact ? 44 : 38) && c.h > (compact ? 26 : 22);
            const large = c.w > 80 && c.h > 46;
            return (
              <button type="button" key={c.symbol} className="hm-cell" style={{ left: c.x, top: c.y, width: c.w, height: c.h, background: heatColor(v) }}
                onClick={() => onPick?.(c.symbol)} title={`${c.name} (${c.symbol})\n${v == null ? "no data" : `${signed(v)}%`} · ₹${num(c.last)}${c.mcap_cr ? ` · market cap ₹${num(c.mcap_cr, 0)} Cr` : ""}`}>
                {big && <b style={{ fontSize: large ? 13 : 10 }}>{c.symbol}</b>}
                {big && c.h > 30 && <small style={{ fontSize: large ? 11 : 9 }}>{v == null ? "–" : `${signed(v, large ? 2 : 1)}%`}</small>}
              </button>
            );
          })}
        </div>
      ))}
      {!byCap && rows.length > 0 && <span className="hm-note">Box size: value traded today, until market caps finish loading</span>}
    </div>
  );
}

export function HeatLegend() {
  return (
    <div className="hm-legend" aria-label="Colour scale">
      {[-3, -2, -1, 0, 1, 2, 3].map((v) => <span key={v} style={{ background: heatColor(v) }}>{v > 0 ? "+" : ""}{v}%</span>)}
    </div>
  );
}

export function SectorBars({ sectors, ticks = {} }) {
  const [period, setPeriod] = useState("1D");
  const rows = useMemo(() => (sectors || []).map((s) => {
    const px = ticks[s.key]?.last_price;
    let v = s.ret?.[period];
    if (px && v != null && s.last) v = (1 + v / 100) * (px / s.last) * 100 - 100;
    return { ...s, v };
  }).filter((s) => s.v != null).sort((a, b) => b.v - a.v), [sectors, ticks, period]);
  const max = Math.max(0.01, ...rows.map((s) => Math.abs(s.v)));
  return (
    <section className="card tt-card">
      <header className="card-head">
        <h3>Sector performance</h3>
        <Segmented size="small" label="Period" value={period} onChange={setPeriod} options={periodOptions(["1D", "1W", "1M", "3M", "YTD", "1Y"])} />
      </header>
      {sectors ? (
        <ul className="tt-bars">
          {rows.map((s) => (
            <li key={s.name}>
              <span className="tt-bar-name">{s.label}</span>
              <b className={tone(s.v)}>{signed(s.v)}%</b>
              <span className="tt-bar-track"><i className={s.v >= 0 ? "pos" : "neg"} style={{ width: `${(Math.abs(s.v) / max) * 100}%` }} /></span>
            </li>
          ))}
          {!rows.length && <li className="muted small">Period returns appear once the daily candles have loaded.</li>}
        </ul>
      ) : <Spinner />}
    </section>
  );
}

const BANDS = [[0, 25, "#d9344a", "Extreme fear"], [25, 45, "#ee8a3c", "Fear"], [45, 55, "#e6b800", "Neutral"], [55, 76, "#4cc38a", "Greed"], [76, 100, "#128a58", "Extreme greed"]];

function arc(cx, cy, r, a0, a1) {
  const p = (a) => [cx + r * Math.cos(Math.PI * (1 - a / 100)), cy - r * Math.sin(Math.PI * (1 - a / 100))];
  const [x0, y0] = p(a0);
  const [x1, y1] = p(a1);
  return `M${x0.toFixed(2)} ${y0.toFixed(2)}A${r} ${r} 0 0 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
}

export function MoodGauge({ mood }) {
  const s = mood?.score;
  return (
    <section className="card tt-card">
      <header className="card-head">
        <h3>Market mood</h3>
        <span className="muted small" title="This app's own measure, not a published index: the average of the readings below, using the Nifty 500 and India VIX.">0 to 100, calculated here</span>
      </header>
      {s == null ? <div className="chart-empty" style={{ height: 180 }}><Spinner /></div> : (
        <>
          <svg viewBox="0 0 240 134" className="tt-gauge" role="img" aria-label={`Market mood ${s} out of 100: ${mood.label}`}>
            {BANDS.map(([a, b, c]) => <path key={a} d={arc(120, 118, 96, a + 0.6, b - 0.6)} stroke={c} strokeWidth="20" fill="none" />)}
            <line x1="120" y1="118" x2={120 + 72 * Math.cos(Math.PI * (1 - s / 100))} y2={118 - 72 * Math.sin(Math.PI * (1 - s / 100))} stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
            <circle cx="120" cy="118" r="5" fill="currentColor" />
          </svg>
          <div className="tt-gauge-read"><b>{s}</b><span>{mood.label}</span></div>
          <ul className="tt-mood-parts">
            {mood.parts.map((p) => <li key={p.name} title={p.note}><span>{p.name}</span><small className="muted">{p.note}</small><b>{p.score}</b></li>)}
          </ul>
        </>
      )}
    </section>
  );
}

const LINE_COLORS = ["var(--accent)", "#e6a700", "#d9534f", "#3aa76d"];

// Index lines rebased to 100 at the start of the range.
export function NormalizedChart() {
  const [range, setRange] = useState("1Y");
  const q = useLoad(() => api.terminalNormalized(range), [range]);
  const series = q.data?.series || [];
  const W = 640, H = 240, L = 8, R = 44, T = 10, B = 22;
  const all = series.flatMap((s) => s.points.map((p) => p[1]));
  const lo = Math.min(...all, 100), hi = Math.max(...all, 100);
  const dates = series[0]?.points.map((p) => p[0]) || [];
  const X = (i, n) => L + (i / Math.max(1, n - 1)) * (W - L - R);
  const Y = (v) => T + (1 - (v - lo) / Math.max(0.01, hi - lo)) * (H - T - B);
  return (
    <section className="card tt-card">
      <header className="card-head">
        <h3>Index performance</h3>
        <Segmented size="small" label="Range" value={range} onChange={setRange} options={periodOptions(["1W", "1M", "3M", "6M", "YTD", "1Y"])} />
      </header>
      {series.length ? (
        <>
          <svg viewBox={`0 0 ${W} ${H}`} className="tt-lines" role="img" aria-label="Indices rebased to 100">
            <line x1={L} x2={W - R} y1={Y(100)} y2={Y(100)} className="tt-base" />
            <text x={W - R + 4} y={Y(100) + 3} className="tt-axis">100</text>
            <text x={W - R + 4} y={Y(hi) + 8} className="tt-axis">{num(hi, 0)}</text>
            <text x={W - R + 4} y={Y(lo)} className="tt-axis">{num(lo, 0)}</text>
            {series.map((s, i) => <path key={s.label} fill="none" stroke={LINE_COLORS[i]} strokeWidth="1.8" d={s.points.map((p, j) => `${j ? "L" : "M"}${X(j, s.points.length).toFixed(1)} ${Y(p[1]).toFixed(1)}`).join("")} />)}
            {dates.length > 1 && <text x={L} y={H - 6} className="tt-axis">{dates[0]}</text>}
            {dates.length > 1 && <text x={W - R} y={H - 6} textAnchor="end" className="tt-axis">{dates.at(-1)}</text>}
          </svg>
          <div className="tt-legend">
            {series.map((s, i) => {
              const v = s.points.at(-1)[1] - 100;
              return <span key={s.label}><i style={{ background: LINE_COLORS[i] }} />{s.label} <b className={tone(v)}>{signed(v, 1)}%</b></span>;
            })}
          </div>
        </>
      ) : <div className="chart-empty" style={{ height: 200 }}>{q.loading ? <Spinner /> : <span className="muted small">Appears once the daily candles have loaded.</span>}</div>}
    </section>
  );
}

// Tells the reader how old public-source data is, and when the source refused.
export function SourceNote({ status, source }) {
  if (!status) return null;
  const when = status.fetched_at ? new Date(status.fetched_at).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : null;
  return (
    <span className={`muted small${status.stale ? " tt-stale" : ""}`} title={status.error || undefined}>
      {status.stale ? (when ? `Showing data from ${when}: ${source} is not answering` : `${source} is not answering`) : `${source}${when ? ` · ${when}` : ""}`}
    </span>
  );
}

// One-line progress for the slow background jobs.
export function LoadNote({ meta }) {
  if (!meta) return null;
  const c = meta.candles, k = meta.caps;
  return (
    <>
      {c?.running && <span className="muted small">Loading daily candles {c.done}/{c.total}: period returns and indicators fill in as this completes.</span>}
      {k?.running && <span className="muted small">Market caps {k.done}/{k.total} (loaded slowly from Screener.in, about 30 minutes the first time).</span>}
      {k?.paused && <span className="muted small tt-stale">Market cap loading paused: {k.paused}.</span>}
    </>
  );
}
