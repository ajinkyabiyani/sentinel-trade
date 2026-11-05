import { useMemo, useState } from "react";
import { INDUSTRY_OF, THEMES, THEMES_OF } from "../lib/classify.js";
import { num, shortSector, signed, tone } from "../lib/format.js";

// "Strongest" groups at three levels (sector -> industry -> theme), ranked by a strength score:
//   score = 60% median RS rating + 20% members above the 50 DMA + 20% above the 200 DMA,
// with small groups pulled toward neutral (50) so one hot stock cannot top the list. Rank arrows compare with the
// same score a week earlier (RS from a week ago). Quadrant: strong (score >= 50) / weak x RS rising / falling,
// or the weekly RRG quadrant for sectors when the scan has it.
const LEVELS = [
  { id: "sector", label: "Sector" },
  { id: "industry", label: "Industry" },
  { id: "theme", label: "Theme" },
];
const QUADS = ["Leading", "Improving", "Weakening", "Lagging"];
const SHRINK = 3;     // pseudo-members at score 50
const SMALL = 5;

const median = (xs) => {
  const v = xs.filter((x) => Number.isFinite(x)).sort((a, b) => a - b);
  if (!v.length) return null;
  const m = Math.floor(v.length / 2);
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
};
const pctTrue = (ms, k) => {
  const v = ms.filter((m) => m[k] !== null && m[k] !== undefined);
  return v.length ? (v.filter((m) => m[k]).length / v.length) * 100 : null;
};

function score(rs, a50, a200, n) {
  const parts = [[0.6, rs], [0.2, a50], [0.2, a200]].filter(([, v]) => v !== null && v !== undefined);
  if (!parts.length) return null;
  const raw = parts.reduce((a, [w, v]) => a + w * v, 0) / parts.reduce((a, [w]) => a + w, 0);
  return (raw * n + 50 * SHRINK) / (n + SHRINK);
}

export function buildGroups(stocks, level, rrg = {}) {
  const groups = {};
  for (const s of stocks) {
    const keys = level === "sector" ? [s.sector] : level === "industry" ? [INDUSTRY_OF[s.symbol] || s.sector] : THEMES_OF[s.symbol] || [];
    for (const k of keys) if (k) (groups[k] ||= []).push(s);
  }
  const rows = Object.entries(groups).map(([name, ms]) => {
    const rs = median(ms.map((m) => m.rs_rating));
    const prs = median(ms.map((m) => m.prev_rs_rating ?? m.rs_rating));
    const a50 = pctTrue(ms, "above_50dma");
    const a200 = pctTrue(ms, "above_200dma");
    const sc = score(rs, a50, a200, ms.length);
    const psc = score(prs, a50, a200, ms.length);
    const up = ms.filter((m) => m.chg_1d > 0).length;
    const down = ms.filter((m) => m.chg_1d < 0).length;
    const mean = (k) => ms.reduce((a, m) => a + (m[k] ?? 0), 0) / ms.length;
    const quadrant = (level === "sector" && rrg[name]) || (sc >= 50 ? (rs >= prs ? "Leading" : "Weakening") : (rs >= prs ? "Improving" : "Lagging"));
    return {
      name, members: [...ms].sort((a, b) => (b.rs_rating ?? 0) - (a.rs_rating ?? 0)), n: ms.length, rs, rsChg: rs != null && prs != null ? rs - prs : null,
      a50, a200, n50: ms.filter((m) => m.above_50dma).length, score: sc, prevScore: psc, up, down, quadrant,
      r1w: mean("chg_1w"), r1m: mean("chg_1m"), r3m: mean("chg_3m"), small: ms.length < SMALL,
      about: level === "theme" ? THEMES[name]?.[0] : null,
    };
  });
  const rankBy = (k) => [...rows].sort((a, b) => (b[k] ?? -1) - (a[k] ?? -1)).forEach((r, i) => { r[`${k}Rank`] = i + 1; });
  rankBy("score");
  rankBy("prevScore");
  return rows.sort((a, b) => a.scoreRank - b.scoreRank).map((r) => ({ ...r, rankChg: r.prevScoreRank - r.scoreRank }));
}

const Delta = ({ v, d = 0 }) => (v == null || Math.abs(v) < 0.05 ? <small className="gc-d flat">0</small>
  : <small className={`gc-d ${v > 0 ? "up" : "down"}`}>{v > 0 ? "▲" : "▼"}{num(Math.abs(v), d)}</small>);

