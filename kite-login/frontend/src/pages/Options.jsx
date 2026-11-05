import { Fragment, useEffect, useMemo, useState } from "react";
import { api } from "../lib/api.js";
import { liveChain } from "../lib/options.js";
import { BUILDUP_TEXT, buildup, fmtK, optionOutlook, premiumChange } from "../lib/outlook.js";
import OutlookPanel, { Levels } from "./OutlookPanel.jsx";
import { dateTime, num, signed, tone } from "../lib/format.js";
import { useTicks } from "../lib/ticks.js";
import { Badge, ErrorNote, Segmented, Spinner } from "../components/ui.jsx";
import StrategyBuilder, { addLeg } from "./StrategyBuilder.jsx";

const PREF = "kite.optionChain";
const WINDOWS = [10, 15, 20, 30];
const LABEL = { NIFTY: "Nifty 50", BANKNIFTY: "Bank Nifty", FINNIFTY: "Fin Nifty", MIDCPNIFTY: "Midcap Select", NIFTYNXT50: "Nifty Next 50", SENSEX: "Sensex", BANKEX: "Bankex" };
const MAX_STREAM = 560;   // instruments per live stream (backend allows 600)

function loadPref() {
  try {
    return { underlying: "NIFTY", window: 15, cols: "std", oiView: "oi", ...JSON.parse(localStorage.getItem(PREF) || "{}") };
  } catch {
    return { underlying: "NIFTY", window: 15, cols: "std", oiView: "oi" };
  }
}

const compact = (v) => (v === null || v === undefined ? "–" : v >= 1e7 ? `${num(v / 1e7, 2)}Cr` : v >= 1e5 ? `${num(v / 1e5, 2)}L` : v >= 1e3 ? `${num(v / 1e3, 1)}K` : num(v, 0));

function Kpi({ label, value, sub, t }) {
  return (
    <div className="kpi">
      <span>{label}</span>
      <b className={t || ""}>{value}</b>
      {sub && <small>{sub}</small>}
    </div>
  );
}

// Call vs put open interest per strike, with spot and max-pain markers
const chgOi = (l) => (l && l.prev_oi !== null && l.prev_oi !== undefined ? (l.oi || 0) - l.prev_oi : 0);

function OiChart({ rows, spot, maxPain, mode }) {
  const W = 1000;
  const H = 220;
  const pad = { l: 8, r: 8, t: 14, b: 30 };
  const val = mode === "chg" ? chgOi : (l) => l?.oi || 0;
  const max = Math.max(...rows.flatMap((r) => [Math.abs(val(r.ce)), Math.abs(val(r.pe))]), 1);
  const lo = mode === "chg" ? -max : 0;
  const n = rows.length;
  const bw = (W - pad.l - pad.r) / n;
  const x = (i) => pad.l + i * bw;
  const y = (v) => pad.t + (1 - (v - lo) / (max - lo)) * (H - pad.t - pad.b);
  const bar = (v, x0, cls) => <rect x={x0} y={Math.min(y(v), y(0))} width={bw * 0.36} height={Math.max(1, Math.abs(y(v) - y(0)))} className={`${cls}${v < 0 ? " neg" : ""}`} />;
  const posOf = (px) => {
    const i = rows.findIndex((r) => r.strike >= px);
    if (i <= 0) return x(0);
    const a = rows[i - 1].strike;
    const b = rows[i].strike;
    return x(i - 1) + bw / 2 + ((px - a) / (b - a)) * bw;
  };
  return (
    <svg className="oi-chart" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label="Open interest by strike">
      {rows.map((r, i) => (
        <g key={r.strike}>
          {bar(val(r.ce), x(i) + bw * 0.12, "oi-ce")}
          {bar(val(r.pe), x(i) + bw * 0.52, "oi-pe")}
          {i % Math.max(1, Math.round(n / 10)) === 0 && (
            <text x={x(i) + bw / 2} y={H - 10} textAnchor="middle" className="axis">{num(r.strike, 0)}</text>
          )}
        </g>
      ))}
      {mode === "chg" && <line x1={pad.l} x2={W - pad.r} y1={y(0)} y2={y(0)} className="pf-zero" />}
      {maxPain && <line x1={posOf(maxPain)} x2={posOf(maxPain)} y1={pad.t} y2={H - pad.b} className="mp-line" />}
      <line x1={posOf(spot)} x2={posOf(spot)} y1={pad.t - 6} y2={H - pad.b} className="spot-line" />
      <text x={posOf(spot) + 6} y={pad.t + 4} className="spot-label">spot {num(spot)}</text>
    </svg>
  );
}

