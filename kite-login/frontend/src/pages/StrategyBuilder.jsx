import { useMemo, useState } from "react";
import { api } from "../lib/api.js";
import { b76, greeks, ncdf } from "../lib/options.js";
import { num, rupee, signed, tone } from "../lib/format.js";
import { ErrorNote } from "../components/ui.jsx";

// Strategy builder: legs picked from the chain (B / S) or a preset, priced live from the chain's ticks.
// Payoff at expiry and today (T+0, Black-76 on each leg's live IV), max profit / loss, breakevens, probability of
// profit (lognormal on ATM IV) and net Greeks. "Paper trade" records the strategy in the paper book at the live
// bid / ask; nothing is ordered on Kite.

const sign = (l) => (l.side === "BUY" ? 1 : -1);
const fill = (l) => (l.side === "BUY" ? l.ask || l.ltp : l.bid || l.ltp);

export const PRESETS = [
  { id: "long_straddle", name: "Long straddle", view: "big move", legs: [["CE", 0, "BUY"], ["PE", 0, "BUY"]] },
  { id: "short_straddle", name: "Short straddle", view: "range", legs: [["CE", 0, "SELL"], ["PE", 0, "SELL"]] },
  { id: "long_strangle", name: "Long strangle", view: "big move", legs: [["CE", 2, "BUY"], ["PE", -2, "BUY"]] },
  { id: "short_strangle", name: "Short strangle", view: "range", legs: [["CE", 2, "SELL"], ["PE", -2, "SELL"]] },
  { id: "bull_call", name: "Bull call spread", view: "bullish", legs: [["CE", 0, "BUY"], ["CE", 2, "SELL"]] },
  { id: "bear_put", name: "Bear put spread", view: "bearish", legs: [["PE", 0, "BUY"], ["PE", -2, "SELL"]] },
  { id: "bull_put", name: "Bull put spread", view: "bullish", legs: [["PE", -1, "SELL"], ["PE", -3, "BUY"]] },
  { id: "bear_call", name: "Bear call spread", view: "bearish", legs: [["CE", 1, "SELL"], ["CE", 3, "BUY"]] },
  { id: "iron_condor", name: "Iron condor", view: "range", legs: [["CE", 2, "SELL"], ["CE", 4, "BUY"], ["PE", -2, "SELL"], ["PE", -4, "BUY"]] },
  { id: "iron_fly", name: "Iron butterfly", view: "range", legs: [["CE", 0, "SELL"], ["PE", 0, "SELL"], ["CE", 3, "BUY"], ["PE", -3, "BUY"]] },
];

export function presetLegs(p, lc) {
  const ai = lc.rows.findIndex((r) => r.strike === lc.atm);
  const out = [];
  for (const [type, off, side] of p.legs) {
    const r = lc.rows[ai + off];
    const leg = r?.[type === "CE" ? "ce" : "pe"];
    if (leg) out.push({ key: leg.key, tradingsymbol: leg.tradingsymbol, strike: r.strike, type, side, lots: 1 });
  }
  return out;
}

// add a leg from the chain: same side adds a lot, the opposite side takes one off
export function addLeg(legs, leg, strike, type, side) {
  const i = legs.findIndex((l) => l.key === leg.key);
  if (i < 0) return [...legs, { key: leg.key, tradingsymbol: leg.tradingsymbol, strike, type, side, lots: 1 }];
  const l = legs[i];
  const lots = l.side === side ? l.lots + 1 : l.lots - 1;
  return lots <= 0 ? legs.filter((_, j) => j !== i) : legs.map((x, j) => (j === i ? { ...x, lots } : x));
}

