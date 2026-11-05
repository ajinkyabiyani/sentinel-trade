import { useState } from "react";
import { api } from "../lib/api.js";
import { num, rupee, signed, tone } from "../lib/format.js";
import { Badge, Empty, ErrorNote, Spinner, useLoad } from "../components/ui.jsx";

// Improvement loop: per strategy, the four agents' latest run top-down (Backtester → Auditor → Quant → Code agent),
// the proposal waiting for your approval, and every change made so far with rollback. Paper only.
const R = (v) => (v == null ? "–" : `${signed(v, 2)}R`);
const P = (v) => (v == null ? "–" : `${num(v, 1)}%`);
const when = (iso) => (iso ? new Date(iso).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" }) : "–");
const STATUS = { ok: ["OK", ""], flag: ["Flagged", "sell"], propose: ["Proposal", "buy"], pass: ["Passed", "buy"], fail: ["Blocked", "sell"] };

function Agent({ n, name, role, status, children }) {
  const [label, t] = STATUS[status] || ["–", ""];
  return (
    <section className="card il-agent">
      <header className="card-head">
        <div className="il-agent-id">
          <span className="il-n">{n}</span>
          <div><h3>{name}</h3><small className="muted">{role}</small></div>
        </div>
        <Badge tone={t}>{label}</Badge>
      </header>
      {children}
    </section>
  );
}

function Strategy({ s, busy, act }) {
  const r = s.result;
  if (!r) {
    return (
      <section className="card"><header className="card-head"><h3>{s.label}</h3></header>
        <Empty>{s.status.running ? "The agents are running…" : "No run yet. Click Run now, or wait for tonight's run after the close."}</Empty>
        <ErrorNote error={s.status.error} />
      </section>
    );
  }
  const b = r.backtester, a = r.auditor, q = r.quant, c = r.code;
  const prop = q.proposal;
  const tgt = q.target?.target_exp_r ?? b.target_exp_r ?? 1;
  const allExp = r.baseline.all.exp_r;
  const propLabel = prop ? prop.label || `${prop.param}: ${prop.from} → ${prop.to}` : "";
  return (
    <div className="il-strat">
      <header className="il-head">
        <div>
          <h2>{s.label}</h2>
          <span className="muted small">Run {when(r.built)} · {r.sessions} cached sessions ({r.period[0]} – {r.period[1]}) · {r.seconds}s
            {Object.keys(s.live).length ? ` · live overrides: ${Object.entries(s.live).map(([k, v]) => `${k} ${v}`).join(", ")}` : ""}</span>
        </div>
        <button type="button" className="ghost small" disabled={busy || s.status.running} onClick={() => act(() => api.improveRun(s.key))}>
          {s.status.running ? "Running…" : "Run now"}
        </button>
      </header>
      <ErrorNote error={s.status.error} />
      <div className={`il-target ${allExp >= tgt ? "met" : ""}`}>
        <span>Expectancy target <b>{R(tgt)}</b> a trade after charges</span>
        <span>now <b className={tone(allExp)}>{R(allExp)}</b> over {r.baseline.all.trades} trades</span>
        <span>{q.target?.met_by?.length ? `reached by ${q.target.met_by.slice(0, 2).join(", ")}`
          : `best found: ${R(q.target?.best_is)} chosen-on, ${R(q.target?.best_oos)} unseen`}</span>
        <i style={{ width: `${Math.max(0, Math.min(100, ((allExp ?? 0) / tgt) * 100))}%` }} />
      </div>

      {prop && !s.dismissed && (
        <section className={`card il-proposal${c.status === "pass" ? "" : " blocked"}`}>
          <div>
            <small className="muted">Proposal waiting for you</small>
            <b>{propLabel}{prop.meets_target ? " · meets the +1R target" : ""}</b>
            <span className="muted small">{prop.why}</span>
            <span className="small">Unseen 30 sessions: {R(prop.oos.exp_r)} a trade vs {R(prop.base_oos.exp_r)} now · win rate {P(prop.oos.win_rate)} vs {P(prop.base_oos.win_rate)} · net {rupee(prop.oos.net)} vs {rupee(prop.base_oos.net)}</span>
          </div>
          <div className="il-actions">
            <button type="button" className="primary" disabled={busy || c.status !== "pass" || s.scheduled.length > 0}
              onClick={() => act(() => api.improveApprove(s.key))}>
              {s.scheduled.length ? "Scheduled" : "Approve"}
            </button>
            <button type="button" className="ghost" disabled={busy} onClick={() => act(() => api.improveDismiss(s.key))}>Dismiss</button>
            <small className="muted">Takes effect at the next pre-open, never mid-session.</small>
          </div>
        </section>
      )}
      {s.scheduled.map((p) => <p key={p.id} className="il-sched small">Scheduled: {p.param} {p.from} → {p.to} from {p.effective} (pre-open).</p>)}

      <div className="il-agents">
        <Agent n="1" name="Backtester" role="Replays the current rules; flags decay" status={b.status}>
          <table className="bt-split">
            <thead><tr><th /><th>60 sessions before</th><th>Last {r.sessions - 60}</th></tr></thead>
            <tbody>
              <tr><td>Trades</td><td>{b.prior.trades}</td><td>{b.recent.trades}</td></tr>
              <tr><td>Win rate</td><td>{P(b.prior.win_rate)}</td><td className={tone(b.d_win_rate)}>{P(b.recent.win_rate)}</td></tr>
              <tr><td>Expectancy</td><td>{R(b.prior.exp_r)}</td><td className={tone(b.d_exp_r)}>{R(b.recent.exp_r)}</td></tr>
              <tr><td>Net</td><td className={tone(b.prior.net)}>{rupee(b.prior.net)}</td><td className={tone(b.recent.net)}>{rupee(b.recent.net)}</td></tr>
            </tbody>
          </table>
          <p className="small">{b.flags.length ? b.flags.join("; ") : `No decay: win rate ${signed(b.d_win_rate, 1)} pts, expectancy ${R(b.d_exp_r)} vs the period before.`}</p>
        </Agent>
        <Agent n="2" name="Auditor" role="Slippage, charges and order size" status={a.status}>
          {a.trades ? (
            <>
              <ul className="il-drag">
                <li><span>Entry slippage</span><b className={a.gap_r >= 0.05 ? "down" : ""}>{R(a.gap_r)}</b></li>
                <li><span>Modelled slippage</span><b>{R(a.slip_r)}</b></li>
                <li><span>Charges</span><b>{R(a.cost_r)}</b></li>
                <li><span>Total drag a trade</span><b className="down">{R(-a.drag_r)}</b></li>
              </ul>
              {a.notes.map((x) => <p key={x} className="small">{x}</p>)}
              {a.recommend.map((x) => <p key={x.param} className="small il-rec">Recommend testing {x.param}: {x.why}.</p>)}
            </>
          ) : <p className="muted small">No trades to audit.</p>}
        </Agent>
        <Agent n="3" name="Quant" role="Walk-forward: choose on 60, judge on 30 unseen" status={q.status}>
          <p className="small">{q.verdict}</p>
          <div className="table-wrap">
            <table className="table il-tried">
              <thead><tr><th>Change</th><th className="num">Chosen-on R</th><th className="num">Unseen R</th><th className="num">Unseen trades</th></tr></thead>
              <tbody>
                <tr className="muted"><td>current rules</td><td className="num">{R(r.baseline.is.exp_r)}</td><td className="num">{R(r.baseline.oos.exp_r)}</td><td className="num">{r.baseline.oos.trades}</td></tr>
                {q.tried.map((t) => {
                  const label = t.label || `${t.param} ${t.from} → ${t.to}`;
                  return (
                  <tr key={label} className={prop && propLabel === label ? "sel" : (t.is_exp_r ?? -9) >= tgt && (t.oos_exp_r ?? -9) >= tgt ? "hit" : ""}>
                    <td>{label}</td>
                    <td className={`num ${tone((t.is_exp_r ?? 0) - (r.baseline.is.exp_r ?? 0))}`}>{R(t.is_exp_r)}</td>
                    <td className={`num ${tone((t.oos_exp_r ?? 0) - (r.baseline.oos.exp_r ?? 0))}`}>{R(t.oos_exp_r)}</td>
                    <td className="num">{t.oos_trades}</td>
                  </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Agent>
        <Agent n="4" name="Code agent" role="Safety gate before anything goes live" status={c.status}>
          <ul className="checks">
            {c.checks.map((x) => <li key={x.name} className={x.ok ? "ok" : "no"}><i>{x.ok ? "✓" : "✗"}</i>{x.name} <small className="muted">· {x.detail}</small></li>)}
          </ul>
        </Agent>
      </div>
    </div>
  );
}

export default function Improve() {
  const q = useLoad(api.improve, [], 5000);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const act = async (fn) => {
    setBusy(true);
    setErr("");
    try { await fn(); await q.reload(true); } catch (e) { setErr(e.message); } finally { setBusy(false); }
  };
  const d = q.data;
  return (
    <div className="il fade-in">
      <section className="card il-intro">
        <p className="small">
          Every weekday at {d?.nightly || "16:10"}, four agents check each strategy on its cached history. The <b>Backtester</b> flags decay. The <b>Auditor</b> measures slippage and charges.
          The <b>Quant</b> tries one-parameter changes, choosing on 60 sessions and judging on the 30 unseen ones. The <b>Code agent</b> blocks anything unsafe.
          Nothing changes until you approve it, and approved changes start at the next pre-open. Paper only: no order is ever sent.
        </p>
        <button type="button" className="primary" disabled={busy || d?.strategies.some((s) => s.status.running)} onClick={() => act(() => api.improveRun())}>Run all now</button>
      </section>
      <ErrorNote error={err || q.error} />
      {!d ? <div className="chart-empty" style={{ height: 200 }}><Spinner /></div>
        : d.strategies.map((s) => <Strategy key={s.key} s={s} busy={busy} act={act} />)}
      <section className="card">
        <header className="card-head"><h3>Change history</h3><span className="muted small">every approval, with rollback</span></header>
        {d?.history.length ? (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Strategy</th><th>Change</th><th>Approved</th><th>Effective</th><th>Status</th><th /></tr></thead>
              <tbody>
                {d.history.map((h) => (
                  <tr key={h.id}>
                    <td>{h.strategy.toUpperCase()}</td>
                    <td>{h.param} {h.from} → {h.to}{h.rollback_of ? " (rollback)" : ""}</td>
                    <td>{when(h.approved_at)}</td>
                    <td>{h.effective}</td>
                    <td><Badge tone={h.status === "live" ? "buy" : ""}>{h.status}</Badge></td>
                    <td>{(h.status === "live" || h.status === "scheduled") && !h.rollback_of && (
                      <button type="button" className="linkish small" disabled={busy} onClick={() => act(() => api.improveRollback(h.id))}>
                        {h.status === "live" ? "Roll back" : "Cancel"}
                      </button>
                    )}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <Empty>No change approved yet.</Empty>}
      </section>
    </div>
  );
}
