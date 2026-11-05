import { Sparkline } from "../components/charts.jsx";
import { num, rupee, shortSector, signed, tone } from "../lib/format.js";
import { liveNet } from "../components/live.jsx";
import { useTicks } from "../lib/ticks.js";

const HUES = [190, 160, 42, 268, 12, 210, 330, 95];
// Symbol "logo": initials on a tinted tile (no third-party logo fetches).
export function Logo({ symbol = "", size = 34, text, hue }) {
  const h = hue ?? HUES[[...symbol].reduce((a, c) => a + c.charCodeAt(0), 0) % HUES.length];
  const txt = text || symbol.replace(/^NIFTY ?/, "N").replace(/[^A-Z0-9&]/g, "").slice(0, symbol.startsWith("NIFTY") ? 3 : 2);
  return (
    <span className="logo-tile" style={{ width: size, height: size, "--h": h, fontSize: size * 0.34 }} aria-hidden="true">
      {txt || "•"}
    </span>
  );
}

const PHASE = { "pre-open": "Pre-open", "opening range": "Opening range", "entry window": "Entry window", managing: "Managing trades", closed: "Session closed" };
const EXIT = { stop: "Stop loss hit", target: "Target hit", time: "15:10 exit", halt: "Daily stop" };
const MAX_CALLS = 6;

// ORB calls in priority order: trades taken today, signals waiting for the next bar, then the shortlist
// names nearest their opening-range high (the next breakouts to watch).
export function orbCalls(orb) {
  if (!orb) return [];
  const ltp = Object.fromEntries((orb.shortlist || []).map((r) => [r.symbol, r.ltp]));
  const out = [];
  for (const t of orb.trades || []) {
    out.push({ symbol: t.symbol, kind: t.status === "open" ? "trade" : "closed", trade: t, ltp: ltp[t.symbol] });
  }
  for (const s of orb.signals || []) {
    if (s.status === "pending" && !out.some((o) => o.symbol === s.symbol)) out.push({ symbol: s.symbol, kind: "signal", signal: s, ltp: ltp[s.symbol] });
  }
  const watch = (orb.shortlist || []).filter((r) => r.or_high && !out.some((o) => o.symbol === r.symbol))
    .sort((a, b) => (b.dist_pct ?? -99) - (a.dist_pct ?? -99));
  for (const r of watch) {
    if (out.length >= MAX_CALLS) break;
    out.push({ symbol: r.symbol, kind: "watch", row: r, ltp: r.ltp });
  }
  return out;
}

function CallCard({ c, live, open, phase }) {
  const last = live?.last_price ?? c.ltp;
  const t = c.trade;
  let tag, body;
  if (c.kind === "trade" || c.kind === "closed") {
    const px = c.kind === "closed" ? t.exit_ref : last;
    const pnl = c.kind === "closed" ? t.net : px != null ? (px - t.entry_ref) * t.qty : null;
    const top = t.target || t.entry_ref + 3 * t.R;
    const at = px != null ? Math.max(0, Math.min(100, ((px - t.sl) / (top - t.sl)) * 100)) : null;
    const entryAt = ((t.entry_ref - t.sl) / (top - t.sl)) * 100;
    tag = c.kind === "trade" ? <span className="call-tag buy">BUY · open</span> : <span className={`call-tag ${t.net >= 0 ? "buy" : "sell"}`}>{EXIT[t.exit_reason] || "Closed"}</span>;
    body = (
      <>
        <span className="watch-bot">
          <b className="watch-last">{num(px)}</b>
          <span className="watch-meta">
            {pnl != null && <span className={`pill-chg ${tone(pnl)}`}>{c.kind === "trade" ? "≈ " : ""}{rupee(pnl)}</span>}
            <small className="muted">{t.qty} @ {num(t.entry_ref)} · {t.entry_time}</small>
          </span>
        </span>
        <span className="call-levels">
          <span><small>Stop loss</small><b className="down">{num(t.sl)}</b></span>
          <span><small>Entry</small><b>{num(t.entry_ref)}</b></span>
          <span><small>{t.target ? "Target 3R" : "3R (no target)"}</small><b className="up">{num(top)}</b></span>
        </span>
        <span className="call-track" title="Price between the stop loss and the 3R target">
          <i className="call-entry" style={{ left: `${entryAt}%` }} />
          {at != null && <i className={`call-px ${tone(pnl)}`} style={{ left: `${at}%` }} />}
        </span>
      </>
    );
  } else if (c.kind === "signal") {
    const s = c.signal;
    tag = <span className="call-tag accent">Signal · enters next bar</span>;
    body = (
      <>
        <span className="watch-bot">
          <b className="watch-last">{num(last ?? s.close)}</b>
          <span className="watch-meta"><small className="muted">closed {num(s.close)} above {num(s.or_high)} at {s.time}</small></span>
        </span>
        <span className="call-levels">
          <span><small>Range high</small><b>{num(s.or_high)}</b></span>
          <span><small>Est. stop</small><b className="down">{s.atr ? num(s.close - 1.5 * s.atr) : "–"}</b></span>
          <span><small>Est. 3R</small><b className="up">{s.atr ? num(s.close + 4.5 * s.atr) : "–"}</b></span>
        </span>
      </>
    );
  } else {
    const r = c.row;
    const dist = last && r.or_high ? (last / r.or_high - 1) * 100 : r.dist_pct;
    tag = <span className="call-tag">{phase === "managing" || phase === "closed" ? "Window closed" : "Watching"}</span>;
    body = (
      <>
        <span className="watch-bot">
          <b className="watch-last">{num(last)}</b>
          <span className="watch-meta">
            {dist != null && <span className={`pill-chg ${tone(dist)}`}>{signed(dist)}% vs high</span>}
            <small className="muted">high {num(r.or_high)} · RVOL {num(r.rvol, 1)}</small>
          </span>
        </span>
        {dist != null && (
          <span className="watch-bar" title="How close price is to the opening-range high (full = at the high)">
            <i style={{ width: `${Math.max(4, Math.min(100, 100 + dist * 50))}%` }} />
          </span>
        )}
      </>
    );
  }
  return (
    <button type="button" className={`watch-card call-${c.kind}`} onClick={() => open(c.symbol)}>
      <span className="watch-top">
        <Logo symbol={c.symbol} size={30} />
        <span className="watch-id">
          <small>NSE · MIS</small>
          <b>{c.symbol}</b>
        </span>
        {tag}
      </span>
      {body}
    </button>
  );
}