function analyse(legs, lc, lot) {
  const spot = lc.spot;
  const fr = lc.forward / spot;
  const t = lc.t;
  const sigma = (lc.atmIv || 15) / 100;
  const move = Math.max(spot * sigma * Math.sqrt(t) * 3, spot * 0.04);
  const lo = Math.max(1, spot - move);
  const hi = spot + move;
  const N = 220;
  const xs = Array.from({ length: N + 1 }, (_, i) => lo + ((hi - lo) * i) / N);
  const priced = legs.map((l) => ({ ...l, qty: l.lots * lot, entry: fill(l), iv: (l.iv || lc.atmIv || 15) / 100 }));
  const ok = priced.every((l) => l.entry > 0);
  const atExp = (S) => priced.reduce((a, l) => a + sign(l) * ((l.type === "CE" ? Math.max(0, S - l.strike) : Math.max(0, l.strike - S)) - l.entry) * l.qty, 0);
  const today = (S) => priced.reduce((a, l) => a + sign(l) * (b76(S * fr, l.strike, t, l.iv, l.type === "CE") - l.entry) * l.qty, 0);
  const exp = xs.map(atExp);
  const now = xs.map(today);
  const callNet = priced.reduce((a, l) => a + (l.type === "CE" ? sign(l) * l.qty : 0), 0);
  const extremes = [...exp, atExp(0.01)];
  const maxP = callNet > 0 ? Infinity : Math.max(...extremes, ...priced.map((l) => atExp(l.strike)));
  const maxL = callNet < 0 ? -Infinity : Math.min(...extremes, ...priced.map((l) => atExp(l.strike)));
  const be = [];
  for (let i = 1; i < xs.length; i++) {
    if ((exp[i - 1] < 0 && exp[i] >= 0) || (exp[i - 1] >= 0 && exp[i] < 0)) {
      be.push(xs[i - 1] + ((xs[i] - xs[i - 1]) * (0 - exp[i - 1])) / (exp[i] - exp[i - 1]));
    }
  }
  // probability the expiry price lands where the payoff is positive (lognormal around the forward, ATM IV)
  const F = lc.forward;
  const sd = sigma * Math.sqrt(t);
  const cdf = (x) => (x <= 0 ? 0 : ncdf((Math.log(x / F) + 0.5 * sd * sd) / sd));
  let pop = 0;
  const wide = Array.from({ length: 801 }, (_, i) => (spot * 2 * i) / 800);
  for (let i = 1; i < wide.length; i++) if (atExp((wide[i - 1] + wide[i]) / 2) > 0) pop += cdf(wide[i]) - cdf(wide[i - 1]);
  if (atExp(spot * 2) > 0) pop += 1 - cdf(spot * 2);
  const g = priced.reduce((a, l) => {
    const x = greeks(lc.forward, l.strike, t, l.iv, l.type === "CE");
    if (!x) return a;
    const m = sign(l) * l.qty;
    return { delta: a.delta + x.delta * m, gamma: a.gamma + x.gamma * m, theta: a.theta + x.theta * m, vega: a.vega + x.vega * m };
  }, { delta: 0, gamma: 0, theta: 0, vega: 0 });
  const premium = priced.reduce((a, l) => a - sign(l) * l.entry * l.qty, 0);
  return { ok, xs, exp, now, maxP, maxL, be, pop, g, premium, priced, nowPnl: today(spot), sigmaMove: spot * sd };
}

