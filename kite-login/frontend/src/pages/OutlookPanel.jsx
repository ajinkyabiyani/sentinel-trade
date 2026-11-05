import { fmtK } from "../lib/outlook.js";
import { num, signed } from "../lib/format.js";
import { Badge } from "../components/ui.jsx";

const pct = (p) => (p === null || p === undefined ? "–" : `${Math.round(p * 100)}%`);
const STATE = {
  strengthening: "writers adding: wall getting stronger",
  weakening: "writers covering: wall giving way",
  attacked: "option buyers piling in: wall under pressure",
  easing: "option buyers leaving: pressure easing",
  unchanged: "no change",
  unknown: "loading OI change…",
};

function Wall({ w, isRes }) {
  if (!w) return null;
  return (
    <div className={`wall ${isRes ? "res" : "sup"}`}>
      <span className="eyebrow">{isRes ? "Resistance · call OI wall" : "Support · put OI wall"}</span>
      <div className="wall-top">
        <b>{num(w.strike, 0)}</b>
        <Badge tone={w.tone === "up" ? "buy" : w.tone === "down" ? "sell" : w.tone === "amber" ? "amber" : ""}>{w.verdict}</Badge>
      </div>
      <dl className="facts">
        <dt>Open interest</dt>
        <dd>
          {fmtK(w.oi || 0)} {w.dOi !== null && <span className="muted">({w.dOi > 0 ? "+" : ""}{fmtK(w.dOi)} today{w.dIv !== null ? `, IV ${signed(w.dIv, 1)}` : ""})</span>}
        </dd>
        <dt>Writers</dt>
        <dd>{STATE[w.state]}</dd>
        <dt>Odds by expiry</dt>
        <dd>touch {pct(w.pTouch)} · close {isRes ? "above" : "below"} {pct(w.pClose)}</dd>
        <dt>Next wall</dt>
        <dd>{w.next ? num(w.next, 0) : "–"}</dd>
      </dl>
    </div>
  );
}

// what a wall's writers are doing means for price: e.g. call writers covering above spot is bullish
const LEAN = {
  res: { strengthening: "down", easing: "down", weakening: "up", attacked: "up" },
  sup: { strengthening: "up", easing: "up", weakening: "down", attacked: "down" },
};

// R3..R1, spot, S1..S3 from the biggest OI walls near the money
export function Levels({ o, lc }) {
  const { resistance, support } = o.levels;
  if (!resistance.length && !support.length) return null;
  const max = Math.max(...[...resistance, ...support].map((w) => w.oi || 0), 1);
  const row = (w, tag, side) => (
    <li key={tag} className={`sr-row ${side}`}>
      <span className="sr-tag">{tag}</span>
      <b className="sr-k">{num(w.strike, 0)}</b>
      <span className="sr-dist muted">{signed((w.strike / lc.spot - 1) * 100, 2)}%</span>
      <span className="sr-oi">
        <i style={{ width: `${((w.oi || 0) / max) * 100}%` }} />
        <span>{fmtK(w.oi || 0)}</span>
      </span>
      <span className="sr-doi">{w.dOi === null ? "–" : `${w.dOi > 0 ? "+" : ""}${fmtK(w.dOi)}`}</span>
      <span className={`sr-state ${LEAN[side][w.state] || "muted"}`}>{STATE[w.state]}</span>
    </li>
  );
  return (
    <section className="card">
      <header className="card-head">
        <div>
          <h3>Support and resistance</h3>
          <span className="muted">The three biggest call OI walls above spot (resistance) and put OI walls below it (support), nearest first</span>
        </div>
      </header>
      <ul className="sr-ladder">
        <li className="sr-row sr-head">
          <span>Level</span><span>Strike</span><span>From spot</span><span>Open interest</span><span>Today</span><span>Writers</span>
        </li>
        {[...resistance].reverse().map((w, i) => row(w, `R${resistance.length - i}`, "res"))}
        <li className="sr-spot"><span>Spot {num(lc.spot)}</span></li>
        {support.map((w, i) => row(w, `S${i + 1}`, "sup"))}
      </ul>
      <p className="footnote">Green = the writers' action leans bullish (put writers adding below, call writers covering above); red = leans bearish.</p>
    </section>
  );
}

