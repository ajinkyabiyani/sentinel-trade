import { useEffect, useState } from "react";
import { api } from "../lib/api.js";
import { inr, num, rupee, signed, tone } from "../lib/format.js";
import { useLoad } from "./ui.jsx";
import { Logo } from "./Logo.jsx";

// A strategy's live terminal. Everything shown is the strategy's real state from /api/live-terminal/<id>, polled quickly while the
// market is open. Motion happens only when that state changes: a new log line slides in, the token moves to the agent now at work,
// the active agent's card glows while it works. After the close the terminal says it has stopped and nothing moves.

const ACTION = { ENTER: "buy", EXIT: "accent", FILTERED: "sell", SKIP: "muted", ERROR: "sell" };
const SHORT = (id) => id.replace("timemachine", "time m.").replace("portfolio", "PM").slice(0, 9);

function useClock(on) {
  const [t, setT] = useState(() => new Date());
  useEffect(() => {
    if (!on) return undefined;
    const i = setInterval(() => setT(new Date()), 1000);
    return () => clearInterval(i);
  }, [on]);
  return t;
}

function Flow({ f, stages, agents, live }) {
  if (!f) return <p className="lt-empty">No candidate has reached the team yet{live ? " today" : " in the last session"}.</p>;
  const at = Math.max(0, stages.indexOf(f.at));
  const label = Object.fromEntries(agents.map((a) => [a.id, a.label]));
  const pct = stages.length > 1 ? (at / (stages.length - 1)) * 100 : 0;
  return (
    <div className={`lt-flow ${f.verdict || "idle"}`}>
      <div className="lt-rail">
        <span className="lt-rail-fill" style={{ width: `${pct}%` }} />
        {stages.map((s, i) => (
          <span key={s} className={`lt-node ${i < at ? "passed" : i === at ? `here ${f.verdict || ""}` : ""}`} style={{ left: `${stages.length > 1 ? (i / (stages.length - 1)) * 100 : 0}%` }} title={label[s]}>
            <i />
            <em>{SHORT(label[s] || s)}</em>
          </span>
        ))}
        <span className={`lt-token ${f.verdict || ""}`} style={{ left: `${pct}%` }}>
          <Logo symbol={f.symbol || ""} size={18} /><b>{f.symbol}</b>
          {f.verdict === "filtered" && <small>FILTERED</small>}
          {f.verdict === "pass" && <small>ENTERED</small>}
        </span>
      </div>
      {f.why && <p className="lt-why">{f.why}</p>}
    </div>
  );
}