function Payoff({ a, spot }) {
  const [hover, setHover] = useState(null);
  const W = 760;
  const H = 260;
  const pad = { l: 64, r: 14, t: 14, b: 26 };
  const vals = [...a.exp, ...a.now];
  let ymin = Math.min(...vals, 0);
  let ymax = Math.max(...vals, 0);
  const span = ymax - ymin || 1;
  ymin -= span * 0.08;
  ymax += span * 0.08;
  const x0 = a.xs[0];
  const x1 = a.xs.at(-1);
  const x = (v) => pad.l + ((v - x0) / (x1 - x0)) * (W - pad.l - pad.r);
  const y = (v) => pad.t + (1 - (v - ymin) / (ymax - ymin)) * (H - pad.t - pad.b);
  const path = (ys) => ys.map((v, i) => `${i ? "L" : "M"}${x(a.xs[i]).toFixed(1)},${y(v).toFixed(1)}`).join("");
  const zf = Math.min(1, Math.max(0, (y(0) - pad.t) / (H - pad.t - pad.b)));
  const area = `${path(a.exp)}L${x(x1)},${y(0)}L${x(x0)},${y(0)}Z`;
  const ticks = Array.from({ length: 5 }, (_, i) => ymin + ((ymax - ymin) * i) / 4);
  const onMove = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    const i = Math.round(((px - pad.l) / (W - pad.l - pad.r)) * (a.xs.length - 1));
    setHover(i >= 0 && i < a.xs.length ? i : null);
  };
  return (
    <div className="payoff">
      <svg viewBox={`0 0 ${W} ${H}`} onMouseMove={onMove} onMouseLeave={() => setHover(null)} role="img" aria-label="Strategy payoff">
        <defs>
          <linearGradient id="pf-fill" gradientUnits="userSpaceOnUse" x1="0" x2="0" y1={pad.t} y2={H - pad.b}>
            <stop offset="0" stopColor="var(--up)" stopOpacity="0.32" />
            <stop offset={zf} stopColor="var(--up)" stopOpacity="0.04" />
            <stop offset={zf} stopColor="var(--down)" stopOpacity="0.04" />
            <stop offset="1" stopColor="var(--down)" stopOpacity="0.32" />
          </linearGradient>
          <linearGradient id="pf-line" gradientUnits="userSpaceOnUse" x1="0" x2="0" y1={pad.t} y2={H - pad.b}>
            <stop offset={zf} stopColor="var(--up)" />
            <stop offset={zf} stopColor="var(--down)" />
          </linearGradient>
        </defs>
        {ticks.map((v) => (
          <g key={v}>
            <line x1={pad.l} x2={W - pad.r} y1={y(v)} y2={y(v)} className="pf-grid" />
            <text x={pad.l - 6} y={y(v) + 4} textAnchor="end" className="axis">{num(v, 0)}</text>
          </g>
        ))}
        <rect x={x(Math.max(x0, spot - a.sigmaMove))} width={x(Math.min(x1, spot + a.sigmaMove)) - x(Math.max(x0, spot - a.sigmaMove))}
          y={pad.t} height={H - pad.t - pad.b} className="pf-sigma" />
        <line x1={pad.l} x2={W - pad.r} y1={y(0)} y2={y(0)} className="pf-zero" />
        <path d={area} fill="url(#pf-fill)" />
        <path d={path(a.exp)} fill="none" stroke="url(#pf-line)" strokeWidth="2" />
        <path d={path(a.now)} fill="none" className="pf-today" />
        {a.be.map((b) => (
          <g key={b}>
            <circle cx={x(b)} cy={y(0)} r="4" className="pf-be" />
            <text x={x(b)} y={H - 8} textAnchor="middle" className="axis pf-be-t">{num(b, 0)}</text>
          </g>
        ))}
        <line x1={x(spot)} x2={x(spot)} y1={pad.t} y2={H - pad.b} className="spot-line" />
        <text x={x(spot) + 5} y={pad.t + 10} className="spot-label">spot {num(spot)}</text>
        {hover != null && (
          <g>
            <line x1={x(a.xs[hover])} x2={x(a.xs[hover])} y1={pad.t} y2={H - pad.b} className="pf-cross" />
            <circle cx={x(a.xs[hover])} cy={y(a.exp[hover])} r="3.5" className="pf-dot" />
            <circle cx={x(a.xs[hover])} cy={y(a.now[hover])} r="3" className="pf-dot today" />
          </g>
        )}
      </svg>
      {hover != null && (
        <div className="pf-tip" style={{ left: `${(x(a.xs[hover]) / W) * 100}%` }}>
          <b>{num(a.xs[hover], 0)}</b> <small className="muted">{signed((a.xs[hover] / spot - 1) * 100, 1)}%</small>
          <span>Expiry <b className={tone(a.exp[hover])}>{rupee(a.exp[hover])}</b></span>
          <span>Today <b className={tone(a.now[hover])}>{rupee(a.now[hover])}</b></span>
        </div>
      )}
      <div className="pf-legend">
        <span className="exp">At expiry</span>
        <span className="t0">Today (T+0)</span>
        <span className="sig">±1σ expected move</span>
        <span className="be">Breakeven</span>
      </div>
    </div>
  );
}

const money = (v) => (v === Infinity ? "Unlimited" : v === -Infinity ? "Unlimited" : `₹${num(Math.abs(v), 0)}`);

