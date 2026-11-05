import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { dateTime, num, shortSector, signed } from "../lib/format.js";

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

export function ema(values, period) {
  const k = 2 / (period + 1);
  let prev = null;
  return values.map((v, i) => {
    if (i < period - 1) return null;
    prev = prev === null ? values.slice(0, period).reduce((a, b) => a + b, 0) / period : v * k + prev * (1 - k);
    return prev;
  });
}

const fmtDay = (d) => new Date(d).toLocaleDateString("en-IN", { day: "numeric", month: "short" });

// Daily line chart: green above the range's starting level, red below it (as in a P&L curve), optional EMA.
export function LineChart({ points, emaLine, height = 300 }) {
  const [ref, w] = useWidth();
  const [hover, setHover] = useState(null);
  const id = useId().replace(/:/g, "");
  const pad = { l: 8, r: 70, t: 14, b: 26 };
  const n = points.length;

  const geo = useMemo(() => {
    if (n < 2) return null;
    const vals = points.map((p) => p[1]).concat((emaLine || []).filter((v) => v !== null));
    let lo = Math.min(...vals);
    let hi = Math.max(...vals);
    const padv = (hi - lo) * 0.08 || hi * 0.01;
    lo -= padv;
    hi += padv;
    const x = (i) => pad.l + (i * (w - pad.l - pad.r)) / (n - 1);
    const y = (v) => pad.t + ((hi - v) * (height - pad.t - pad.b)) / (hi - lo);
    const line = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
    const base = points[0][1];
    const by = y(base);
    const area = `${line}L${x(n - 1).toFixed(1)},${by.toFixed(1)}L${x(0).toFixed(1)},${by.toFixed(1)}Z`;
    const emaPath = emaLine
      ? emaLine.map((v, i) => (v === null ? "" : `${emaLine[i - 1] == null ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`)).join("")
      : "";
    const ticks = Array.from({ length: 5 }, (_, k) => lo + ((k + 0.5) * (hi - lo)) / 5);
    const xt = Array.from({ length: Math.min(6, n) }, (_, k) => Math.round((k * (n - 1)) / Math.max(1, Math.min(6, n) - 1)));
    return { x, y, line, area, by, base, emaPath, ticks, xt };
  }, [points, emaLine, w, height, n]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!geo) return <div ref={ref} className="chart-empty" style={{ height }}>No data</div>;
  const { x, y, line, area, by, base, emaPath, ticks, xt } = geo;
  const onMove = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    const i = Math.round(((e.clientX - r.left - pad.l) / (w - pad.l - pad.r)) * (n - 1));
    setHover(Math.max(0, Math.min(n - 1, i)));
  };
  const hp = hover !== null ? points[hover] : null;

  return (
    <div ref={ref} className="chart" style={{ height }}>
      <svg width={w} height={height} onMouseMove={onMove} onMouseLeave={() => setHover(null)} role="img" aria-label="Price chart">
        <defs>
          <linearGradient id={`up${id}`} x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor="var(--up)" stopOpacity="0.32" />
            <stop offset="100%" stopColor="var(--up)" stopOpacity="0.02" />
          </linearGradient>
          <linearGradient id={`dn${id}`} x1="0" x2="0" y1="1" y2="0">
            <stop offset="0%" stopColor="var(--down)" stopOpacity="0.32" />
            <stop offset="100%" stopColor="var(--down)" stopOpacity="0.02" />
          </linearGradient>
          <clipPath id={`cu${id}`}>
            <rect x="0" y="0" width={w} height={Math.max(0, by)} />
          </clipPath>
          <clipPath id={`cd${id}`}>
            <rect x="0" y={by} width={w} height={Math.max(0, height - by)} />
          </clipPath>
        </defs>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={pad.l} x2={w - pad.r} y1={y(t)} y2={y(t)} className="grid" />
            {Math.abs(y(t) - by) > 14 && <text x={w - pad.r + 10} y={y(t) + 4} className="axis">{num(t, t > 1000 ? 0 : 2)}</text>}
          </g>
        ))}
        {xt.map((i) => (
          <text key={i} x={x(i)} y={height - 6} className="axis" textAnchor={i === 0 ? "start" : i === n - 1 ? "end" : "middle"}>
            {fmtDay(points[i][0])}
          </text>
        ))}
        <path d={area} fill={`url(#up${id})`} clipPath={`url(#cu${id})`} />
        <path d={area} fill={`url(#dn${id})`} clipPath={`url(#cd${id})`} />
        <line x1={pad.l} x2={w - pad.r} y1={by} y2={by} className="baseline" />
        <path d={line} className="line up" clipPath={`url(#cu${id})`} />
        <path d={line} className="line down" clipPath={`url(#cd${id})`} />
        {emaPath && <path d={emaPath} className="ema" />}
        <g transform={`translate(${w - pad.r + 4},${by - 10})`}>
          <rect width="62" height="20" rx="10" className="base-pill" />
          <text x="31" y="14" textAnchor="middle" className="base-pill-text">{num(base, base > 1000 ? 0 : 2)}</text>
        </g>
        {hp && (
          <g>
            <line x1={x(hover)} x2={x(hover)} y1={pad.t} y2={height - pad.b} className="cross" />
            <circle cx={x(hover)} cy={y(hp[1])} r="4.5" className={hp[1] >= base ? "dot up" : "dot down"} />
          </g>
        )}
      </svg>
      {hp && (
        <div className="chart-tip" style={{ left: Math.min(Math.max(x(hover) - 70, 0), w - 150), top: 8 }}>
          <span className="muted">{dateTime(hp[0], { dateStyle: "medium" })}</span>
          <b>{num(hp[1])}</b>
          <span className={hp[1] >= base ? "up" : "down"}>{signed(((hp[1] / base) - 1) * 100)}% in range</span>
          {emaLine?.[hover] != null && <span className="muted">EMA {num(emaLine[hover])}</span>}
        </div>
      )}
    </div>
  );
}

