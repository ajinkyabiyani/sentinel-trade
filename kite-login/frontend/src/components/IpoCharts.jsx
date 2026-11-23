import { useState } from "react";
import { num, signed } from "../lib/format.js";

// Small charts for the IPO Agent: odds ring, listing-gain range strip, columns by band, GMP-vs-listing scatter, the
// recently-listed dumbbells and the financials bars. Plain SVG on the theme tokens.
const toneOf = (pct) => (pct >= 75 ? "good" : pct >= 55 ? "warn" : "bad");

export function OddsRing({ pct, size = 92 }) {
  const R = 38, C = 2 * Math.PI * R;
  const v = Math.max(0, Math.min(100, pct ?? 0));
  return (
    <svg className={`ic-ring ${pct == null ? "" : toneOf(pct)}`} viewBox="0 0 100 100" width={size} height={size} role="img" aria-label={`${num(pct, 0)}% listed above price`}>
      <circle cx="50" cy="50" r={R} className="ic-ring-bg" />
      <circle cx="50" cy="50" r={R} className="ic-ring-fg" strokeDasharray={`${(v / 100) * C} ${C}`} transform="rotate(-90 50 50)" />
      <text x="50" y="49" textAnchor="middle" className="ic-ring-v">{pct == null ? "–" : `${num(pct, 0)}%`}</text>
      <text x="50" y="64" textAnchor="middle" className="ic-ring-l">listed up</text>
    </svg>
  );
}

// Worst 10%, median and best 10% listing gains on one axis, with zero marked.
export function GainStrip({ p10, med, p90 }) {
  if (p10 == null || p90 == null) return null;
  const lo = Math.min(p10, 0), hi = Math.max(p90, 0);
  const pad = Math.max(2, (hi - lo) * 0.08);
  const a = lo - pad, b = hi + pad;
  const x = (v) => `${((v - a) / (b - a)) * 100}%`;
  return (
    <div className="ic-strip" title={`Worst 10% ${signed(p10, 1)}%, median ${signed(med, 1)}%, best 10% ${signed(p90, 1)}%`}>
      <div className="ic-strip-track">
        <span className="ic-strip-zero" style={{ left: x(0) }} />
        <span className="ic-strip-band" style={{ left: x(p10), width: `calc(${x(p90)} - ${x(p10)})` }} />
        <span className={`ic-strip-med ${med >= 0 ? "up" : "down"}`} style={{ left: x(med) }} />
      </div>
      <div className="ic-strip-labels">
        <span style={{ left: x(p10) }} className={p10 >= 0 ? "up" : "down"}>{signed(p10, 0)}%</span>
        <span style={{ left: x(med) }} className={`med ${med >= 0 ? "up" : "down"}`}>{signed(med, 0)}%</span>
        <span style={{ left: x(p90) }} className={p90 >= 0 ? "up" : "down"}>{signed(p90, 0)}%</span>
      </div>
      <div className="ic-strip-key"><span>worst 10%</span><span>median</span><span>best 10%</span></div>
    </div>
  );
}

// One column per band: height is the share that listed above price; the median gain and count sit underneath.
export function BandColumns({ rows, title }) {
  const H = 120;
  return (
    <div className="ic-cols">
      <h4>{title}</h4>
      <div className="ic-cols-plot">
        {[25, 50, 75].map((g) => <span key={g} className="ic-grid" style={{ bottom: `${(g / 100) * H}px` }}><em>{g}%</em></span>)}
        {rows.map((r) => (
          <div key={r.band} className="ic-col" title={`${r.band}: ${r.n ? `${num(r.win_pct, 0)}% of ${r.n} listed above price, median ${signed(r.median_gain, 1)}%` : "no issues"}`}>
            <b>{r.n ? `${num(r.win_pct, 0)}%` : "–"}</b>
            <span className="ic-col-bar" style={{ height: `${((r.win_pct ?? 0) / 100) * H}px` }}><span className={r.n ? toneOf(r.win_pct) : ""} /></span>
            <small>{r.band}</small>
            <em className={r.median_gain >= 0 ? "up" : "down"}>{r.n ? `${signed(r.median_gain, 0)}%` : ""}</em>
            <i>{r.n} IPOs</i>
          </div>
        ))}
      </div>
    </div>
  );
}

