import { useState } from "react";
import { api } from "../lib/api.js";
import { num, rupee, signed, tone } from "../lib/format.js";
import { useTicks } from "../lib/ticks.js";
import { Badge, Empty, ErrorNote, Spinner, useLoad } from "../components/ui.jsx";
import { WithLogo } from "../components/Logo.jsx";
import AgentTerminal from "../components/AgentTerminal.jsx";

// Multi Agent Team: fully automatic. After each 5-minute candle the Scanner checks the charts, and each setup that passes goes to
// Technical, Risk and Trader on Claude, then the code check. The live terminal at the top shows the team at work; below are the
// paper trades. Paper only: nothing here sends an order.

function Shadow({ e }) {
  const x = e.shadow;
  if (!x) return <span className="muted">–</span>;
  if (x.status === "none") return <span className="muted" title={x.why}>n/a</span>;
  if (x.status !== "closed") return <span className="muted" title={`ATR ${num(x.atr)}`}>following{x.level != null ? ` · trail ${num(x.level)}` : ""}</span>;
  const diff = (x.net || 0) - (e.status === "closed" ? e.net || 0 : 0);
  return (
    <span title={`${x.exit_reason} at ${x.exit_at} · ATR ${num(x.atr)}`}>
      <b className={tone(x.net)}>{rupee(x.net)}</b>
      <span className="muted"> {x.exit_reason}</span>
      {e.status === "closed" && Math.abs(diff) >= 1 && <span className={tone(diff)}> ({rupee(diff)})</span>}
    </span>
  );
}

function WhatIf({ day, all }) {
  if (!day && !all) return null;
  const line = (w) => `${w.trades} trade${w.trades === 1 ? "" : "s"}: actual ${rupee(w.actual)} against ${rupee(w.trail)} with the trail (${w.better} better, ${w.worse} worse)`;
  return (
    <div className="mt-whatif">
      <b>ATR trail what-if</b>
      <span className="muted small">{day?.rule}. The real trades are never changed by it.</span>
      {day?.trades ? <span>This day, {line(day)}.</span> : <span className="muted">No finished trade on this day to compare yet.</span>}
      {all?.trades ? <span>All {all.days} day{all.days === 1 ? "" : "s"} so far, {line(all)}.{all.trades < 50 && <span className="muted"> Too few trades to judge yet: about 50 are needed.</span>}</span> : null}
    </div>
  );
}

function livePnl(e, ticks, slip) {
  if (e.status === "closed") return { px: e.exit, pnl: e.net };
  const px = ticks[e.key]?.last_price ?? e.ltp;
  const way = e.side === "SHORT" ? -1 : 1;
  return { px, pnl: px == null ? null : (px * (1 - way * slip) - e.entry) * e.qty * way };
}

// ---------------------------------------------------------------- the crew

// ---------------------------------------------------------------- the page

export default function MultiAgentTeam() {
  const [day, setDay] = useState(null);
  const q = useLoad(() => api.multiAgentTeam(day), [day], 5_000);
  const d = q.data;
  const held = d?.book?.positions || [];
  const { ticks, live } = useTicks(held.filter((e) => e.status === "open").map((e) => e.key));
  if (q.loading && !d) return <section className="card orb-loading"><Spinner /></section>;
  if (!d) return <ErrorNote error={q.error} />;

  const b = d.book;
  const slipOf = (e) => (e.kind === "cash" ? b.slip.cash : e.kind === "stock" ? b.slip.stock : b.slip.index);
  const marks = held.map((e) => ({ e, ...livePnl(e, ticks, slipOf(e)) }));
  return (
    <div className="tt-page">
      <AgentTerminal id="multi_agent_team" />
      {!d.signed_in && <ErrorNote error="Claude isn't signed in on this computer. Open the Claude app and sign in." />}
      {!d.settings_ready && <ErrorNote error="Set your paper capital and risk under Team risk settings on the Portfolio & Risk page first." />}


      <section className="card">
        <header className="card-head">
          <h3>Paper trades</h3>
          <label className="jd-inline"><span className="muted small">Date</span>
            <select aria-label="Session date" value={d.day} onChange={(e) => setDay(e.target.value)}>
              {[...new Set([d.today, d.day, ...(d.sessions || [])])].map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </label>
        </header>
        {held.length ? (
          <div className="table-wrap">
            <table className="table pd-table">
              <thead><tr><th>Contract</th><th>In</th><th className="num">Qty</th><th className="num">Entry</th><th className="num">Stop / target</th><th className="num">LTP / exit</th><th className="num">P&L</th><th>Status</th><th className="num" title="What the same trade would have made with an ATR trailing exit added. It never changes the real trade.">ATR trail (what-if)</th></tr></thead>
              <tbody>
                {marks.map(({ e, px, pnl }) => (
                  <tr key={e.id}>
                    <td><WithLogo symbol={e.underlying || e.instrument}><b>{e.instrument}</b></WithLogo> <span className="muted small">{e.underlying}</span></td>
                    <td className="small">{e.entry_at}{e.exit_at ? ` → ${e.exit_at}` : ""}</td>
                    <td className="num">{e.qty}</td>
                    <td className="num">{num(e.entry)}</td>
                    <td className="num small">{num(e.stop)} / {num(e.target)}</td>
                    <td className="num">{num(px)}</td>
                    <td className="num"><b className={tone(pnl)}>{pnl == null ? "–" : rupee(pnl)}</b>{e.r != null && <span className="muted small"> {signed(e.r)}R</span>}</td>
                    <td>{e.status === "open" ? <Badge tone="accent">Open{live ? " · live" : ""}</Badge> : <Badge tone={(e.net || 0) >= 0 ? "buy" : "sell"}>{e.exit_reason}</Badge>}</td>
                    <td className="num small"><Shadow e={e} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <Empty>No paper trade on this date yet.</Empty>}
        <WhatIf day={b.what_if} all={d.what_if_all} />
        <p className="muted small">Entry is the live price when the team finished, plus slippage. Closed trades are after charges, and open ones are marked on live ticks before exit charges.</p>
      </section>

      <p className="muted small tt-note">{"Code checks can turn any BUY into a HOLD: India VIX under 12, a strike over one step from at the money, no liquidity, or a loss over your limit."}</p>
      <ErrorNote error={q.error} />
    </div>
  );
}
