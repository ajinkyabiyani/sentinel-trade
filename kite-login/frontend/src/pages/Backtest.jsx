import { useEffect, useState } from "react";
import { api } from "../lib/api.js";
import { inr, num, rupee, signed, tone } from "../lib/format.js";
import { Badge, DataTable, Empty, ErrorNote, Segmented, Spinner, Stat, useLoad } from "../components/ui.jsx";

// Strategy lab: pick a strategy, set capital and period, run it on Kite history, read the result top-down
// (verdict, key numbers, equity curve, then where the money was made and lost). Historical data only.
const STRATS = [
  { id: "orb", name: "ORB Strategy", kind: "Intraday · F&O stocks",
    desc: "The live ORB bot replayed on past sessions of 5-minute candles: same filters, sizing, costs and daily stops." },
  { id: "darvas", name: "Darvas Box (quant)", kind: "Swing · daily candles",
    desc: "Fixed quant rules (nothing fitted): 10-day box ≤ 12–15% within 5% of the 52-week high, close above on 1.5× volume, stop-limit 0.05% over the breakout high, box-bottom stop (7% cap) trailed up box by box; new buys only while Nifty is above its 50-day average." },
];
const NOUN = { orb: "sessions" };

const pct = (v, d = 1) => (v == null ? "–" : `${num(v * 100, d)}%`);
const day = (iso) => new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "2-digit" });

