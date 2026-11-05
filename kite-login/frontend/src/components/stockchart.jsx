import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { num } from "../lib/format.js";

// Daily / weekly stock chart in three panes: price (candles, bars or line; log option) with EMA 21, SMA 50,
// SMA 200 and the 52-week high; RVOL (volume / 50-bar average volume); RS line vs Nifty 50 rebased to 100.
// Indicators are computed on the full history, so the visible window starts with warmed-up averages.

export const sma = (v, p) => v.map((_, i) => (i < p - 1 ? null : v.slice(i - p + 1, i + 1).reduce((a, b) => a + b, 0) / p));
export const emaS = (v, p) => {
  const k = 2 / (p + 1);
  let prev = null;
  return v.map((x, i) => {
    if (i < p - 1) return null;
    prev = prev === null ? v.slice(0, p).reduce((a, b) => a + b, 0) / p : x * k + prev * (1 - k);
    return prev;
  });
};

export function weekly(c) {
  const out = [];
  for (const x of c) {
    const d = new Date(x[0]);
    const monday = new Date(d);
    monday.setDate(d.getDate() - ((d.getDay() + 6) % 7));
    const k = monday.toISOString().slice(0, 10);
    const last = out.at(-1);
    if (last && last.k === k) {
      last.c[2] = Math.max(last.c[2], x[2]);
      last.c[3] = Math.min(last.c[3], x[3]);
      last.c[4] = x[4];
      last.c[5] += x[5];
    } else out.push({ k, c: [x[0], x[1], x[2], x[3], x[4], x[5]] });
  }
  return out.map((w) => w.c);
}

function useWidth(fallback = 800) {
  // callback ref: re-attaches whenever the measured element changes (a loading placeholder, then the chart)
  const [w, setW] = useState(fallback);
  const ro = useRef(null);
  const ref = useCallback((el) => {
    ro.current?.disconnect();
    if (!el) return;
    const now = Math.round(el.getBoundingClientRect().width);   // measure at once, then follow resizes
    if (now > 0) setW(Math.max(200, now));
    ro.current = new ResizeObserver(([e]) => setW(Math.max(200, Math.round(e.contentRect.width))));
    ro.current.observe(el);
  }, []);
  useEffect(() => () => ro.current?.disconnect(), []);
  return [ref, w];
}

const IND = [
  { id: "ema21", label: "EMA 21", cls: "e21" },
  { id: "sma50", label: "SMA 50", cls: "s50" },
  { id: "sma200", label: "SMA 200", cls: "s200" },
];

