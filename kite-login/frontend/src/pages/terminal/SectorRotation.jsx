import { useMemo, useState } from "react";
import { api } from "../../lib/api.js";
import { num, signed, tone } from "../../lib/format.js";
import { Empty, ErrorNote, Segmented, Spinner, useLoad } from "../../components/ui.jsx";

// Sector and theme rotation by RSI. Each NSE sector or thematic index sits on a map: across, its daily RSI(14) on the live
// price; up, how much that RSI changed over the last 5 sessions. The four corners are the rotation's phases.
// Read top-down: the four phases with who is in each, then the map beside the ranked list (hovering either highlights both).
const QUAD = {
  Leading: { cls: "lead", text: "RSI above 50 and rising: strength building" },
  Weakening: { cls: "weak", text: "RSI above 50 but falling: strength fading" },
  Improving: { cls: "impr", text: "RSI below 50 but rising: an early turn" },
  Lagging: { cls: "lag", text: "RSI below 50 and falling: weak and getting weaker" },
};
const ORDER = ["Leading", "Weakening", "Improving", "Lagging"];
const W = 640, H = 440, PAD = { l: 40, r: 14, t: 14, b: 34 };

// Places each label beside its point, nudging it down or up when it would overlap one already placed; a label with no room is
// left off (its name still shows on hover and in the list).
function placeLabels(points) {
  const placed = [];
  const out = {};
  for (const p of [...points].sort((a, b) => a.y - b.y)) {
    const w = p.label.length * 6.4 + 4, h = 13;
    const right = p.x + 8 + w < W - PAD.r;
    const x0 = right ? p.x + 8 : p.x - 8 - w;
    let spot = null;
    for (const dy of [0, 12, -12, 22, -22]) {
      const y0 = p.y - 9 + dy;
      if (y0 < PAD.t || y0 + h > H - PAD.b) continue;
      if (!placed.some((b) => x0 < b.x + b.w && x0 + w > b.x && y0 < b.y + b.h && y0 + h > b.y)) { spot = { x: x0, y: y0, w, h }; break; }
    }
    if (spot) {
      placed.push(spot);
      out[p.key] = { x: right ? spot.x : spot.x + w, y: spot.y + 10, anchor: right ? "start" : "end" };
    }
  }
  return out;
}

function RotationMap({ rows, focus, setFocus }) {
  const xs = rows.flatMap((r) => r.tail.map((p) => p[0]));
  const ys = rows.flatMap((r) => r.tail.map((p) => p[1]));
  const xLo = Math.min(30, Math.floor(Math.min(...xs) / 5) * 5), xHi = Math.max(80, Math.ceil(Math.max(...xs) / 5) * 5);
  const yAbs = Math.max(10, Math.ceil(Math.max(...ys.map(Math.abs)) / 5) * 5);
  const x = (v) => PAD.l + ((v - xLo) / (xHi - xLo)) * (W - PAD.l - PAD.r);
  const y = (v) => PAD.t + ((yAbs - v) / (2 * yAbs)) * (H - PAD.t - PAD.b);
  const ticksX = []; for (let v = xLo; v <= xHi; v += 10) ticksX.push(v);
  const ticksY = [-yAbs, -yAbs / 2, 0, yAbs / 2, yAbs];
  const labels = useMemo(() => placeLabels(rows.map((r) => { const [a, b] = r.tail[r.tail.length - 1]; return { key: r.key, label: r.label, x: x(a), y: y(b) }; })),
    [rows, xLo, xHi, yAbs]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <svg className="rot-map" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Rotation map: RSI across, its five-session change up">
      <rect x={x(50)} y={y(yAbs)} width={x(xHi) - x(50)} height={y(0) - y(yAbs)} className="rot-q lead" />
      <rect x={x(50)} y={y(0)} width={x(xHi) - x(50)} height={y(-yAbs) - y(0)} className="rot-q weak" />
      <rect x={x(xLo)} y={y(0)} width={x(50) - x(xLo)} height={y(-yAbs) - y(0)} className="rot-q lag" />
      <rect x={x(xLo)} y={y(yAbs)} width={x(50) - x(xLo)} height={y(0) - y(yAbs)} className="rot-q impr" />
      {[30, 70].filter((v) => v > xLo && v < xHi).map((v) => <line key={v} x1={x(v)} x2={x(v)} y1={PAD.t} y2={H - PAD.b} className="rot-band" />)}
      <line x1={x(50)} x2={x(50)} y1={PAD.t} y2={H - PAD.b} className="rot-axis" />
      <line x1={PAD.l} x2={W - PAD.r} y1={y(0)} y2={y(0)} className="rot-axis" />
      {ticksX.map((v) => <text key={v} x={x(v)} y={H - 16} className="rot-tick" textAnchor="middle">{v}</text>)}
      {ticksY.map((v) => <text key={v} x={PAD.l - 6} y={y(v) + 4} className="rot-tick" textAnchor="end">{v > 0 ? `+${v}` : v}</text>)}
      <text x={W - PAD.r - 6} y={PAD.t + 14} className="rot-qlabel lead" textAnchor="end">LEADING</text>
      <text x={W - PAD.r - 6} y={H - PAD.b - 8} className="rot-qlabel weak" textAnchor="end">WEAKENING</text>
      <text x={PAD.l + 6} y={H - PAD.b - 8} className="rot-qlabel lag">LAGGING</text>
      <text x={PAD.l + 6} y={PAD.t + 14} className="rot-qlabel impr">IMPROVING</text>
      <text x={(W + PAD.l) / 2} y={H - 2} className="rot-tick" textAnchor="middle">RSI(14), daily →</text>
      {rows.map((r) => {
        const on = focus === r.key, dim = focus && !on;
        const [cx, cy] = r.tail[r.tail.length - 1];
        const lab = labels[r.key];
        return (
          <g key={r.key} className={`rot-pt ${QUAD[r.quadrant]?.cls || ""}${on ? " on" : ""}${dim ? " dim" : ""}`} onMouseEnter={() => setFocus(r.key)} onMouseLeave={() => setFocus(null)}>
            <polyline points={r.tail.map(([a, b]) => `${x(a)},${y(b)}`).join(" ")} className="rot-tail" />
            <circle cx={x(cx)} cy={y(cy)} r={on ? 6 : 4.5} />
            {(lab || on) && <text x={lab ? lab.x : x(cx) + 8} y={lab ? lab.y : y(cy) + 4} textAnchor={lab ? lab.anchor : "start"} className="rot-name">{r.label}</text>}
            <title>{`${r.label}: RSI ${num(r.rsi, 1)}, ${signed(r.rsi_change, 1)} in 5 sessions · ${r.quadrant}`}</title>
          </g>
        );
      })}
    </svg>
  );
}