// Every IPO of the last 12 months: GMP the day before listing (across) against the actual listing gain (up).
export function GmpScatter({ points, board }) {
  const [hover, setHover] = useState(null);
  const pts = (points || []).filter((p) => (board === "SME" ? p[2] === "S" : p[2] === "M"));
  if (!pts.length) return null;
  const W = 560, H = 260, P = { l: 40, r: 12, t: 12, b: 32 };
  const xMax = Math.min(150, Math.max(30, ...pts.map((p) => p[0]))), xMin = Math.max(-10, Math.min(0, ...pts.map((p) => p[0])));
  const yMax = Math.min(160, Math.max(30, ...pts.map((p) => p[1]))), yMin = Math.max(-50, Math.min(-10, ...pts.map((p) => p[1])));
  const X = (v) => P.l + ((Math.max(xMin, Math.min(xMax, v)) - xMin) / (xMax - xMin)) * (W - P.l - P.r);
  const Y = (v) => P.t + (1 - (Math.max(yMin, Math.min(yMax, v)) - yMin) / (yMax - yMin)) * (H - P.t - P.b);
  const up = pts.filter((p) => p[1] > 0).length;
  const tx = [0, 25, 50, 75, 100, 125, 150].filter((v) => v >= xMin && v <= xMax);
  const ty = [-40, -20, 0, 25, 50, 100, 150].filter((v) => v >= yMin && v <= yMax);
  return (
    <div className="ic-scatter">
      <div className="ic-scatter-head">
        <h4>GMP against the actual listing · last 12 months</h4>
        <span className="muted small">{pts.length} {board} IPOs · {Math.round((up / pts.length) * 100)}% listed above price · dots on the dashed line listed exactly at their GMP</span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="GMP against listing gain">
        {ty.map((v) => <g key={`y${v}`}><line x1={P.l} x2={W - P.r} y1={Y(v)} y2={Y(v)} className={v === 0 ? "ic-axis0" : "ic-gridline"} /><text x={P.l - 6} y={Y(v) + 3} textAnchor="end" className="ic-tick">{v}%</text></g>)}
        {tx.map((v) => <g key={`x${v}`}><line x1={X(v)} x2={X(v)} y1={P.t} y2={H - P.b} className={v === 0 ? "ic-axis0" : "ic-gridline"} /><text x={X(v)} y={H - P.b + 14} textAnchor="middle" className="ic-tick">{v}%</text></g>)}
        <line x1={X(Math.max(xMin, yMin))} y1={Y(Math.max(xMin, yMin))} x2={X(Math.min(xMax, yMax))} y2={Y(Math.min(xMax, yMax))} className="ic-diag" />
        {pts.map((p, i) => <circle key={i} cx={X(p[0])} cy={Y(p[1])} r={hover === i ? 5 : 3.2} className={`ic-dot ${p[1] > 0 ? "up" : p[1] < 0 ? "down" : ""}`} onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)}><title>{`GMP ${signed(p[0], 1)}% → listed ${signed(p[1], 1)}%`}</title></circle>)}
        <text x={W - P.r} y={H - 4} textAnchor="end" className="ic-axis-l">GMP the day before listing →</text>
        <text x={P.l + 4} y={P.t + 10} className="ic-axis-l">↑ listing gain</text>
      </svg>
    </div>
  );
}

// Recently listed: the GMP (hollow) and what it actually listed at (filled), on one shared axis.
export function Dumbbells({ rows }) {
  const list = rows.filter((r) => r.listing_gain != null || r.final_gmp_pct != null);
  if (!list.length) return null;
  const vals = list.flatMap((r) => [r.final_gmp_pct ?? 0, r.listing_gain ?? 0]);
  const lo = Math.min(-10, ...vals), hi = Math.max(20, ...vals);
  const x = (v) => `${((v - lo) / (hi - lo)) * 100}%`;
  return (
    <div className="ic-db">
      <div className="ic-db-key"><span><i className="gmp" /> GMP</span><span><i className="act" /> Listed at</span></div>
      {list.map((r) => {
        const g = r.final_gmp_pct, l = r.listing_gain;
        return (
          <div key={r.id} className="ic-db-row" title={`${r.name}: GMP ${g == null ? "–" : `${signed(g, 1)}%`}, listed ${l == null ? "–" : `${signed(l, 1)}%`}`}>
            <span className="ic-db-name"><b>{r.name}</b><small>{r.board}</small></span>
            <span className="ic-db-track">
              <span className="ic-db-zero" style={{ left: x(0) }} />
              {g != null && l != null && <span className={`ic-db-line ${l >= g ? "beat" : "miss"}`} style={{ left: x(Math.min(g, l)), width: `calc(${x(Math.max(g, l))} - ${x(Math.min(g, l))})` }} />}
              {g != null && <i className="gmp" style={{ left: x(g) }} />}
              {l != null && <i className={`act ${l >= 0 ? "up" : "down"}`} style={{ left: x(l) }} />}
            </span>
            <b className={l == null ? "muted" : l >= 0 ? "up" : "down"} title={l == null ? r.status_label : undefined}>{l == null ? ({ LP: "Up", LN: "Down" })[r.status] || "–" : `${signed(l, 1)}%`}</b>
          </div>
        );
      })}
    </div>
  );
}

// Revenue and profit by financial year as paired bars.
export function FinBars({ fin }) {
  if (!fin) return null;
  const rev = fin.rows["Revenue"] || fin.rows["Total Income"];
  const pat = fin.rows["Profit After Tax"];
  if (!rev) return null;
  const idx = fin.periods.map((p, i) => i).filter((i) => rev[i] != null).reverse();
  const max = Math.max(1, ...idx.map((i) => Math.abs(rev[i])));
  const H = 110;
  return (
    <div className="ic-fin">
      <div className="ic-fin-key"><span><i className="rev" /> {fin.rows["Revenue"] ? "Revenue" : "Total income"}</span><span><i className="pat" /> Profit after tax</span><em>{fin.unit}</em></div>
      <div className="ic-fin-plot">
        {idx.map((i) => (
          <div key={fin.periods[i]} className="ic-fin-col" title={`${fin.periods[i]}: revenue ${num(rev[i], 0)}, profit ${pat?.[i] == null ? "–" : num(pat[i], 0)}`}>
            <div className="ic-fin-bars">
              <span className="rev" style={{ height: `${(Math.abs(rev[i]) / max) * H}px` }}><em>{num(rev[i], 0)}</em></span>
              {pat?.[i] != null && <span className={`pat ${pat[i] < 0 ? "neg" : ""}`} style={{ height: `${Math.max(2, (Math.abs(pat[i]) / max) * H)}px` }}><em>{num(pat[i], 0)}</em></span>}
            </div>
            <small>{fin.periods[i].replace(/^\d+ (\w{3}) (\d{4})$/, "$1 $2")}</small>
          </div>
        ))}
      </div>
    </div>
  );
}
