import { useState } from "react";
import { api } from "../lib/api.js";
import { num, rupee, signed, tone } from "../lib/format.js";
import { useTicks } from "../lib/ticks.js";
import { Badge, Empty, ErrorNote, Spinner, useLoad } from "../components/ui.jsx";
import { WithLogo } from "../components/Logo.jsx";
import AgentTerminal from "../components/AgentTerminal.jsx";

// Claude Skill Desk: Claude trades the India Quant Desk skill pack (the Everything Claude Trading plugin, rewritten for NSE and SEBI)
// on paper. Code finds the setups and checks every answer. Intraday options, swing options and cash buys. Nothing here sends an order.

const KIND = (e) => `${e.vehicle === "option" ? "Option" : "Shares"} · ${e.horizon}`;

function livePnl(e, ticks) {
  if (e.status === "closed") return { px: e.exit, pnl: e.net };
  const px = ticks[e.key]?.last_price ?? e.ltp;
  return { px, pnl: px == null ? null : (px * (1 - (e.slip || 0)) - e.entry) * e.qty };
}

export default function ClaudeSkillDesk() {
  const [day, setDay] = useState(null);
  const q = useLoad(() => api.skillDesk(day), [day], 15_000);
  const d = q.data;
  const held = d?.positions || [];
  const { ticks, live } = useTicks(held.filter((e) => e.status === "open").map((e) => e.key));
  if (q.loading && !d) return <section className="card orb-loading"><Spinner /></section>;
  if (!d) return <ErrorNote error={q.error} />;

  const marks = held.map((e) => ({ e, ...livePnl(e, ticks) }));
  return (
    <div className="tt-page">
      <AgentTerminal id="skill_desk" />
      {!d.signed_in && <ErrorNote error="Claude isn't signed in on this computer. Open the Claude app and sign in." />}
      {!d.skill_ready && <ErrorNote error="The india-quant-desk skill pack is missing from .claude/skills." />}

      <section className="card">
        <header className="card-head">
          <h3>Positions</h3>
          <label className="jd-inline"><span className="muted small">Date</span>
            <select aria-label="Session date" value={d.day} onChange={(e) => setDay(e.target.value)}>
              {[...new Set([d.today, d.day, ...(d.sessions || [])])].map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </label>
        </header>
        {held.length ? (
          <div className="table-wrap">
            <table className="table pd-table">
              <thead><tr><th>Instrument</th><th>Kind</th><th>In</th><th className="num">Qty</th><th className="num">Entry</th><th className="num">Stop / target</th><th className="num">LTP / exit</th><th className="num">P&L</th><th>Status</th></tr></thead>
              <tbody>
                {marks.map(({ e, px, pnl }) => (
                  <tr key={e.id}>
                    <td><WithLogo symbol={e.underlying}><b>{e.instrument}</b></WithLogo></td>
                    <td className="small">{KIND(e)}{e.horizon === "swing" && e.status === "open" ? ` · day ${e.sessions + 1}` : ""}</td>
                    <td className="small">{e.day} {e.entry_at}{e.exit_at ? ` → ${e.exit_day === e.day ? "" : `${e.exit_day} `}${e.exit_at}` : ""}</td>
                    <td className="num">{e.qty}{e.lots ? <span className="muted small"> ({e.lots} lot{e.lots === 1 ? "" : "s"})</span> : null}</td>
                    <td className="num">{num(e.entry)}</td>
                    <td className="num small">{num(e.stop)} / {num(e.target)}</td>
                    <td className="num">{num(px)}</td>
                    <td className="num"><b className={tone(pnl)}>{pnl == null ? "–" : rupee(pnl)}</b>{e.r != null && <span className="muted small"> {signed(e.r)}R</span>}</td>
                    <td>{e.status === "open" ? <Badge tone="accent">Open{live ? " · live" : ""}</Badge> : <Badge tone={(e.net || 0) >= 0 ? "buy" : "sell"}>{e.exit_reason}</Badge>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <Empty>No paper trade on this date.</Empty>}
        <p className="muted small">Fills are at the live price plus slippage. Closed trades are after charges; open ones are marked on live ticks before exit charges.</p>
      </section>



      <section className="card">
        <header className="card-head"><h3>Rules the code enforces</h3><span className="muted small">Skill pack: {d.skill.path} · {d.skill.references.length} references</span></header>
        <ul className="small" style={{ margin: 0, paddingLeft: 18, lineHeight: 1.7 }}>{d.rules.map((x) => <li key={x}>{x}</li>)}</ul>
      </section>

      <p className="muted small tt-note">Adapted from Everything Claude Trading (MIT licence).</p>
      <ErrorNote error={q.error} />
    </div>
  );
}