function Card({ g, level, openStock }) {
  const [open, setOpen] = useState(null);
  return (
    <article className={`gc q-${g.quadrant.toLowerCase()}`}>
      <header>
        <span className="gc-rank">#{g.scoreRank}</span> <Delta v={g.rankChg} /> <b>{level === "sector" ? shortSector(g.name) : g.name}</b>
        {g.small && <small className="muted"> · small group</small>}
      </header>
      {g.about && <p className="gc-about">{g.about}</p>}
      <div className="gc-bar"><i style={{ width: `${Math.max(2, Math.min(100, g.score ?? 0))}%` }} /></div>
      <div className="gc-stats">
        <span><small>Score</small><b>{g.score == null ? "–" : Math.round(g.score)}</b></span>
        <span><small>Median RS</small><b>{g.rs == null ? "–" : num(g.rs, g.rs % 1 ? 1 : 0)} <Delta v={g.rsChg} d={g.rsChg % 1 ? 1 : 0} /></b></span>
        <span><small>Adv / Dec</small><b><span className="up">{g.up}</span> / <span className="down">{g.down}</span></b><em>{g.n50} / {g.n} above 50D</em></span>
      </div>
      <div className="gc-chips">
        {g.members.slice(0, 3).map((m) => (
          <button key={m.symbol} type="button" className="gc-chip" onClick={() => openStock(m.symbol)} title={m.name}>{m.symbol} <b>{m.rs_rating}</b></button>
        ))}
        <span className="gc-links">
          <button type="button" className="linkish small" onClick={() => setOpen(open === "stocks" ? null : "stocks")} aria-expanded={open === "stocks"}>Stocks {open === "stocks" ? "▾" : "▸"}</button>
          <button type="button" className="linkish small" onClick={() => setOpen(open === "details" ? null : "details")} aria-expanded={open === "details"}>Details {open === "details" ? "▴" : "▾"}</button>
        </span>
      </div>
      {open === "stocks" && (
        <table className="gc-list">
          <tbody>
            {g.members.map((m) => (
              <tr key={m.symbol} onClick={() => openStock(m.symbol)}>
                <td><b>{m.symbol}</b></td>
                <td className="num">RS {m.rs_rating ?? "–"}</td>
                <td className={`num ${tone(m.chg_1d)}`}>{signed(m.chg_1d, 1)}%</td>
                <td className={`num ${tone(m.chg_1m)}`}>{signed(m.chg_1m, 1)}%<small> 1M</small></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {open === "details" && (
        <dl className="gc-details">
          <dt>1W / 1M / 3M</dt>
          <dd><span className={tone(g.r1w)}>{signed(g.r1w, 1)}%</span> · <span className={tone(g.r1m)}>{signed(g.r1m, 1)}%</span> · <span className={tone(g.r3m)}>{signed(g.r3m, 1)}%</span></dd>
          <dt>Above 50 / 200 DMA</dt>
          <dd>{g.a50 == null ? "–" : `${Math.round(g.a50)}%`} / {g.a200 == null ? "after next scan" : `${Math.round(g.a200)}%`}</dd>
          <dt>Quadrant</dt><dd>{g.quadrant}</dd>
          <dt>Members</dt><dd>{g.n}</dd>
        </dl>
      )}
    </article>
  );
}

export default function GroupCards({ stocks, sectors, openStock }) {
  const [level, setLevel] = useState("sector");
  const [quads, setQuads] = useState(new Set());
  const rrg = useMemo(() => Object.fromEntries((sectors || []).map((s) => [s.sector, s.quadrant])), [sectors]);
  const groups = useMemo(() => buildGroups(stocks, level, rrg), [stocks, level, rrg]);
  const shown = quads.size ? groups.filter((g) => quads.has(g.quadrant)) : groups;
  const count = (q) => groups.filter((g) => g.quadrant === q).length;
  const toggle = (q) => setQuads((s) => {
    const n = new Set(s);
    if (n.has(q)) n.delete(q);
    else n.add(q);
    return n;
  });
  const has200 = stocks.some((s) => s.above_200dma !== undefined);

  return (
    <section className="gcards">
      <header className="gc-head">
        <h2>Strongest</h2>
        <select value={level} onChange={(e) => setLevel(e.target.value)} aria-label="Grouping level">
          {LEVELS.map((l) => <option key={l.id} value={l.id}>{l.label} ({buildGroups(stocks, l.id, rrg).length})</option>)}
        </select>
        <label className={`gc-q${quads.size === 0 ? " on" : ""}`}><input type="checkbox" checked={quads.size === 0} onChange={() => setQuads(new Set())} /> All</label>
        {QUADS.map((q) => (
          <label key={q} className={`gc-q q-${q.toLowerCase()}${quads.has(q) ? " on" : ""}`}>
            <input type="checkbox" checked={quads.has(q)} onChange={() => toggle(q)} /> {q} ({count(q)})
          </label>
        ))}
      </header>
      <p className="muted small gc-note">
        {level === "sector" ? "NSE sectors" : level === "industry" ? "Industries at a finer level (e.g. Cables & Wires, Stockbroking)" : "Investment themes; a stock can belong to several"} across
        the {stocks.length} scanned stocks. Score = 60% median RS rating + 20% members above 50 DMA + 20% above 200 DMA{has200 ? "" : " (200 DMA after the next scan)"},
        with small groups pulled toward neutral. Arrows: change versus a week ago. Click a stock chip to open it.
      </p>
      <div className="gc-grid">
        {shown.map((g) => <Card key={g.name} g={g} level={level} openStock={openStock} />)}
      </div>
    </section>
  );
}