export default function AgentTerminal({ id }) {
  const [fast, setFast] = useState(false);
  const q = useLoad(() => api.liveTerminal(id), [id, fast], fast ? 1_500 : 20_000);
  const d = q.data;
  const live = Boolean(d?.phase?.live);
  useEffect(() => { setFast(live); }, [live]);
  const clock = useClock(live);
  if (!d) return <section className="lt lt-loading"><span className="lt-dot" /> {q.error || "Connecting to the desk…"}</section>;
  const pnl = d.balance.now - d.balance.start;
  const stale = d.session !== d.today;
  return (
    <section className={`lt ${live ? "is-live" : "is-stopped"}`} aria-label={`${d.name} live terminal`}>
      <header className="lt-head">
        <div className="lt-id">
          <span className={`lt-mark ${live ? "on" : ""}`}><i /><i /></span>
          <div>
            <div className="lt-title"><b>{d.name}</b><span>terminal</span></div>
            <div className="lt-status">
              <span className={`lt-dot ${live ? "on" : ""}`} />
              <b>{live ? "running" : "stopped"}</b>
              <span>{live ? clock.toLocaleTimeString("en-IN", { hour12: false }) : `${d.phase.label} · ${d.phase.next}`}</span>
              {stale && <span className="lt-chip">showing {d.session}</span>}
            </div>
            {d.now_doing ? <div className="lt-doing"><span>{d.now_doing.agent}</span>{d.now_doing.text}</div>
              : <div className="lt-doing quiet">{live ? "waiting for the next candle" : "every trade gets a reason"}</div>}
          </div>
        </div>
        <div className="lt-kpis">
          <div className="lt-kpi big"><b>{inr(d.balance.now, 0)}</b><small>balance · <span className={tone(pnl)}>{signed(pnl, 0)}</span></small></div>
          <div className="lt-kpi"><b>{d.trades.total}</b><small>trades</small></div>
          <div className="lt-kpi"><b className="up">{d.trades.won}</b><small>wins</small></div>
          <div className="lt-kpi"><b className="down">{d.trades.lost}</b><small>losses</small></div>
        </div>
      </header>

      <div className="lt-body">
        <aside className="lt-agents">
          <div className="lt-label"><span>Agents</span><em>{d.agents.length} online</em></div>
          {d.agents.map((a) => (
            <div key={a.id} className={`lt-agent ${a.state}`}>
              <div className="lt-agent-top"><b>{a.label}</b><span className={`lt-badge ${a.state}`}>{a.badge}</span></div>
              <small className="lt-job">{a.job}</small>
              <span className="lt-line">{a.line}</span>
            </div>
          ))}
        </aside>

        <div className="lt-main">
          <div className="lt-panel">
            <div className="lt-label"><span>Live flow · {d.stages.map((s) => SHORT(d.agents.find((a) => a.id === s)?.label || s)).join(" → ")}</span><em>{d.candidates.length} in view</em></div>
            {d.candidates.length > 0 && (
              <div className="lt-cands">
                {d.candidates.slice(-10).map((c, i) => (
                  <span key={c.symbol + i} className={`lt-cand ${c.state}`} title={c.note}>
                    <Logo symbol={c.symbol} size={16} /><b>{c.symbol}</b><small>{c.note || c.state}</small>
                  </span>
                ))}
              </div>
            )}
            <Flow f={d.flow} stages={d.stages} agents={d.agents} live={live} />
          </div>

          <div className="lt-split">
            <div className="lt-panel">
              <div className="lt-label"><span>Trade · entry vs {d.trade?.status === "open" ? "now" : "exit"}</span>{d.trade && <em className={d.trade.status === "open" ? "on" : ""}>{d.trade.status}</em>}</div>
              {d.trade ? (
                <div className="lt-trade">
                  <div className="lt-trade-top"><Logo symbol={d.trade.symbol} size={22} /><b>{d.trade.symbol}</b>{d.trade.pnl_pct != null && <span className={tone(d.trade.pnl_pct)}>{signed(d.trade.pnl_pct)}%</span>}</div>
                  <dl>
                    <dt>entry</dt><dd>{num(d.trade.entry)}</dd>
                    <dt>{d.trade.status === "open" ? "now" : "exit"}</dt><dd>{num(d.trade.now)}</dd>
                    <dt>stop / target</dt><dd>{num(d.trade.stop)} / {num(d.trade.target)}</dd>
                    <dt>qty</dt><dd>{d.trade.qty}</dd>
                    <dt>p&amp;l</dt><dd className={tone(d.trade.pnl)}>{d.trade.pnl == null ? "–" : rupee(d.trade.pnl)}</dd>
                  </dl>
                  {d.trade.reason && <p className="lt-reason">reason: {d.trade.reason}</p>}
                </div>
              ) : <p className="lt-empty">No trade {live ? "open" : "in this session"}.</p>}
            </div>
            <div className="lt-panel lt-tail">
              <div className="lt-label"><span>$ tail -f {d.id}.log</span><em>{d.tail.length} lines</em></div>
              <ol>
                {d.tail.map((l, i) => <li key={`${l.at}|${l.text}|${i}`} className={l.kind}><time>{l.at}</time>{l.text}</li>)}
                {live && <li className="lt-cursor"><span /></li>}
              </ol>
            </div>
          </div>

          <div className="lt-panel">
            <div className="lt-label"><span>Log</span><em>{d.log.length} events · {d.session}</em></div>
            {d.log.length ? (
              <ul className="lt-log">
                {d.log.map((r, i) => (
                  <li key={`${r.at}|${r.symbol}|${r.action}|${i}`}>
                    <time>{r.at}</time>
                    <span className={`lt-act ${ACTION[r.action] || ""}`}>{r.action}</span>
                    <b>{r.symbol}</b>
                    <span className="lt-reason">{r.reason ? `reason: ${r.reason}` : ""}</span>
                    <span className={`lt-val ${tone(r.value)}`}>{r.value == null ? "" : rupee(r.value)}</span>
                  </li>
                ))}
              </ul>
            ) : <p className="lt-empty">Nothing logged yet.</p>}
          </div>
        </div>
      </div>
    </section>
  );
}