export default function Insight({ darvas, orb, orbError, orbProgress, indices, openStock, go }) {
  const calls = orbCalls(orb);
  const { ticks } = useTicks(calls.map((c) => `NSE:${c.symbol}`));
  const nifty = indices?.find((m) => m.key === "NSE:NIFTY 50");
  const nSum = darvas?.indices?.find((r) => r.index === "NIFTY 50");
  const riskOn = nSum ? nSum.above_50dma && nSum.above_200dma : null;
  const sectors = [...(darvas?.sectors || [])].sort((a, b) => b.rs_ratio - a.rs_ratio).slice(0, 5);
  const maxAbs = Math.max(1, ...sectors.map((s) => Math.abs(s.chg_1m_pct || 0)));
  const ctl = orb?.controller;
  const dayNet = liveNet(orb, ticks);

  return (
    <aside className="insight">
      <header className="insight-head">
        <h3>Insight</h3>
      </header>

      <div className="insight-sec">
        <div className="insight-sub">
          <span>ORB calls</span>
          <button type="button" className="link-muted" onClick={() => go("orb")}>ORB tab →</button>
        </div>
        {orb && (
          <div className="orb-strip">
            <span><small>Session</small><b>{PHASE[orb.phase] || orb.phase}</b></span>
            <span><small>Nifty</small><b className={orb.nifty ? (orb.nifty.ok ? "up" : "down") : ""}>{orb.nifty ? (orb.nifty.ok ? "Confirms" : "No confirm") : "–"}</b></span>
            <span><small>Day P&L</small><b className={tone(dayNet)}>{ctl ? rupee(dayNet) : "–"}</b></span>
          </div>
        )}
        {calls.length ? calls.map((c) => <CallCard key={c.symbol + c.kind} c={c} live={ticks[`NSE:${c.symbol}`]} open={openStock} phase={orb.phase} />) : (
          <p className="muted small">
            {orbError ? "ORB calls need a Kite session." : !orb ? (orbProgress?.running ? `Loading ORB data ${orbProgress.done}/${orbProgress.total}…` : "Loading ORB data…")
              : orb.phase === "pre-open" || orb.phase === "opening range" ? "Calls appear after the 09:15–09:30 opening range forms." : "No stock passed today's ORB filters."}
          </p>
        )}
      </div>

      <div className="insight-sec">
        <div className="insight-sub"><span>Market regime</span></div>
        <button type="button" className="regime-card" onClick={() => openStock("NIFTY 50")}>
          <span className="watch-top">
            <Logo symbol="NIFTY 50" size={30} />
            <span className="watch-id">
              <small>NSE · INDEX</small>
              <b>Nifty 50</b>
            </span>
            {riskOn !== null && <span className={`regime-tag ${riskOn ? "up" : "down"}`}>{riskOn ? "Risk-on" : "Risk-off"}</span>}
          </span>
          <span className="regime-line"><Sparkline values={(nifty?.series || []).slice(-60).map((p) => p[1])} height={70} /></span>
          <span className="regime-foot">
            <span><small>50 DMA</small><b className={nSum?.above_50dma ? "up" : "down"}>{nSum ? (nSum.above_50dma ? "Above" : "Below") : "–"}</b></span>
            <span><small>200 DMA</small><b className={nSum?.above_200dma ? "up" : "down"}>{nSum ? (nSum.above_200dma ? "Above" : "Below") : "–"}</b></span>
            <span><small>1M</small><b className={tone(nSum?.chg_1m_pct)}>{nSum ? `${signed(nSum.chg_1m_pct)}%` : "–"}</b></span>
          </span>
        </button>
      </div>

      <div className="insight-sec">
        <div className="insight-sub"><span>Equity sectors</span><small className="muted">by relative strength</small></div>
        <div className="sector-card">
          {sectors.length ? sectors.map((s) => (
            <div key={s.sector} className="sector-row">
              <div className="sector-top">
                <span>{shortSector(s.sector)}</span>
                <b className={tone(s.chg_1m_pct)}>{signed(s.chg_1m_pct)}%</b>
              </div>
              <div className={`sector-bar ${tone(s.chg_1m_pct)}`}>
                <i style={{ width: `${(Math.abs(s.chg_1m_pct || 0) / maxAbs) * 100}%` }} />
                <span className={`pill-chg ${tone(s.chg_1w_pct)}`}>1W {signed(s.chg_1w_pct)}%</span>
              </div>
              <small className="muted">{s.quadrant} · RS {num(s.rs_ratio, 1)}</small>
            </div>
          )) : <p className="muted small">Sector ranking appears after the daily scan.</p>}
        </div>
      </div>
    </aside>
  );
}