export function EquityChart({ points, base, height = 220 }) {
  const w = 1000;
  if (!points || points.length < 2) return <Empty>The curve needs at least two days of results.</Empty>;
  const v = points.map((p) => p[1]);
  const lo = Math.min(base ?? v[0], ...v), hi = Math.max(base ?? v[0], ...v);
  const pad = (hi - lo) * 0.08 || 1;
  const y = (x) => 10 + (1 - (x - lo + pad) / (hi - lo + 2 * pad)) * (height - 34);
  const x = (i) => (i / (points.length - 1)) * (w - 70);
  const line = v.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p).toFixed(1)}`).join("");
  const up = v.at(-1) >= (base ?? v[0]);
  const ticks = [lo, (lo + hi) / 2, hi];
  const marks = [0, Math.floor(points.length / 3), Math.floor((2 * points.length) / 3), points.length - 1];
  return (
    <svg className={`bt-chart ${up ? "up" : "down"}`} viewBox={`0 0 ${w} ${height}`} preserveAspectRatio="none" role="img" aria-label="Equity curve">
      {ticks.map((t) => (
        <g key={t}>
          <line x1="0" x2={w - 70} y1={y(t)} y2={y(t)} className="sc-grid" />
          <text x={w - 64} y={y(t) + 4} className="axis">{num(t, 0)}</text>
        </g>
      ))}
      {base != null && <line x1="0" x2={w - 70} y1={y(base)} y2={y(base)} className="bt-base" />}
      <path d={`${line}L${x(v.length - 1)},${height - 24}L0,${height - 24}Z`} className="bt-area" />
      <path d={line} className="bt-line" />
      {marks.map((i, k) => (
        <text key={k} x={x(i)} y={height - 6} className="axis" textAnchor={k === 0 ? "start" : k === 3 ? "end" : "middle"}>{day(points[i][0])}</text>
      ))}
    </svg>
  );
}

function Progress({ running, done, total, stage, error }) {
  if (!running && !error) return null;
  const p = total ? Math.round((done / total) * 100) : null;
  return (
    <div className={`bt-progress${error ? " err" : ""}`}>
      {running && <Spinner small />}
      <span>{error || stage || "Working"}{running && total ? ` · ${done}/${total}` : ""}</span>
      {running && <i style={{ width: `${p ?? 5}%` }} />}
    </div>
  );
}

// The user's targets (Nov 2026): every result is graded against them
const TARGETS = [
  ["win_rate", "Win rate", (v) => `${num(v, 1)}%`, (v) => v >= 55, "55%+"],
  ["profit_factor", "Profit factor (net)", (v) => num(v), (v) => v >= 1.8, "1.8+"],
  ["max_dd_pct", "Max drawdown", (v) => `${num(v, 1)}%`, (v) => v <= 12, "12% or less"],
  ["avg_rr", "Average reward : risk", (v) => `1 : ${num(v, 2)}`, (v) => v >= 3, "1 : 3+"],
  ["sharpe", "Sharpe ratio", (v) => num(v), (v) => v >= 1.4, "1.4+"],
  ["calmar", "Calmar ratio", (v) => num(v), (v) => v >= 2, "2+"],
];

function Scorecard({ sc }) {
  if (!sc) return null;
  const met = TARGETS.filter(([k, , , ok]) => sc[k] != null && ok(sc[k])).length;
  return (
    <section className="card bt-score">
      <header className="card-head"><h3>Targets</h3><span className={`muted small ${met >= 5 ? "up" : met >= 3 ? "amber" : "down"}`}>{met} of 6 met</span></header>
      <div className="bt-score-grid">
        {TARGETS.map(([k, label, fmt, ok, goal]) => {
          const v = sc[k];
          const good = v != null && ok(v);
          return (
            <div key={k} className={good ? "ok" : "no"}>
              <small>{label}</small>
              <b>{v == null ? "–" : fmt(v)}</b>
              <em>{good ? "✓" : "✗"} target {goal}</em>
            </div>
          );
        })}
      </div>
    </section>
  );
}

// Darvas results carry an equity curve rather than per-day P&L: derive the same six figures from it
function darvasScore(r) {
  const m = r.oos.metrics;
  const eq = r.oos.equity.map((x) => x[1]);
  const rets = eq.slice(1).map((v, i) => v / eq[i] - 1);
  const mu = rets.reduce((a, b) => a + b, 0) / Math.max(1, rets.length);
  const sd = Math.sqrt(rets.reduce((a, b) => a + (b - mu) ** 2, 0) / Math.max(1, rets.length - 1));
  const dd = Math.abs(m.max_drawdown || 0) * 100;
  return { win_rate: m.win_rate * 100, profit_factor: m.profit_factor, max_dd_pct: dd, avg_rr: m.payoff_ratio,
    sharpe: sd > 0 ? (mu / sd) * Math.sqrt(248) : null, calmar: dd > 0 ? (m.cagr * 100) / dd : null };
}

function Verdict({ tone: t, title, children }) {
  return (
    <div className={`bt-verdict ${t}`}>
      <b>{title}</b>
      <span>{children}</span>
    </div>
  );
}

// ---------------- ORB ----------------
function OrbForm({ busy, onRun }) {
  const [f, setF] = useState({ sessions: 60, capital: 100000, notional: 50000, mode: "3R", momentum: true });
  const set = (k) => (e) => setF({ ...f, [k]: e.target.type === "checkbox" ? e.target.checked : Number(e.target.value) });
  return (
    <form className="bt-form" onSubmit={(e) => { e.preventDefault(); onRun(f); }}>
      <label>Sessions to test
        <select value={f.sessions} onChange={set("sessions")}>
          {[20, 40, 60, 90, 120, 180, 250].map((n) => <option key={n} value={n}>{n} days (~{Math.round(n / 21)} mo)</option>)}
        </select>
      </label>
      <label>Capital (₹)<input type="number" min="10000" step="10000" value={f.capital} onChange={set("capital")} /></label>
      <label>Per trade (₹)<input type="number" min="1000" step="5000" value={f.notional} onChange={set("notional")} /></label>
      <label>Exit
        <Segmented value={f.mode} onChange={(mode) => setF({ ...f, mode })} size="sm" label="Exit mode"
          options={[{ id: "3R", label: "3R target" }, { id: "none", label: "Hold to 15:10" }]} />
      </label>
      <label className="bt-check"><input type="checkbox" checked={f.momentum} onChange={set("momentum")} /> Momentum check</label>
      <button type="submit" className="primary" disabled={busy}>{busy ? "Running…" : "Run backtest"}</button>
    </form>
  );
}

function TradeResult({ r, kind }) {
  const noun = NOUN[kind];
  const s = r.stats;
  const { early, late } = r.split;
  const good = s.net > 0 && (s.profit_factor ?? 0) >= 1.2;
  const consistent = early.net > 0 === late.net > 0;
  return (
    <>
      <Verdict tone={good ? "up" : s.net > 0 ? "amber" : "down"}
        title={good ? "Profitable after charges" : s.net > 0 ? "Marginally profitable" : "Lost money after charges"}>
        {rupee(s.net)} over {s.sessions} {noun} ({day(r.period[0])} – {day(r.period[1])}), {s.trades} trades, {signed(s.return_pct)}% on
        {" "}{inr(r.params.capital, 0)}. {consistent ? "Both halves of the period agree in direction." : "The earlier and later halves disagree: treat the edge as unstable."}
        {r.per_signal ? ` Every signal, before the position cap: ${r.per_signal.n} signals, ${r.per_signal.win_rate}% won, ${signed(r.per_signal.avg_r)}R on average after charges.` : ""}
      </Verdict>
      <Scorecard sc={r.scorecard} />
      <section className="paper-stats">
        <Stat label="Net P&L" value={rupee(s.net)} tone={tone(s.net)} sub={`${rupee(s.per_day)} a session · charges ${rupee(s.costs)}`} />
        <Stat label="Win rate" value={s.win_rate == null ? "–" : `${num(s.win_rate, 1)}%`} sub={`${s.wins} won · ${s.losses} lost`} />
        <Stat label="Profit factor" value={s.profit_factor == null ? "–" : num(s.profit_factor)} sub={`avg win ${rupee(s.avg_win)} · loss ${rupee(s.avg_loss)}`} />
        <Stat label="Average R" value={s.avg_r == null ? "–" : `${signed(s.avg_r)}R`} tone={tone(s.avg_r)} sub="per closed trade" />
        <Stat label="Max drawdown" value={rupee(s.max_drawdown)} tone={s.max_drawdown < 0 ? "down" : ""} sub={`best ${rupee(s.best_day)} · worst ${rupee(s.worst_day)}`} />
        <Stat label="Green days" value={`${s.green_days}/${s.traded_days}`} sub={`${s.sessions - s.traded_days} ${noun} with no trade`} />
      </section>
      <section className="card">
        <header className="card-head"><h3>Equity curve</h3><span className="muted small">capital plus cumulative net, after charges</span></header>
        <EquityChart points={r.curve} base={r.params.capital} />
      </section>
      <div className="ws-pair">
        <section className="card">
          <header className="card-head"><h3>Earlier vs recent</h3><span className="muted small">first 60% of {noun} against the last 40%</span></header>
          <table className="bt-split">
            <thead><tr><th /><th>Earlier</th><th>Recent</th></tr></thead>
            <tbody>
              <tr><td>Period</td><td>{early.period ? `${day(early.period[0])} – ${day(early.period[1])}` : "–"}</td><td>{late.period ? `${day(late.period[0])} – ${day(late.period[1])}` : "–"}</td></tr>
              <tr><td>Net</td><td className={tone(early.net)}>{rupee(early.net)}</td><td className={tone(late.net)}>{rupee(late.net)}</td></tr>
              <tr><td>Trades</td><td>{early.trades}</td><td>{late.trades}</td></tr>
              <tr><td>Win rate</td><td>{early.win_rate ?? "–"}%</td><td>{late.win_rate ?? "–"}%</td></tr>
              <tr><td>Profit factor</td><td>{early.profit_factor ?? "–"}</td><td>{late.profit_factor ?? "–"}</td></tr>
              <tr><td>Max drawdown</td><td className="down">{rupee(early.max_drawdown)}</td><td className="down">{rupee(late.max_drawdown)}</td></tr>
            </tbody>
          </table>
          <p className="muted small">The rules are fixed by the spec and nothing is fitted to this data, so every {noun.replace(/s$/, "")} is unseen. The split shows whether the result holds over time.</p>
        </section>
        <section className="card">
          <header className="card-head"><h3>How trades ended</h3></header>
          <ul className="bt-bars">
            {Object.entries(r.exits).sort((a, b) => b[1].n - a[1].n).map(([k, x]) => (
              <li key={k}>
                <span>{k}</span>
                <span className="bt-bar"><i className={tone(x.net)} style={{ width: `${(x.n / Math.max(1, s.trades)) * 100}%` }} /></span>
                <b>{x.n}</b>
                <b className={tone(x.net)}>{rupee(x.net)}</b>
              </li>
            ))}
          </ul>
          <header className="card-head pd-sub"><h3>By month</h3></header>
          <ul className="bt-bars">
            {r.months.map((m) => (
              <li key={m.month}>
                <span>{new Date(`${m.month}-01`).toLocaleDateString("en-IN", { month: "short", year: "2-digit" })}</span>
                <span className="bt-bar"><i className={tone(m.net)} style={{ width: `${(Math.abs(m.net) / Math.max(1, ...r.months.map((x) => Math.abs(x.net)))) * 100}%` }} /></span>
                <b>{m.trades}</b>
                <b className={tone(m.net)}>{rupee(m.net)}</b>
              </li>
            ))}
          </ul>
        </section>
      </div>
      <div className="ws-pair">
        {[["Best stocks", r.best_stocks], ["Worst stocks", r.worst_stocks]].map(([t, rows]) => (
          <section key={t} className="card">
            <header className="card-head"><h3>{t}</h3></header>
            <ul className="bt-bars">
              {rows.map((x) => (
                <li key={x.symbol}><span>{x.symbol}</span><span className="muted small">{x.wins}/{x.n} won</span><b /><b className={tone(x.net)}>{rupee(x.net)}</b></li>
              ))}
            </ul>
          </section>
        ))}
      </div>
      <section className="card">
        <header className="card-head"><h3>All trades</h3><span className="muted small">{r.trades.length} trades</span></header>
        {kind === "orb" ? <DataTable rowKey={(t) => t.session + t.symbol + t.entry_time} initialSort={{ key: "session", dir: "desc" }} rows={r.trades}
          columns={[
            { key: "session", label: "Date", render: (t) => day(t.session) },
            { key: "symbol", label: "Stock", render: (t) => <b>{t.symbol}</b> },
            { key: "entry_time", label: "Entry", render: (t) => `${t.entry_time} @ ${num(t.entry_ref)}` },
            { key: "qty", label: "Qty", align: "right" },
            { key: "sl", label: "Stop / target", render: (t) => `${num(t.sl)} / ${t.target ? num(t.target) : "–"}` },
            { key: "exit_time", label: "Exit", render: (t) => `${t.exit_time || "–"} @ ${num(t.exit_ref)}` },
            { key: "exit_reason", label: "Reason", render: (t) => <Badge tone={t.exit_reason === "target" ? "buy" : t.exit_reason?.startsWith("stop") ? "sell" : ""}>{t.exit_reason}</Badge> },
            { key: "net", label: "Net", align: "right", render: (t) => <b className={tone(t.net)}>{rupee(t.net)}</b> },
          ]} />
          : <DataTable rowKey={(t) => t.day + t.symbol + (t.entry_time || t.entry_date)} initialSort={{ key: "entry_date", dir: "desc" }} rows={r.trades}
            columns={[
              { key: "entry_date", label: "Date", render: (t) => day(t.entry_date) },
              { key: "symbol", label: "Stock", render: (t) => <b>{t.symbol}</b> },
              { key: "side", label: "Side", render: (t) => <span className={`dir ${t.side.toLowerCase()}`}>{t.side}</span> },
              { key: "entry", label: "Entry", render: (t) => `${t.entry_time ? `${t.entry_time} @ ` : ""}${num(t.entry)}` },
              { key: "stop", label: "Stop / target", render: (t) => `${num(t.stop)} / ${t.target ? num(t.target) : "–"}` },
              { key: "exit", label: "Exit", render: (t) => `${t.exit_time || (t.exit_date ? day(t.exit_date) : "–")} @ ${num(t.exit)}` },
              { key: "exit_reason", label: "Reason", render: (t) => <Badge tone={t.r > 0 ? "buy" : t.r < 0 ? "sell" : ""}>{t.exit_reason}</Badge> },
              { key: "r", label: "R", align: "right", render: (t) => <b className={tone(t.r)}>{signed(t.r)}R</b> },
              { key: "net", label: "Net", align: "right", render: (t) => <b className={tone(t.net)}>{rupee(t.net)}</b> },
            ]} />}
      </section>
      {kind === "orb" ? (
        <p className="muted small bt-note">
          Today's F&O list is applied to past sessions (survivorship). The momentum check uses each stock's own 20-day trend; the sweep's RS rank is not known for past days and is left out. Exits are checked on 5-minute candles with the stop assumed first when one candle touches both.
        </p>
      ) : <ul className="muted small bt-note">{(r.notes || []).map((n) => <li key={n}>{n}</li>)}</ul>}
    </>
  );
}

// ---------------- Darvas ----------------
const UNIVERSES = [["top500", "Top 500 liquid (monthly)"], ["core", "All five indices"], ["NIFTY 50", "Nifty 50"], ["NIFTY BANK", "Bank Nifty"], ["NIFTY MIDCAP 100", "Midcap 100"], ["NIFTY SMLCAP 100", "Smallcap 100"]];

function DarvasForm({ busy, onRun }) {
  const [f, setF] = useState({ universe: "top500", years: 6, capital: 100000, risk_pct: 1 });
  const set = (k, n = true) => (e) => setF({ ...f, [k]: n ? Number(e.target.value) : e.target.value });
  return (
    <form className="bt-form" onSubmit={(e) => { e.preventDefault(); onRun(f); }}>
      <label>Stocks
        <select value={f.universe} onChange={set("universe", false)}>
          {UNIVERSES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
      </label>
      <label>History
        <select value={f.years} onChange={set("years")}>
          {[2, 3, 4, 5, 6, 8, 10].map((n) => <option key={n} value={n}>{n} years</option>)}
        </select>
      </label>
      <label>Capital (₹)<input type="number" min="10000" step="10000" value={f.capital} onChange={set("capital")} /></label>
      <label>Risk per trade
        <select value={f.risk_pct} onChange={set("risk_pct")}>
          {[0.5, 1, 1.5, 2].map((n) => <option key={n} value={n}>{n}% of equity</option>)}
        </select>
      </label>
      <button type="submit" className="primary" disabled={busy}>{busy ? "Running…" : "Run backtest"}</button>
    </form>
  );
}

const VERDICT_TONE = { "EDGE CONFIRMED": "up", "WEAK EDGE": "amber", "NO EDGE": "down", "INSUFFICIENT DATA": "amber" };

function DarvasResult({ r }) {
  const m = r.oos.metrics, b = r.baseline.metrics;
  const cap = r.params?.capital ?? r.oos.equity[0]?.[1];
  const eq = r.oos.equity.map((x) => [x[0], x[1]]);
  const bench = r.benchmark?.oos;
  const trades = r.oos.trades.map((t) => ({ ...t, symbol: t.key.split(":")[1] }));
  return (
    <>
      <Verdict tone={VERDICT_TONE[r.verdict.status] || ""} title={r.verdict.status.toLowerCase().replace(/^./, (c) => c.toUpperCase())}>
        {r.verdict.headline} {r.verdict.detail}
      </Verdict>
      <Scorecard sc={darvasScore(r)} />
      <section className="paper-stats">
        <Stat label="Backtest return" value={pct(m.total_return)} tone={tone(m.total_return)} sub={`CAGR ${pct(m.cagr)}${bench ? ` · Nifty ${pct(bench.cagr)}` : ""}`} />
        <Stat label="Win rate" value={pct(m.win_rate)} sub={`${m.trades} closed trades · 95% ${pct(m.win_rate_ci95?.[0], 0)}–${pct(m.win_rate_ci95?.[1], 0)}`} />
        <Stat label="Expectancy" value={`${signed(m.expectancy_r)}R`} tone={tone(m.expectancy_r)} sub={`t-stat ${num(m.t_stat)} · avg win ${num(m.avg_win_r)}R`} />
        <Stat label="Profit factor" value={m.profit_factor == null ? "–" : num(m.profit_factor)} sub={`payoff ${m.payoff_ratio == null ? "–" : num(m.payoff_ratio)}`} />
        <Stat label="Max drawdown" value={pct(m.max_drawdown)} tone="down" sub={bench ? `Nifty ${pct(bench.max_drawdown)}` : ""} />
        {m.t1_hit_rate ? <Stat label="T1 hit rate" value={pct(m.t1_hit_rate)} sub={`${num(m.avg_bars_held, 0)} days held on average`} />
          : <Stat label="Average hold" value={`${num(m.avg_bars_held, 0)} days`} sub={`time in the market ${pct(m.exposure, 0)}`} />}
      </section>
      <section className="card">
        <header className="card-head"><h3>Equity curve</h3><span className="muted small">{r.symbols} stocks · {day(r.period[0])} – {day(r.period[1])} · costs {num(r.round_trip_cost_pct)}% a round trip</span></header>
        <EquityChart points={eq} base={cap} />
      </section>
      <div className="ws-pair">
        {r.windows.length > 1 ? <section className="card">
          <header className="card-head"><h3>Unseen vs full history</h3><span className="muted small">is the edge real or fitted?</span></header>
          <table className="bt-split">
            <thead><tr><th /><th>Unseen (walk-forward)</th><th>Default settings, all years</th></tr></thead>
            <tbody>
              <tr><td>Trades</td><td>{m.trades}</td><td>{b.trades}</td></tr>
              <tr><td>Win rate</td><td>{pct(m.win_rate)}</td><td>{pct(b.win_rate)}</td></tr>
              <tr><td>Expectancy</td><td className={tone(m.expectancy_r)}>{signed(m.expectancy_r)}R</td><td className={tone(b.expectancy_r)}>{signed(b.expectancy_r)}R</td></tr>
              <tr><td>Return</td><td className={tone(m.total_return)}>{pct(m.total_return)}</td><td className={tone(b.total_return)}>{pct(b.total_return)}</td></tr>
              <tr><td>CAGR</td><td>{pct(m.cagr)}</td><td>{pct(b.cagr)}</td></tr>
              <tr><td>Max drawdown</td><td className="down">{pct(m.max_drawdown)}</td><td className="down">{pct(b.max_drawdown)}</td></tr>
            </tbody>
          </table>
          {bench && <p className="muted small">Holding {r.benchmark.name} over the same unseen dates: {pct(bench.total_return)} total, CAGR {pct(bench.cagr)}, drawdown {pct(bench.max_drawdown)}.</p>}
        </section> : (
          <section className="card">
            <header className="card-head"><h3>Against the index</h3><span className="muted small">fixed rules, nothing fitted: every year after the warm-up is tested</span></header>
            <table className="bt-split">
              <thead><tr><th /><th>Darvas</th><th>{r.benchmark?.name || "Nifty 50"} held</th></tr></thead>
              <tbody>
                <tr><td>Return</td><td className={tone(m.total_return)}>{pct(m.total_return)}</td><td className={tone(bench?.total_return)}>{pct(bench?.total_return)}</td></tr>
                <tr><td>CAGR</td><td>{pct(m.cagr)}</td><td>{pct(bench?.cagr)}</td></tr>
                <tr><td>Max drawdown</td><td className="down">{pct(m.max_drawdown)}</td><td className="down">{pct(bench?.max_drawdown)}</td></tr>
                <tr><td>Trades</td><td>{m.trades}</td><td>–</td></tr>
                <tr><td>Time in the market</td><td>{pct(m.exposure, 0)}</td><td>100%</td></tr>
              </tbody>
            </table>
            {m.trades < 30 && <p className="muted small">Fewer than 30 trades is too few to judge an edge: pick more stocks (Top 500) or more years.</p>}
          </section>
        )}
        <section className="card">
          <header className="card-head"><h3>By year</h3><span className="muted small">expectancy per trade</span></header>
          <ul className="bt-bars">
            {Object.entries(m.by_year || {}).map(([y, x]) => (
              <li key={y}>
                <span>{y}</span>
                <span className="bt-bar"><i className={tone(x.expectancy_r)} style={{ width: `${Math.min(100, Math.abs(x.expectancy_r) * 100)}%` }} /></span>
                <b>{x.trades}</b>
                <b className={tone(x.expectancy_r)}>{signed(x.expectancy_r)}R</b>
              </li>
            ))}
          </ul>
          <header className="card-head pd-sub"><h3>How trades ended</h3></header>
          <ul className="bt-bars">
            {Object.entries(m.exit_reasons || {}).sort((a, c) => c[1] - a[1]).map(([k, n]) => (
              <li key={k}><span>{k}</span><span className="bt-bar"><i style={{ width: `${(n / Math.max(1, m.trades)) * 100}%` }} /></span><b>{n}</b><b /></li>
            ))}
          </ul>
        </section>
      </div>
      {r.windows.length > 1 && <section className="card">
        <header className="card-head"><h3>Rolling test windows</h3><span className="muted small">each window's trades, after a 3-year warm-up</span></header>
        <DataTable rowKey={(w) => w.test} rows={r.windows}
          columns={[
            { key: "train", label: "Trained on", render: (w) => w.train.replace("..", " → ") },
            { key: "test", label: "Tested on (unseen)", render: (w) => w.test.replace("..", " → ") },
            { key: "params", label: "Settings picked", render: (w) => <span className="small">{Object.entries(w.params).map(([k, v]) => `${k.replace(/_/g, " ")} ${v}`).join(" · ") || "defaults"}</span> },
            { key: "oos_n", label: "Trades", align: "right" },
            { key: "oos_wr", label: "Win rate", align: "right", render: (w) => pct(w.oos_wr) },
            { key: "oos_exp", label: "Expectancy", align: "right", render: (w) => <b className={tone(w.oos_exp)}>{signed(w.oos_exp)}R</b> },
          ]} />
      </section>}
      <section className="card">
        <header className="card-head"><h3>All trades</h3><span className="muted small">{trades.length} trades</span></header>
        <DataTable rowKey={(t) => t.key + t.entry_date} initialSort={{ key: "entry_date", dir: "desc" }} rows={trades}
          columns={[
            { key: "entry_date", label: "Entry", render: (t) => day(t.entry_date) },
            { key: "symbol", label: "Stock", render: (t) => <b>{t.symbol}</b> },
            { key: "segment", label: "Size" },
            { key: "entry", label: "Price", align: "right", render: (t) => num(t.entry) },
            { key: "stop", label: "Stop", align: "right", render: (t) => num(t.stop) },
            { key: "exit_date", label: "Exit", render: (t) => (t.exit_date ? day(t.exit_date) : "open") },
            { key: "exit_reason", label: "Reason", render: (t) => <Badge tone={t.r > 0 ? "buy" : "sell"}>{t.exit_reason}</Badge> },
            { key: "r", label: "R", align: "right", render: (t) => <b className={tone(t.r)}>{signed(t.r)}R</b> },
            { key: "pnl", label: "P&L", align: "right", render: (t) => <b className={tone(t.pnl)}>{rupee(t.pnl)}</b> },
          ]} />
      </section>
      <ul className="muted small bt-note">{r.assumptions.map((a) => <li key={a}>{a}</li>)}</ul>
    </>
  );
}

export default function Backtest() {
  const [strat, setStrat] = useState(() => {
    try { const v = localStorage.getItem("st.bt"); return ["orb", "darvas"].includes(v) ? v : "orb"; } catch { return "orb"; }
  });
  useEffect(() => { try { localStorage.setItem("st.bt", strat); } catch { /* private window */ } }, [strat]);
  const orbQ = useLoad(api.backtestOrb, [], 3000);
  const darQ = useLoad(api.backtestDarvas, [], 3000);
  const Q = { orb: orbQ, darvas: darQ };
  const [err, setErr] = useState("");
  const run = async (fn, body, reload) => {
    setErr("");
    try { await fn(body); reload(true); } catch (e) { setErr(e.message); }
  };
  const op = orbQ.data?.progress;
  const ds = darQ.data?.status;
  const q = Q[strat] || orbQ;
  const res = q.data?.result;
  const meta = STRATS.find((s) => s.id === strat) || STRATS[0];

  return (
    <div className="bt fade-in">
      <div className="bt-picks">
        {STRATS.map((s) => {
          const running = s.id === "darvas" ? ds?.running : Q[s.id].data?.progress?.running;
          const r = Q[s.id].data?.result;
          const head = s.id === "darvas" ? r && r.verdict.status.toLowerCase() : r && rupee(r.stats.net);
          return (
            <button key={s.id} type="button" className={`card bt-pick${strat === s.id ? " on" : ""}`} onClick={() => setStrat(s.id)}>
              <small>{s.kind}</small>
              <b>{s.name}</b>
              <span className="muted small">{running ? "Backtest running…" : r ? `Last run ${new Date(r.built || r.generated_at).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" })}` : "Not run yet"}</span>
              {head && <em className={s.id === "darvas" ? VERDICT_TONE[r.verdict.status] : tone(r.stats.net)}>{head}</em>}
            </button>
          );
        })}
      </div>

      <section className="card">
        <header className="card-head">
          <div>
            <h3>{meta.name} settings</h3>
            <span className="muted small">{meta.desc}</span>
          </div>
        </header>
        {strat === "orb" ? <OrbForm busy={op?.running} onRun={(b) => run(api.backtestOrbRun, b, orbQ.reload)} />
          : <DarvasForm busy={ds?.running} onRun={(b) => run(api.backtestDarvasRun, b, darQ.reload)} />}
        {strat === "orb" && op && <Progress {...op} error={op.error} />}
        {strat === "darvas" && ds && (ds.running || (ds.exit_code != null && ds.exit_code !== 0)) && (
          <Progress running={ds.running} done={ds.progress?.[0] || 0} total={ds.progress?.[1] || 0} stage={ds.stage}
            error={!ds.running && ds.exit_code ? `${ds.stage}: ${ds.log.at(-1) || ""}` : ""} />
        )}
        {strat === "orb" && !res && <p className="muted small">The first run downloads 5-minute history for every F&O stock (a few minutes); later runs reuse it.</p>}
        {strat !== "orb" && !res && <p className="muted small">The first run downloads the history it needs (a few minutes); later runs reuse the cache.</p>}
        <ErrorNote error={err || q.error} />
      </section>

      {q.loading && !q.data ? <div className="chart-empty" style={{ height: 200 }}><Spinner /></div>
        : !res ? <Empty>No result yet. Choose settings above and run the backtest.</Empty>
          : strat === "darvas" ? <DarvasResult r={res} /> : <TradeResult r={res} kind={strat} />}
    </div>
  );
}