export function Sparkline({ values, height = 44 }) {
  const id = useId().replace(/:/g, "");
  if (!values || values.length < 2) return <svg height={height} />;
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const W = 120;
  const pts = values.map((v, i) => [(i * W) / (values.length - 1), height - 3 - ((v - lo) * (height - 6)) / (hi - lo || 1)]);
  const d = pts.map(([a, b], i) => `${i ? "L" : "M"}${a.toFixed(1)},${b.toFixed(1)}`).join("");
  const up = values[values.length - 1] >= values[0];
  return (
    <svg className={`spark ${up ? "up" : "down"}`} viewBox={`0 0 ${W} ${height}`} preserveAspectRatio="none" height={height} aria-hidden="true">
      <defs>
        <linearGradient id={`sp${id}`} x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%" stopColor="currentColor" stopOpacity="0.3" />
          <stop offset="100%" stopColor="currentColor" stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={`${d}L${W},${height}L0,${height}Z`} fill={`url(#sp${id})`} />
      <path d={d} fill="none" stroke="currentColor" strokeWidth="1.6" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

export const QUADRANT_TONE = { Leading: "up", Weakening: "amber", Lagging: "down", Improving: "accent" };

// Relative rotation graph: x = RS-Ratio, y = RS-Momentum, centred on 100; tails show the last weeks' path.
export function RotationMap({ sectors, selected, onSelect, tailLen = 3 }) {
  const [hover, setHover] = useState(null);
  const S = 520;
  const P = 34;
  const all = sectors.flatMap((s) => s.tail.flatMap((t) => [t.rs_ratio - 100, t.rs_momentum - 100]));
  const ext = Math.max(2.5, ...all.map(Math.abs)) * 1.12;
  const px = (v) => P + ((v - 100 + ext) / (2 * ext)) * (S - 2 * P);
  const py = (v) => S - P - ((v - 100 + ext) / (2 * ext)) * (S - 2 * P);
  const c = S / 2;
  const focus = hover || selected;

  return (
    <svg className="rrg" viewBox={`0 0 ${S} ${S}`} role="img" aria-label="Sector rotation map">
      <rect x={c} y={P} width={c - P} height={c - P} className="q q-leading" />
      <rect x={c} y={c} width={c - P} height={c - P} className="q q-weakening" />
      <rect x={P} y={c} width={c - P} height={c - P} className="q q-lagging" />
      <rect x={P} y={P} width={c - P} height={c - P} className="q q-improving" />
      <line x1={P} x2={S - P} y1={c} y2={c} className="axis-line" />
      <line x1={c} x2={c} y1={P} y2={S - P} className="axis-line" />
      <text x={S - P - 8} y={P + 20} textAnchor="end" className="q-label up">LEADING</text>
      <text x={S - P - 8} y={S - P - 10} textAnchor="end" className="q-label amber">WEAKENING</text>
      <text x={P + 8} y={S - P - 10} className="q-label down">LAGGING</text>
      <text x={P + 8} y={P + 20} className="q-label accent">IMPROVING</text>
      <text x={S - P} y={c - 8} textAnchor="end" className="axis">RS-Ratio →</text>
      <text x={c + 8} y={P - 10} className="axis">↑ RS-Momentum</text>
      {sectors.map((s) => {
        const tone = QUADRANT_TONE[s.quadrant];
        const dim = focus && focus !== s.sector;
        const tail = focus === s.sector ? s.tail : s.tail.slice(-tailLen); // full trail only for the focused group
        const head = tail[tail.length - 1];
        const d = tail.map((t, i) => `${i ? "L" : "M"}${px(t.rs_ratio).toFixed(1)},${py(t.rs_momentum).toFixed(1)}`).join("");
        return (
          <g
            key={s.sector}
            className={`sector ${tone}${dim ? " dim" : ""}${focus === s.sector ? " focus" : ""}`}
            onMouseEnter={() => setHover(s.sector)}
            onMouseLeave={() => setHover(null)}
            onClick={() => onSelect?.(selected === s.sector ? null : s.sector)}
          >
            <path d={d} className="tail" />
            {tail.slice(0, -1).map((t, i) => (
              <circle key={t.date} cx={px(t.rs_ratio)} cy={py(t.rs_momentum)} r={1.6 + (i / tail.length) * 1.6} className="tail-dot" />
            ))}
            <circle cx={px(head.rs_ratio)} cy={py(head.rs_momentum)} r="7" className="head" />
            <text x={px(head.rs_ratio) + 10} y={py(head.rs_momentum) + 4} className="sector-label">
              {{ LARGE: "Large cap", MID: "Mid cap", SMALL: "Small cap" }[s.sector] || shortSector(s.sector)}
            </text>
            <title>{`${s.sector}: ${s.quadrant} · RS-Ratio ${s.rs_ratio} · RS-Momentum ${s.rs_momentum} · 1M ${s.chg_1m_pct}% · vs Nifty 3M ${s.rel_3m_pct}%`}</title>
          </g>
        );
      })}
    </svg>
  );
}

// Stock page chart: one cyan line over candles [iso, o, h, l, c, v], OHLC tooltip, last-price pill.
// overlays: [{ values (one per candle, null allowed), cls, label }]; kind "candle" draws OHLC candles instead of the line.
export function PriceChart({ candles, intraday, height = 300, prevClose, overlays = [], kind = "line" }) {
  const [ref, w] = useWidth();
  const [hover, setHover] = useState(null);
  const id = useId().replace(/:/g, "");
  const pad = { l: 8, r: 84, t: 16, b: 28 };
  const n = candles?.length || 0;
  const geo = useMemo(() => {
    if (n < 2) return null;
    const closes = candles.map((c) => c[4]);
    const ref0 = intraday && prevClose ? [prevClose] : [];
    const ov = overlays.flatMap((o) => o.values.filter((v) => v != null));
    const wick = kind === "candle" ? candles.flatMap((c) => [c[2], c[3]]) : [];
    let lo = Math.min(...closes, ...ref0, ...ov, ...wick);
    let hi = Math.max(...closes, ...ref0, ...ov, ...wick);
    const pv = (hi - lo) * 0.1 || hi * 0.01;
    lo -= pv;
    hi += pv;
    const x = (i) => pad.l + (i * (w - pad.l - pad.r)) / (n - 1);
    const y = (v) => pad.t + ((hi - v) * (height - pad.t - pad.b)) / (hi - lo);
    const line = closes.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
    const area = `${line}L${x(n - 1).toFixed(1)},${height - pad.b}L${x(0).toFixed(1)},${height - pad.b}Z`;
    const ticks = Array.from({ length: 4 }, (_, k) => lo + ((k + 0.5) * (hi - lo)) / 4);
    const nx = Math.max(2, Math.min(6, n, Math.floor((w - pad.l - pad.r) / 72)));   // labels never collide
    const xt = Array.from({ length: nx }, (_, k) => Math.round((k * (n - 1)) / Math.max(1, nx - 1)));
    const ovPaths = overlays.map((o) => {
      let d = "";
      let pen = false;
      o.values.forEach((v, i) => {
        if (v == null) pen = false;
        else {
          d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
          pen = true;
        }
      });
      return { ...o, d };
    });
    return { x, y, line, area, ticks, xt, ovPaths };
  }, [candles, w, height, n, prevClose, intraday, overlays, kind]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!geo) return <div ref={ref} className="chart-empty" style={{ height }}>No candles for this range</div>;
  const { x, y, line, area, ticks, xt, ovPaths } = geo;
  const cw = Math.max(1, Math.min(9, ((w - pad.l - pad.r) / n) * 0.62));
  const label = (iso) => (intraday
    ? new Date(iso).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata" })
    : new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: n > 300 ? "2-digit" : undefined }));
  const last = candles[n - 1][4];
  const lastTxt = num(last, last > 1000 ? 0 : last < 200 ? 4 : 2);
  const pillW = Math.max(56, lastTxt.length * 6.8 + 12);
  const hc = hover !== null ? candles[hover] : null;
  const onMove = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    setHover(Math.max(0, Math.min(n - 1, Math.round(((e.clientX - r.left - pad.l) / (w - pad.l - pad.r)) * (n - 1)))));
  };
  return (
    <div ref={ref} className="chart price-chart" style={{ height }}>
      <svg width={w} height={height} onMouseMove={onMove} onMouseLeave={() => setHover(null)} role="img" aria-label="Price chart">
        <defs>
          <linearGradient id={`pc${id}`} x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor="var(--accent)" stopOpacity="0.22" />
            <stop offset="100%" stopColor="var(--accent)" stopOpacity="0" />
          </linearGradient>
          <pattern id={`dots${id}`} width="14" height="14" patternUnits="userSpaceOnUse">
            <circle cx="1" cy="1" r="0.8" className="dot-grid" />
          </pattern>
        </defs>
        <rect x={pad.l} y={pad.t} width={w - pad.l - pad.r} height={height - pad.t - pad.b} fill={`url(#dots${id})`} />
        {ticks.map((t) => (
          <text key={t} x={w - pad.r + 10} y={y(t) + 4} className="axis">{num(t, t > 1000 ? 0 : 2)}</text>
        ))}
        {xt.map((i) => (
          <text key={i} x={x(i)} y={height - 8} className="axis" textAnchor={i === 0 ? "start" : i === n - 1 ? "end" : "middle"}>{label(candles[i][0])}</text>
        ))}
        {intraday && prevClose && (
          <line x1={pad.l} x2={w - pad.r} y1={y(prevClose)} y2={y(prevClose)} className="baseline" />
        )}
        {kind === "candle" ? candles.map((c, i) => (
          <g key={c[0]} className={c[4] >= c[1] ? "cdl up" : "cdl down"}>
            <line x1={x(i)} x2={x(i)} y1={y(c[2])} y2={y(c[3])} />
            <rect x={x(i) - cw / 2} width={cw} y={y(Math.max(c[1], c[4]))} height={Math.max(1, Math.abs(y(c[1]) - y(c[4])))} />
          </g>
        )) : (
          <>
            <path d={area} fill={`url(#pc${id})`} />
            <path d={line} className="line accent-line" />
          </>
        )}
        {ovPaths.map((o) => <path key={o.label} d={o.d} className={`ov-line ${o.cls}`} />)}
        <g transform={`translate(${w - pad.r + 4},${y(last) - 10})`}>
          <rect width={pillW} height="20" rx="6" className="last-pill" />
          <text x={pillW / 2} y="14" textAnchor="middle" className="last-pill-text">{lastTxt}</text>
        </g>
        {hc && (
          <g>
            <line x1={x(hover)} x2={x(hover)} y1={pad.t} y2={height - pad.b} className="cross" />
            <circle cx={x(hover)} cy={y(hc[4])} r="4.5" className="dot accent" />
          </g>
        )}
      </svg>
      {hc && (
        <div className="chart-tip ohlc-tip" style={{ left: Math.min(Math.max(x(hover) + 12, 0), w - 170), top: 10 }}>
          <span className="muted">{dateTime(hc[0], intraday ? { dateStyle: "medium", timeStyle: "short" } : { dateStyle: "medium" })}</span>
          <dl>
            <dt>Open</dt><dd>{num(hc[1])}</dd>
            <dt>High</dt><dd>{num(hc[2])}</dd>
            <dt>Low</dt><dd>{num(hc[3])}</dd>
            <dt>Close</dt><dd>{num(hc[4])}</dd>
            {hc[5] > 0 && <><dt>Volume</dt><dd>{hc[5].toLocaleString("en-IN")}</dd></>}
          </dl>
        </div>
      )}
    </div>
  );
}