export default function SectorRotation({ openStock }) {
  const [kind, setKind] = useState("sectors");
  const [focus, setFocus] = useState(null);
  const [phase, setPhase] = useState(null);
  const q = useLoad(() => api.terminalRotation(kind), [kind], 30_000);
  const d = q.data?.kind === kind ? q.data : null;          // never show the other universe's data while this one loads
  const rows = useMemo(() => d?.rows || [], [d]);
  const list = phase ? rows.filter((r) => r.quadrant === phase) : rows;
  const switchKind = (v) => { setKind(v); setFocus(null); setPhase(null); };
  return (
    <div className="tt-page rot-page">
      <div className="rot-top">
        <Segmented label="Universe" value={kind} onChange={switchKind} options={[{ id: "sectors", label: "Sectors" }, { id: "themes", label: "Themes" }]} />
        <span className="muted small">{d ? `${rows.length} indices · RSI on today's live price · as of ${new Date(d.asof).toLocaleTimeString("en-IN", { timeStyle: "short" })}` : ""}</span>
      </div>

      <section className="rot-phases">
        {ORDER.map((k) => {
          const inIt = rows.filter((r) => r.quadrant === k);
          return (
            <button key={k} type="button" className={`card rot-phase-card ${QUAD[k].cls}${phase === k ? " on" : ""}`} aria-pressed={phase === k} onClick={() => setPhase(phase === k ? null : k)}>
              <span className="rot-phase-top"><b>{k}</b><em>{d ? inIt.length : "–"}</em></span>
              <small>{QUAD[k].text}</small>
              <span className="rot-phase-names">{inIt.slice(0, 6).map((r) => <i key={r.key}>{r.label}</i>)}{inIt.length > 6 && <i className="more">+{inIt.length - 6}</i>}{d && !inIt.length && <span className="muted">none right now</span>}</span>
            </button>
          );
        })}
      </section>

      <div className="rot-main">
        <section className="card rot-card">
          <header className="card-head"><h3>Rotation map</h3><span className="muted small">across: RSI · up: its change over {d?.lookback ?? 5} sessions · tails: the path</span></header>
          {!d && !q.error ? <div className="rot-loading"><Spinner /> <span className="muted small">Reading a year of daily candles for each index…</span></div>
            : rows.length ? <RotationMap rows={phase ? list : rows} focus={focus} setFocus={setFocus} /> : <Empty>No index data yet. It needs a Kite session.</Empty>}
          <p className="muted small rot-note">Above 70 is overbought and below 30 oversold (dashed lines). Indices usually move round the map clockwise: improving, leading, weakening, lagging.</p>
        </section>

        <section className="card rot-rank">
          <header className="card-head"><h3>{phase || "All phases"}</h3><span className="muted small">ranked by RSI{phase && <> · <button type="button" className="link-btn small" onClick={() => setPhase(null)}>show all</button></>}</span></header>
          <div className="rot-cols" aria-hidden="true"><span>{kind === "sectors" ? "Sector" : "Theme"}</span><span>RSI</span><span>5d Δ</span><span>1W</span><span>1M</span></div>
          <ul className="rot-list">
            {list.map((r) => (
              <li key={r.key} className={`${focus === r.key ? "on" : ""}`} onMouseEnter={() => setFocus(r.key)} onMouseLeave={() => setFocus(null)}>
                <button type="button" className="rot-li" onClick={() => openStock?.(r.key.split(":")[1], "NSE")} title={`Open ${r.label}`}>
                  <span className="rot-li-name"><i className={`rot-dot ${QUAD[r.quadrant]?.cls}`} /><b>{r.label}</b>{r.zone && <em className={r.zone === "Overbought" ? "ob" : "os"}>{r.zone === "Overbought" ? "OB" : "OS"}</em>}</span>
                  <span className="rot-li-rsi"><span className="rot-gauge"><span style={{ left: `${Math.max(0, Math.min(100, r.rsi))}%` }} /></span><b>{num(r.rsi, 0)}</b></span>
                  <span className={tone(r.rsi_change)}>{signed(r.rsi_change, 1)}</span>
                  <span className={tone(r.ret_1w)}>{r.ret_1w == null ? "–" : `${signed(r.ret_1w, 1)}%`}</span>
                  <span className={tone(r.ret_1m)}>{r.ret_1m == null ? "–" : `${signed(r.ret_1m, 1)}%`}</span>
                </button>
              </li>
            ))}
          </ul>
          {d?.missing?.length > 0 && <p className="muted small rot-note">No history yet: {d.missing.join(", ")}.</p>}
        </section>
      </div>
      <ErrorNote error={q.error} />
    </div>
  );
}