const greek = (v, d) => (v === null || v === undefined ? "–" : num(v, d));

function Leg({ leg, side, onPick, maxOi, cols, picked }) {
  if (!leg) return <td colSpan={cols === "greeks" ? 7 : 5} className="muted">–</td>;
  const cells = [
    <td key="oi" className={`num oi-cell c-${side}`}>
      <span className={`oi-bar ${side}`} style={{ width: `${Math.min(100, ((leg.oi || 0) / maxOi) * 100)}%` }} />
      <span className="oi-val">{compact(leg.oi)}</span>
      {leg.prev_oi !== null && leg.prev_oi !== undefined && (
        <small className={`doi ${(leg.oi || 0) - leg.prev_oi >= 0 ? "up" : "down"}`}>
          {(leg.oi || 0) - leg.prev_oi >= 0 ? "+" : ""}{fmtK((leg.oi || 0) - leg.prev_oi)}
          {(() => {
            const b = buildup((leg.oi || 0) - leg.prev_oi, premiumChange(leg));
            return b ? <em className={`bu ${b}`} title={BUILDUP_TEXT[b]}>{b}</em> : null;
          })()}
        </small>
      )}
    </td>,
    ...(cols === "greeks" ? [] : [<td key="vol" className={`num muted c-${side}`}>{compact(leg.volume)}</td>]),
    <td key="iv" className={`num c-${side}`}>{leg.iv ? num(leg.iv, 1) : "–"}</td>,
    <td key="d" className={`num muted c-${side}`}>{greek(leg.delta, 2)}</td>,
    ...(cols === "greeks" ? [
      <td key="g" className={`num muted c-${side}`}>{greek(leg.gamma, 4)}</td>,
      <td key="t" className={`num muted c-${side}`}>{greek(leg.theta, 2)}</td>,
      <td key="v" className={`num muted c-${side}`}>{greek(leg.vega, 2)}</td>,
    ] : []),
    <td key="ltp" className={`num ltp-cell c-${side}${picked ? ` picked ${picked}` : ""}`}>
      <span className="ltp-wrap">
        <b key={leg.ltp} className={leg.live ? (leg.change_pct >= 0 ? "flash-up" : "flash-down") : ""}>{num(leg.ltp)}</b>
        <small className={tone(leg.change_pct)}>{leg.change_pct === null || leg.change_pct === undefined ? "" : `${signed(leg.change_pct, 1)}%`}</small>
        <span className="bs">
          <button type="button" className="b" title={`Add buy ${leg.tradingsymbol} to the strategy builder`} onClick={() => onPick(leg, "BUY")}>B</button>
          <button type="button" className="s" title={`Add sell ${leg.tradingsymbol} to the strategy builder`} onClick={() => onPick(leg, "SELL")}>S</button>
        </span>
      </span>
    </td>,
  ];
  return side === "ce" ? cells : [...cells].reverse();
}