export default function StrategyBuilder({ legs, setLegs, lc, chain, openTicket }) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const lot = chain.lot_size;
  const byKey = useMemo(() => {
    const m = {};
    for (const r of lc.rows) for (const side of ["ce", "pe"]) if (r[side]) m[r[side].key] = r[side];
    return m;
  }, [lc]);
  const live = legs.map((l) => ({ ...l, ...(byKey[l.key] ? { ltp: byKey[l.key].ltp, bid: byKey[l.key].bid, ask: byKey[l.key].ask, iv: byKey[l.key].iv } : {}) }));
  const a = useMemo(() => (live.length ? analyse(live, lc, lot) : null), [live, lc, lot]); // eslint-disable-line react-hooks/exhaustive-deps
  const name = PRESETS.find((p) => JSON.stringify(presetLegs(p, lc).map((l) => [l.key, l.side])) === JSON.stringify(legs.map((l) => [l.key, l.side])))?.name
    || (legs.length === 1 ? `${legs[0].side === "BUY" ? "Long" : "Short"} ${num(legs[0].strike, 0)} ${legs[0].type}` : "Custom strategy");
  const set = (i, patch) => setLegs(legs.map((l, j) => (j === i ? { ...l, ...patch } : l)));

  const paper = async () => {
    setBusy(true);
    setErr("");
    setMsg("");
    try {
      const p = await api.paperOptionsOpen({ name, underlying: chain.underlying, exchange: chain.exchange,
        legs: legs.map((l) => ({ tradingsymbol: l.tradingsymbol, side: l.side, lots: l.lots })) });
      setMsg(`Paper position opened: ${p.name}, ${p.premium >= 0 ? "credit" : "debit"} ₹${num(Math.abs(p.premium), 0)}. Track it on the Paper tab.`);
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card builder">
      <header className="card-head">
        <div>
          <h3>Strategy builder</h3>
          <span className="muted small">Pick a preset or press B / S in the chain to add legs · {chain.underlying} {chain.expiry} · lot {lot}</span>
        </div>
        {legs.length > 0 && <button type="button" className="ghost small" onClick={() => { setLegs([]); setMsg(""); }}>Clear</button>}
      </header>
      <div className="preset-row">
        {PRESETS.map((p) => (
          <button key={p.id} type="button" className={`preset ${p.view.replace(" ", "-")}`} onClick={() => { setLegs(presetLegs(p, lc)); setMsg(""); }}>
            <b>{p.name}</b>
            <small>{p.view}</small>
          </button>
        ))}
      </div>

      {legs.length === 0 ? (
        <p className="muted builder-empty">No legs yet. Choose a preset above, or press <b className="up">B</b> / <b className="down">S</b> next to any price in the chain.</p>
      ) : (
        <div className="builder-grid">
          <div className="legs">
            <table className="table legs-table">
              <thead>
                <tr><th>Side</th><th>Strike</th><th className="num">Lots</th><th className="num">Price</th><th className="num">IV</th><th /></tr>
              </thead>
              <tbody>
                {live.map((l, i) => (
                  <tr key={l.key}>
                    <td>
                      <button type="button" className={`side-tog ${l.side === "BUY" ? "b" : "s"}`} onClick={() => set(i, { side: l.side === "BUY" ? "SELL" : "BUY" })}
                        title="Switch buy / sell">{l.side === "BUY" ? "B" : "S"}</button>
                    </td>
                    <td><b>{num(l.strike, l.strike % 1 ? 1 : 0)}</b> <span className={l.type === "CE" ? "ce-t" : "pe-t"}>{l.type}</span></td>
                    <td className="num">
                      <span className="stepper">
                        <button type="button" onClick={() => (l.lots > 1 ? set(i, { lots: l.lots - 1 }) : setLegs(legs.filter((_, j) => j !== i)))} aria-label="One lot less">−</button>
                        <b>{l.lots}</b>
                        <button type="button" onClick={() => set(i, { lots: Math.min(100, l.lots + 1) })} aria-label="One lot more">+</button>
                      </span>
                    </td>
                    <td className="num">{num(fill(l))}</td>
                    <td className="num muted">{l.iv ? num(l.iv, 1) : "–"}</td>
                    <td className="num">
                      <button type="button" className="linkish small" title="Open a real Kite order ticket for this leg (you confirm it there)"
                        onClick={() => openTicket({ exchange: chain.exchange, tradingsymbol: l.tradingsymbol, transaction_type: l.side, product: "NRML",
                          order_type: "LIMIT", price: fill(l), quantity: l.lots * lot })}>Kite…</button>
                      <button type="button" className="leg-x" onClick={() => setLegs(legs.filter((_, j) => j !== i))} aria-label={`Remove ${l.tradingsymbol}`}>×</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {a && (
              <div className="b-stats">
                <span><small>{a.premium >= 0 ? "Net credit" : "Net debit"}</small><b className={tone(a.premium)}>₹{num(Math.abs(a.premium), 0)}</b></span>
                <span><small>Max profit</small><b className="up">{money(a.maxP)}</b></span>
                <span><small>Max loss</small><b className="down">{money(a.maxL)}</b></span>
                <span><small>Reward : risk</small><b>{Number.isFinite(a.maxP) && Number.isFinite(a.maxL) && a.maxL < 0 ? `${num(a.maxP / -a.maxL, 2)}` : "–"}</b></span>
                <span><small>Breakeven</small><b>{a.be.length ? a.be.map((b) => num(b, 0)).join(" · ") : "–"}</b></span>
                <span><small>Prob. of profit</small><b>{num(a.pop * 100, 0)}%</b></span>
                <span><small>Delta</small><b className={tone(a.g.delta)}>{signed(a.g.delta, 1)}</b></span>
                <span><small>Theta / day</small><b className={tone(a.g.theta)}>{rupee(a.g.theta)}</b></span>
                <span><small>Vega / IV pt</small><b className={tone(a.g.vega)}>{rupee(a.g.vega)}</b></span>
                <span><small>Gamma</small><b>{num(a.g.gamma, 4)}</b></span>
              </div>
            )}
            <div className="builder-act">
              <button type="button" className="primary" disabled={busy || !a?.ok} onClick={paper}>
                {busy ? "Opening…" : "Paper trade this strategy"}
              </button>
              <small className="muted">Fills at the live bid / ask, charges added on exit. No Kite order.</small>
            </div>
            {msg && <p className="ok-note">{msg}</p>}
            <ErrorNote error={err} />
          </div>
          {a && <Payoff a={a} spot={lc.spot} />}
        </div>
      )}
    </section>
  );
}
