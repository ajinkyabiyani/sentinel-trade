import { useState } from "react";
import { api } from "../lib/api.js";
import { num, rupee, signed, tone } from "../lib/format.js";
import { useTicks } from "../lib/ticks.js";
import { Badge, Empty, ErrorNote, Spinner, Stat, useLoad } from "../components/ui.jsx";
import { WithLogo } from "../components/Logo.jsx";

// Gap Momentum: one ATM stock option a day in the direction of the biggest opening gap, only when Nifty is trending.
// Paper only (real quotes, simulated fills). Telegram gets the booked entry and the exit. Nothing here sends an order.

const hm = (iso) => (iso ? String(iso).slice(11, 16) : "");
const exp = (d) => (d ? new Date(`${d}T00:00:00`).toLocaleDateString("en-IN", { day: "2-digit", month: "short" }) : "");
const contract = (p) => `${p.symbol} ${num(p.strike, 0)} ${p.side} · ${exp(p.expiry)}`;
const REGIME = { UP: ["Trending up", "buy"], DOWN: ["Trending down", "sell"], SIDEWAYS: ["Sideways · no trade", ""], UNKNOWN: ["Not enough data", ""] };

export default function GapMomentum() {
  const [day, setDay] = useState(null);
  const q = useLoad(() => api.gapMomentum(day), [day], 10_000);
  const d = q.data;
  const pos = d?.position;
  const { ticks, live } = useTicks(pos ? [pos.key] : []);
  if (q.loading && !d) return <section className="card orb-loading"><Spinner /></section>;
  if (!d) return <ErrorNote error={q.error} />;

  const ltp = pos ? ticks[pos.key]?.last_price ?? null : null;
  const openPnl = pos && ltp != null ? (ltp - pos.entry) * pos.qty : null;
  const [regimeText, regimeTone] = REGIME[d.regime] || ["Waiting for 09:00", ""];
  const s = d.stats;

  return (
    <div className="tt-page">
      {s.halted && <ErrorNote error="Kill switch is on: realized P&L fell ₹15,000 below its peak. No new trades until 'halted' is reset in .orb_cache/gap_momentum/ledger.json." />}

      <section className="card">
        <header className="card-head">
          <h3>Today</h3>
          <label className="jd-inline"><span className="muted small">Date</span>
            <select aria-label="Session date" value={d.day} onChange={(e) => setDay(e.target.value)}>
              {[...new Set([d.today, d.day, ...(d.sessions || [])])].map((x) => <option key={x} value={x}>{x}</option>)}
            </select>
          </label>
        </header>
        <div className="stats">
          <Stat label="Nifty regime" value={<Badge tone={regimeTone}>{regimeText}</Badge>} sub="Nifty 50 vs 20-day average" />
          <Stat label="Data" value="Kite" sub={d.last_session ? `Last session ${d.last_session}` : "Read at 09:00"} />
          <Stat label="Booked P&L (all days)" value={rupee(s.net)} tone={tone(s.net)} sub={`${s.trades} trade${s.trades === 1 ? "" : "s"}${s.win_pct != null ? ` · ${s.win_pct}% won` : ""}`} />
          <Stat label="Telegram" value={d.telegram ? "On" : "Off"} sub="Booked entry and exit only" />
        </div>
      </section>

      <section className="card">
        <header className="card-head"><h3>Position</h3>{pos && <Badge tone="accent">Open{live ? " · live" : ""}</Badge>}</header>
        {pos ? (
          <div className="table-wrap">
            <table className="table pd-table">
              <thead><tr><th>Contract</th><th>In</th><th className="num">Qty</th><th className="num">Entry</th><th className="num">Stop (SL)</th><th className="num">1R level</th><th className="num">LTP</th><th className="num">P&L</th><th>Exit</th></tr></thead>
              <tbody>
                <tr>
                  <td><WithLogo symbol={pos.symbol}><b>{contract(pos)}</b></WithLogo><div className="muted small">Gap {signed(pos.gap_atr)} ATR · {pos.side === "CE" ? "buy call" : "buy put"}</div></td>
                  <td className="small">{hm(pos.entry_time)}</td>
                  <td className="num">{pos.qty}<span className="muted small"> ({pos.lots} lot{pos.lots === 1 ? "" : "s"})</span></td>
                  <td className="num">{num(pos.entry)}</td>
                  <td className="num">{num(pos.trigger)}<div className="muted small">max loss {rupee(-pos.max_loss)}</div></td>
                  <td className="num muted">{num(pos.ref_1r)}</td>
                  <td className="num">{num(ltp)}</td>
                  <td className="num"><b className={tone(openPnl)}>{rupee(openPnl)}</b></td>
                  <td className="small">15:15 or stop</td>
                </tr>
              </tbody>
            </table>
          </div>
        ) : <Empty>{d.day_trades.length ? "Today's trade is closed (below)." : d.done ? "No trade today." : "No position. The strategy looks for a gap at 09:15:20 and stops trying at 09:25."}</Empty>}
        <p className="muted small">No profit target, as in the backtest: the trade holds to 15:15 unless the stop is hit. The 1R level is for reference only. Open P&L is before exit charges.</p>
      </section>

      <section className="card">
        <header className="card-head"><h3>Gaps at the open</h3><span className="muted small">Top {d.universe.length || 15} stocks by option turnover · threshold ±0.30 ATR</span></header>
        {d.signals.length ? (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Stock</th><th className="num">Prev close</th><th className="num">Futures open</th><th className="num">Gap (ATR)</th><th>Side</th></tr></thead>
              <tbody>
                {d.signals.map((g) => (
                  <tr key={g.symbol}>
                    <td><WithLogo symbol={g.symbol}><b>{g.symbol}</b></WithLogo></td>
                    <td className="num">{num(g.prev_close)}</td>
                    <td className="num">{num(g.fut_open)}</td>
                    <td className={`num ${tone(g.gap_atr)}`}>{signed(g.gap_atr)}</td>
                    <td><Badge tone={g.side === "CE" ? "buy" : "sell"}>{g.side === "CE" ? "Call" : "Put"}</Badge></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <Empty>{d.universe.length ? "No stock gapped past ±0.30 ATR, or the open hasn't come yet." : "The universe is built at 09:00 on a trending day."}</Empty>}
      </section>

      <section className="card">
        <header className="card-head"><h3>Booked trades</h3></header>
        {d.trades.length ? (
          <div className="table-wrap">
            <table className="table pd-table">
              <thead><tr><th>Date</th><th>Contract</th><th className="num">Qty</th><th className="num">Entry</th><th className="num">SL</th><th className="num">Exit</th><th className="num">Charges</th><th className="num">P&L</th><th>Reason</th></tr></thead>
              <tbody>
                {d.trades.map((t) => (
                  <tr key={`${t.date}${t.tradingsymbol}`}>
                    <td className="small">{t.date.slice(0, 4)}-{t.date.slice(4, 6)}-{t.date.slice(6)} {hm(t.entry_time)} → {hm(t.exit_time)}</td>
                    <td><WithLogo symbol={t.symbol}><b>{contract(t)}</b></WithLogo></td>
                    <td className="num">{t.qty}</td>
                    <td className="num">{num(t.entry)}</td>
                    <td className="num">{num(t.trigger)}</td>
                    <td className="num">{num(t.exit)}</td>
                    <td className="num muted">{num(t.charges)}</td>
                    <td className="num"><b className={tone(t.pnl)}>{rupee(t.pnl)}</b>{t.r != null && <span className="muted small"> {signed(t.r)}R</span>}</td>
                    <td><Badge tone={t.pnl >= 0 ? "buy" : "sell"}>{t.reason === "STOP" ? "Stop" : "15:15 exit"}</Badge></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <Empty>No booked trades yet.</Empty>}
      </section>

      <section className="card">
        <header className="card-head"><h3>Log</h3><span className="muted small">{d.day}</span></header>
        {d.events.length ? (
          <ul className="small" style={{ margin: 0, paddingLeft: 18, lineHeight: 1.7 }}>
            {[...d.events].reverse().map((e, i) => <li key={i}><span className="muted">{hm(e.t)}</span> {e.msg}</li>)}
          </ul>
        ) : <Empty>Nothing yet. The strategy starts at 09:00 on weekdays once Kite is signed in.</Empty>}
      </section>

      <section className="card">
        <header className="card-head"><h3>Rules the code enforces</h3><span className="muted small">₹{num(d.risk, 0)} risk a trade · paper</span></header>
        <ul className="small" style={{ margin: 0, paddingLeft: 18, lineHeight: 1.7 }}>{d.rules.map((x) => <li key={x}>{x}</li>)}</ul>
      </section>
      <ErrorNote error={q.error} />
    </div>
  );
}