export default function OutlookPanel({ o, lc, status }) {
  if (!o) return null;
  const pos = Math.round(((o.score + 1) / 2) * 100);
  const span = [o.range.sdLow, o.range.sdHigh];
  const at = (v) => `${Math.max(0, Math.min(100, ((v - span[0]) / (span[1] - span[0])) * 100))}%`;
  return (
    <section className={`card outlook ${o.tone}`}>
      <div className="ol-main">
        <span className="eyebrow accent-eyebrow">Chain outlook · live</span>
        <div className="ol-verdict">
          <b className={o.tone}>{o.label}</b>
          <span className="muted">score {signed(o.score * 100, 0)}</span>
        </div>
        <p className="ol-headline">{o.headline}</p>
        <div className="gauge" aria-label={`Direction score ${Math.round(o.score * 100)}`}>
          <span className="gauge-fill" />
          <i style={{ left: `${pos}%` }} />
        </div>
        <div className="gauge-labels"><span>bearish</span><span>range</span><span>bullish</span></div>
        {!o.haveFlow && (
          <p className="muted ol-note">
            {status?.loading ? `Loading yesterday's OI to measure today's change (${status.have}/${status.need})…` : "Change in OI not available yet: direction uses price, futures and skew only."}
          </p>
        )}
      </div>

      <Wall w={o.resistance} isRes />
      <Wall w={o.support} />

      <div className="ol-range">
        <span className="eyebrow">Expected range by expiry</span>
        <div className="range-bar">
          <span className="rb-1sd" />
          <span className="rb-straddle" style={{ left: at(o.range.low), width: `calc(${at(o.range.high)} - ${at(o.range.low)})` }} />
          {o.support?.strike && <i className="rb-mark sup" style={{ left: at(o.support.strike) }} title={`support ${o.support.strike}`} />}
          {o.resistance?.strike && <i className="rb-mark res" style={{ left: at(o.resistance.strike) }} title={`resistance ${o.resistance.strike}`} />}
          <i className="rb-spot" style={{ left: at(lc.spot) }} title={`spot ${lc.spot}`} />
        </div>
        <div className="range-legend">
          <span>{num(o.range.sdLow, 0)}</span>
          <span className="muted">straddle {num(o.range.low, 0)}–{num(o.range.high, 0)}</span>
          <span>{num(o.range.sdHigh, 0)}</span>
        </div>
        <small className="muted">Bar = 1σ range from IV; brighter band = ATM straddle range. Rest of today: ±{num(o.range.todayMove, 0)} pts.</small>
      </div>

      <div className="ol-evidence">
        <span className="eyebrow">Evidence</span>
        <ul>
          {o.factors.map((f) => (
            <li key={f.key}>
              <span className={`ev-dot ${f.score > 0.1 ? "up" : f.score < -0.1 ? "down" : "flat"}`} />
              <span><b>{f.label}</b> {f.text}</span>
              <span className={`ev-score ${f.score > 0.1 ? "up" : f.score < -0.1 ? "down" : "muted"}`}>{signed(f.score * 100, 0)}</span>
            </li>
          ))}
        </ul>
      </div>

      <div className="ol-watch">
        <span className="eyebrow">What to watch</span>
        <ul>{o.watch.map((w) => <li key={w}>{w}</li>)}</ul>
      </div>
      <p className="footnote ol-foot">A rules-based read of the chain (OI build-ups, PCR change, momentum, futures, max pain, skew), not a forecast or advice. Odds are risk-neutral, from IV.</p>
    </section>
  );
}