export default function Options({ openTicket }) {
  const [pref, setPref] = useState(loadPref);
  const [unders, setUnders] = useState(null);
  const [expiry, setExpiry] = useState("");
  const [chain, setChain] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [now, setNow] = useState(Date.now());
  const [legs, setLegs] = useState([]);
  const save = (p) => {
    setPref(p);
    try {
      localStorage.setItem(PREF, JSON.stringify(p));
    } catch {
      /* storage blocked */
    }
  };

  useEffect(() => {
    api.optionUnderlyings().then(setUnders).catch((e) => setError(e.message));
    const t = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(t);
  }, []);

  // Snapshot (contracts, expiries, baseline quotes): on open, on change, and every 2 minutes as a safety net.
  // Prices, OI and volume then move with live ticks.
  useEffect(() => {
    let alive = true;
    const load = (quiet) => {
      if (!quiet) setLoading(true);
      api.optionChain(pref.underlying, expiry || undefined)
        .then((c) => alive && (setChain(c), setError("")))
        .catch((e) => alive && setError(e.message))
        .finally(() => alive && setLoading(false));
    };
    load(false);
    const t = setInterval(() => document.visibilityState === "visible" && load(true), 120_000);
    const fast = setInterval(() => window.__ocLoading && load(true), 15_000);
    return () => {
      alive = false;
      clearInterval(t);
      clearInterval(fast);
    };
  }, [pref.underlying, expiry]);
  useEffect(() => setLegs([]), [pref.underlying, chain?.expiry]);
  useEffect(() => {
    const st = chain?.prev_oi_status;
    window.__ocLoading = Boolean(st && (st.loading || st.have < st.need));
  }, [chain]);

  const keys = useMemo(() => {
    if (!chain) return [];
    const all = [chain.future_detail?.key, ...chain.rows.flatMap((r) => [r.ce?.key, r.pe?.key])].filter(Boolean);
    if (all.length + 1 <= MAX_STREAM) return [chain.spot_key, ...all];
    const ai = chain.rows.findIndex((r) => r.strike === chain.atm);
    const half = Math.floor(MAX_STREAM / 4);
    return [chain.spot_key, ...chain.rows.slice(Math.max(0, ai - half), ai + half).flatMap((r) => [r.ce?.key, r.pe?.key]).filter(Boolean)];
  }, [chain]);
  const { ticks, live } = useTicks(keys);
  const lc = useMemo(() => liveChain(chain, ticks, now), [chain, ticks, now]);
  const outlook = useMemo(() => optionOutlook(lc, chain, ticks[chain?.future_detail?.key]), [lc, chain, ticks]);

  const view = useMemo(() => {
    if (!lc) return [];
    const ai = lc.rows.findIndex((r) => r.strike === lc.atm);
    return lc.rows.slice(Math.max(0, ai - pref.window), ai + pref.window + 1);
  }, [lc, pref.window]);
  const maxOi = Math.max(...view.flatMap((r) => [r.ce?.oi || 0, r.pe?.oi || 0]), 1);
  const spotIdx = view.findIndex((r) => r.strike >= (lc?.spot || 0));
  const lastTick = Object.values(ticks).map((t) => t.time).filter(Boolean).sort().at(-1);

  const pick = (strike, type) => (leg, side) => setLegs((ls) => addLeg(ls, leg, strike, type, side));
  const pickedSide = (leg) => {
    const l = leg && legs.find((x) => x.key === leg.key);
    return l ? (l.side === "BUY" ? "b" : "s") : "";
  };
  const span = pref.cols === "greeks" ? 7 : 5;

  const pcrNote = !lc?.pcr ? "" : lc.pcr >= 1.2 ? "put-heavy: writers defend lower levels" : lc.pcr <= 0.8 ? "call-heavy: writers cap upside" : "balanced";

  return (
    <div className="stack options">
      <section className="card opt-controls">
        <label className="field">
          <span>Underlying</span>
          <select value={pref.underlying} onChange={(e) => { setExpiry(""); save({ ...pref, underlying: e.target.value }); }}>
            <optgroup label="Indices">
              {(unders?.indices || Object.keys(LABEL)).map((u) => <option key={u} value={u}>{LABEL[u] || u}</option>)}
            </optgroup>
            {unders?.stocks?.length > 0 && (
              <optgroup label="Stocks">
                {unders.stocks.map((u) => <option key={u} value={u}>{u}</option>)}
              </optgroup>
            )}
          </select>
        </label>
        <div className="field">
          <span>Expiry</span>
          <Segmented size="small" label="Expiry" value={chain?.expiry || ""} onChange={setExpiry}
            options={(chain?.expiries || []).slice(0, 6).map((e) => ({ id: e, label: dateTime(e, { day: "numeric", month: "short" }) }))} />
        </div>
        <div className="field">
          <span>Strikes around ATM</span>
          <Segmented size="small" label="Strikes" value={pref.window} onChange={(w) => save({ ...pref, window: w })}
            options={WINDOWS.map((w) => ({ id: w, label: `±${w}` }))} />
        </div>
        <div className="field">
          <span>Columns</span>
          <Segmented size="small" label="Columns" value={pref.cols} onChange={(c) => save({ ...pref, cols: c })}
            options={[{ id: "std", label: "OI & volume" }, { id: "greeks", label: "Greeks" }]} />
        </div>
        <div className="opt-status">
          {loading && <Spinner small />}
          {lc?.live && live ? <span className="live-tag">LIVE</span> : <Badge>{chain ? "market closed" : "loading"}</Badge>}
          <small className="muted">{lastTick ? `last tick ${lastTick.slice(11, 19)}` : chain ? `snapshot ${chain.as_of.slice(11, 19)}` : ""}</small>
        </div>
      </section>
      <ErrorNote error={error} />

      {lc && (
        <>
          <OutlookPanel o={outlook} lc={lc} status={chain.prev_oi_status} />
          <Levels o={outlook} lc={lc} />
          <section className="card">
            <div className="kpis opt-kpis">
              <Kpi label={`${LABEL[chain.underlying] || chain.underlying} spot`} value={num(lc.spot)} t={tone(lc.spotChange)}
                sub={lc.spotChange === null ? "" : `${signed(lc.spotChange, 2)}% today`} />
              <Kpi label="Forward (parity)" value={num(lc.forward)} sub={chain.future ? `future ${num(chain.future)}` : "no future this expiry"} />
              <Kpi label="ATM strike · IV" value={`${num(lc.atm, 0)} · ${lc.atmIv ? `${num(lc.atmIv, 1)}%` : "–"}`} sub={`${num(lc.daysToExpiry, 1)} days to expiry`} />
              <Kpi label="ATM straddle" value={num(lc.straddle)} sub={lc.impliedMove ? `implies ±${num(lc.impliedMove, 2)}% (±${num(lc.straddle, 0)} pts) by expiry` : ""} />
              <Kpi label="PCR (OI)" value={lc.pcr ? num(lc.pcr, 2) : "–"} t={lc.pcr >= 1 ? "up" : "down"} sub={pcrNote} />
              <Kpi label="Max pain" value={num(lc.maxPain, 0)} sub={lc.maxPain ? `${signed((lc.maxPain / lc.spot - 1) * 100, 1)}% from spot` : ""} />
              <Kpi label="Support (put OI wall)" value={num(lc.support, 0)} t="up" sub={lc.support ? `${signed((lc.support / lc.spot - 1) * 100, 1)}%` : ""} />
              <Kpi label="Resistance (call OI wall)" value={num(lc.resistance, 0)} t="down" sub={lc.resistance ? `${signed((lc.resistance / lc.spot - 1) * 100, 1)}%` : ""} />
              <Kpi label="Lot size" value={num(chain.lot_size, 0)} sub={`${chain.exchange} · expiry ${dateTime(chain.expiry, { dateStyle: "medium" })}`} />
            </div>
          </section>

          <StrategyBuilder legs={legs} setLegs={setLegs} lc={lc} chain={chain} openTicket={openTicket} />

          <section className="card">
            <header className="card-head">
              <div>
                <h3>{pref.oiView === "chg" ? "Change in open interest by strike" : "Open interest by strike"}</h3>
                <span className="muted">
                  <span className="legend ce" /> calls (writers' resistance) <span className="legend pe" /> puts (writers' support) · dashed = max pain
                </span>
              </div>
              <Segmented size="small" label="OI chart" value={pref.oiView} onChange={(v) => save({ ...pref, oiView: v })}
                options={[{ id: "oi", label: "Open interest" }, { id: "chg", label: "Change in OI" }]} />
            </header>
            <OiChart rows={view} spot={lc.spot} maxPain={lc.maxPain} mode={pref.oiView} />
          </section>

          <section className="card chain-card">
            <div className="table-wrap chain-wrap">
              <table className="table chain">
                <thead>
                  <tr className="side-head">
                    <th colSpan={span} className="calls">CALLS</th>
                    <th className="strike-h">STRIKE</th>
                    <th colSpan={span} className="puts">PUTS</th>
                  </tr>
                  <tr>
                    {pref.cols === "greeks" ? (
                      <>
                        <th className="num">OI · ΔOI</th><th className="num">IV %</th><th className="num">Δ</th><th className="num" title="Gamma: change in delta per point">Γ</th>
                        <th className="num" title="Theta: premium lost per day">Θ</th><th className="num" title="Vega: premium change per 1 IV point">Vega</th><th className="num">LTP</th>
                        <th className="strike-h">{chain.expiry}</th>
                        <th className="num">LTP</th><th className="num">Vega</th><th className="num">Θ</th><th className="num">Γ</th><th className="num">Δ</th><th className="num">IV %</th><th className="num">OI · ΔOI</th>
                      </>
                    ) : (
                      <>
                        <th className="num">OI · ΔOI</th><th className="num">Volume</th><th className="num">IV %</th><th className="num">Δ</th><th className="num">LTP</th>
                        <th className="strike-h">{chain.expiry}</th>
                        <th className="num">LTP</th><th className="num">Δ</th><th className="num">IV %</th><th className="num">Volume</th><th className="num">OI · ΔOI</th>
                      </>
                    )}
                  </tr>
                </thead>
                <tbody>
                  {view.map((r, i) => (
                    <Fragment key={r.strike}>
                      {i === spotIdx && (
                        <tr className="spot-row">
                          <td colSpan={span * 2 + 1}><span>Spot {num(lc.spot)}</span></td>
                        </tr>
                      )}
                      <tr className={`${r.strike === lc.atm ? "atm" : ""} ${r.strike < lc.spot ? "ce-itm" : ""} ${r.strike > lc.spot ? "pe-itm" : ""}`}>
                        <Leg leg={r.ce} side="ce" onPick={pick(r.strike, "CE")} maxOi={maxOi} cols={pref.cols} picked={pickedSide(r.ce)} />
                        <td className="strike">
                          <b>{num(r.strike, r.strike % 1 ? 1 : 0)}</b>
                          {r.strike === lc.maxPain && <small>max pain</small>}
                          {r.strike === lc.support && <small className="up">support</small>}
                          {r.strike === lc.resistance && <small className="down">resistance</small>}
                        </td>
                        <Leg leg={r.pe} side="pe" onPick={pick(r.strike, "PE")} maxOi={maxOi} cols={pref.cols} picked={pickedSide(r.pe)} />
                      </tr>
                    </Fragment>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="footnote">
              Live from Kite ticks: LTP, bid/ask, OI and volume per contract; IV and Δ recomputed on each tick (Black-76 on the put-call-parity
              forward); Greeks are per unit, theta per day and vega per IV point. PCR and max pain use every strike of the expiry. Shaded = in the money.
              B / S adds the contract to the strategy builder (press again for another lot, the other letter to take one off).
            </p>
          </section>
        </>
      )}
    </div>
  );
}