export function StockChart({ candles, bench, bars, kind = "candle", log = false, ind = { ema21: true, sma50: true, sma200: true }, hi52 }) {
  const [ref, w] = useWidth();
  const [hover, setHover] = useState(null);
  const H = { price: 340, gap: 14, vol: 64, rs: 86, axis: 22 };
  const total = H.price + H.gap + H.vol + H.gap + H.rs + H.axis;
  const pad = { l: 8, r: 70, t: 18 };

  const g = useMemo(() => {
    if (!candles?.length) return null;
    const closes = candles.map((c) => c[4]);
    const vols = candles.map((c) => c[5]);
    const lines = { ema21: emaS(closes, 21), sma50: sma(closes, 50), sma200: sma(closes, 200) };
    const vavg = sma(vols, 50);
    const bmap = new Map((bench || []).map((b) => [b[0].slice(0, 10), b[4]]));
    let lastB = null;
    const ratio = candles.map((c) => {
      const b = bmap.get(c[0].slice(0, 10));
      if (b) lastB = b;
      return lastB ? c[4] / lastB : null;
    });
    const from = Math.max(0, candles.length - bars);
    const view = candles.slice(from);
    const n = view.length;
    const L = (arr) => arr.slice(from);
    const ov = Object.fromEntries(Object.entries(lines).map(([k, v]) => [k, L(v)]));
    const rv = view.map((c, i) => (vavg[from + i] ? c[5] / vavg[from + i] : null));
    const r0 = L(ratio).find((x) => x);
    const rs = L(ratio).map((x) => (x && r0 ? (x / r0) * 100 : null));
    const vis = [...view.flatMap((c) => (kind === "line" ? [c[4]] : [c[2], c[3]])), ...IND.filter((i) => ind[i.id]).flatMap((i) => ov[i.id].filter((x) => x != null))];
    if (hi52 && hi52 < Math.max(...vis) * 1.25) vis.push(hi52);
    let lo = Math.min(...vis);
    let hi = Math.max(...vis);
    const f = (v) => (log ? Math.log(v) : v);
    const span = f(hi) - f(lo) || 1;
    const loF = f(lo) - span * 0.04;
    const hiF = f(hi) + span * 0.06;
    const step = (w - pad.l - pad.r) / n;
    const x = (i) => pad.l + i * step + step / 2;
    const y = (v) => pad.t + (1 - (f(v) - loF) / (hiF - loF)) * (H.price - pad.t);
    const vTop = H.price + H.gap;
    const vmax = Math.max(2, ...rv.filter((v) => v != null));
    const yv = (v) => vTop + H.vol - (Math.min(v, vmax) / vmax) * H.vol;
    const rTop = vTop + H.vol + H.gap;
    const rsv = rs.filter((v) => v != null);
    const rlo = Math.min(...rsv, 100);
    const rhi = Math.max(...rsv, 100);
    const yr = (v) => rTop + 6 + (1 - (v - rlo) / (rhi - rlo || 1)) * (H.rs - 12);
    const path = (arr, yy) => {
      let d = "";
      let pen = false;
      arr.forEach((v, i) => {
        if (v == null) pen = false;
        else {
          d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${yy(v).toFixed(1)}`;
          pen = true;
        }
      });
      return d;
    };
    const ticks = Array.from({ length: 5 }, (_, k) => {
      const v = loF + ((hiF - loF) * (k + 0.5)) / 5;
      return log ? Math.exp(v) : v;
    });
    const nx = Math.min(8, n);
    const xt = Array.from({ length: nx }, (_, k) => Math.round((k * (n - 1)) / Math.max(1, nx - 1)));
    return { view, n, ov, rv, rs, x, y, yv, yr, step, path, ticks, xt, vTop, rTop, vmax, rlo, rhi };
  }, [candles, bench, bars, kind, log, ind, hi52, w]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!g) return <div ref={ref} className="chart-empty" style={{ height: total }}>No candles</div>;
  const { view, n, ov, rv, rs, x, y, yv, yr, step, path, ticks, xt, vTop, rTop } = g;
  const cw = Math.max(1, Math.min(10, step * 0.62));
  const last = view[n - 1][4];
  const hc = hover != null ? view[hover] : null;
  const fmt = (v) => num(v, v > 1000 ? 0 : 2);
  const span = (new Date(view[n - 1][0]) - new Date(view[0][0])) / 864e5;
  const dl = (iso) => new Date(iso).toLocaleDateString("en-IN", span > 400 ? { month: "short", year: "2-digit" } : { day: "numeric", month: "short" });
  const onMove = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    const i = Math.floor(((e.clientX - r.left) * (w / r.width) - pad.l) / step);
    setHover(i >= 0 && i < n ? i : null);
  };

  return (
    <div ref={ref} className="stock-chart">
      <div className="sc-legend">
        {IND.filter((i) => ind[i.id]).map((i) => (
          <span key={i.id} className={i.cls}>{i.label} {fmt((hc ? ov[i.id][hover] : ov[i.id].at(-1)) ?? NaN)}</span>
        ))}
        {hc && (
          <span className="sc-ohlc">
            {new Date(hc[0]).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" })} · O {fmt(hc[1])} H {fmt(hc[2])} L {fmt(hc[3])} C {fmt(hc[4])}
            {rv[hover] != null && ` · RVOL ${num(rv[hover], 1)}×`}
          </span>
        )}
      </div>
      <svg width={w} height={total} onMouseMove={onMove} onMouseLeave={() => setHover(null)} role="img" aria-label="Price, relative volume and relative strength chart">
        {ticks.map((t) => (
          <g key={t}>
            <line x1={pad.l} x2={w - pad.r} y1={y(t)} y2={y(t)} className="sc-grid" />
            <text x={w - pad.r + 6} y={y(t) + 4} className="axis">{fmt(t)}</text>
          </g>
        ))}
        {hi52 && y(hi52) > pad.t - 4 && (
          <g>
            <line x1={pad.l} x2={w - pad.r} y1={y(hi52)} y2={y(hi52)} className="sc-52" />
            <text x={w - pad.r - 4} y={y(hi52) - 5} textAnchor="end" className="sc-52t">52w high {fmt(hi52)} · price {num((1 - last / hi52) * 100, 1)}% below</text>
          </g>
        )}
        {kind === "line" ? <path d={path(view.map((c) => c[4]), y)} className="sc-line" />
          : view.map((c, i) => (
            <g key={c[0]} className={c[4] >= c[1] ? "cdl up" : "cdl down"}>
              <line x1={x(i)} x2={x(i)} y1={y(c[2])} y2={y(c[3])} />
              {kind === "candle"
                ? <rect x={x(i) - cw / 2} width={cw} y={y(Math.max(c[1], c[4]))} height={Math.max(1, Math.abs(y(c[1]) - y(c[4])))} />
                : <path d={`M${x(i) - cw / 2},${y(c[1])}H${x(i)}M${x(i)},${y(c[4])}H${x(i) + cw / 2}`} />}
            </g>
          ))}
        {IND.filter((i) => ind[i.id]).map((i) => <path key={i.id} d={path(ov[i.id], y)} className={`sc-ma ${i.cls}`} />)}
        <g transform={`translate(${w - pad.r + 2},${y(last) - 10})`}>
          <rect width={pad.r - 4} height="20" rx="5" className="last-pill" />
          <text x={(pad.r - 4) / 2} y="14" textAnchor="middle" className="last-pill-text">{fmt(last)}</text>
        </g>

        <text x={pad.l} y={vTop + 10} className="sc-pane">RVOL (volume ÷ 50-bar avg)</text>
        <line x1={pad.l} x2={w - pad.r} y1={yv(1)} y2={yv(1)} className="sc-grid" />
        <text x={w - pad.r + 6} y={yv(1) + 4} className="axis">1×</text>
        {rv.map((v, i) => v != null && (
          <rect key={i} x={x(i) - cw / 2} width={cw} y={yv(v)} height={Math.max(1, yv(0) - yv(v))} className={`sc-vol ${view[i][4] >= view[i][1] ? "up" : "down"}${v >= 1.5 ? " hi" : ""}`} />
        ))}

        <text x={pad.l} y={rTop + 10} className="sc-pane">RS line vs Nifty 50 (rebased to 100)</text>
        <line x1={pad.l} x2={w - pad.r} y1={yr(100)} y2={yr(100)} className="sc-grid" />
        <path d={path(rs, yr)} className="sc-rs" />
        {rs.at(-1) != null && <text x={w - pad.r + 6} y={yr(rs.at(-1)) + 4} className="axis sc-rs-t">{num(rs.at(-1), 0)}</text>}

        {xt.map((i) => <text key={i} x={x(i)} y={total - 6} className="axis" textAnchor={i === 0 ? "start" : i === n - 1 ? "end" : "middle"}>{dl(view[i][0])}</text>)}
        {hc && <line x1={x(hover)} x2={x(hover)} y1={pad.t} y2={total - H.axis} className="cross" />}
      </svg>
    </div>
  );
}
