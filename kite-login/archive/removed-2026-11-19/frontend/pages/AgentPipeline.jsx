import { api } from "../lib/api.js";
import { inr, num, rupee, signed, tone } from "../lib/format.js";
import { useTicks } from "../lib/ticks.js";
import { Badge, DataTable, ErrorNote, Spinner, useLoad } from "../components/ui.jsx";
import { WithLogo } from "../components/Logo.jsx";
import AgentTerminal from "../components/AgentTerminal.jsx";

// Agent Pipeline: a team of its own with its own paper book. Intraday and long only: it buys shares and it buys options (a call on a
// break up, a put on a break down), plus a swing option book. The live terminal at the top shows the team at work; the books below
// hold every trade. Paper only: no order is sent to Kite.

function mark(t, tick) {
  if (t.status !== "open") return { px: t.exit_ref ?? t.ltp, net: t.net };
  const px = tick?.last_price ?? t.ltp;
  return { px, net: t.net + (px - t.ltp) * t.qty };
}

function Book({ rows, ticks, live, title, sub, option }) {
  return (
    <section className="card tt-card">
      <header className="card-head">
        <div className="row"><h3>{title}</h3>{live && rows.some((t) => t.status === "open") && <span className="live-chip"><span className="live-dot" /> Live</span>}</div>
        <span className="muted small">{sub}</span>
      </header>
      <DataTable rowKey={(t) => t.key} rows={rows} empty="No trade this session."
        columns={[
          { key: "symbol", label: option ? "Contract" : "Stock", render: (t) => <WithLogo symbol={t.symbol}><b>{t.symbol}</b></WithLogo> },
          { key: "entry_time", label: "Entry", render: (t) => `${t.entry_time} @ ${num(t.entry_ref)}` },
          { key: "qty", label: option ? "Lots / qty" : "Qty", align: "num", render: (t) => (option ? `${t.lots ?? "–"} / ${t.qty}` : t.qty) },
          { key: "sl", label: "Stop / target", align: "num", render: (t) => `${num(t.sl)} / ${num(t.target)}` },
          { key: "ltp", label: "LTP / exit", align: "num", render: (t) => { const m = mark(t, ticks[t.key]); return <b key={m.px}>{num(m.px)}</b>; } },
          { key: "net", label: "Net P&L", align: "num", render: (t) => { const v = mark(t, ticks[t.key]).net; return <b className={tone(v)}>{rupee(v)}</b>; } },
          { key: "r", label: "R", align: "num", render: (t) => <span className={tone(t.r)}>{signed(t.r)}R</span> },
          { key: "status", label: "Status", render: (t) => (t.status === "open" ? <Badge tone="accent">open</Badge> : <Badge tone={t.net >= 0 ? "buy" : "sell"}>{t.exit_reason}</Badge>) },
        ]} />
    </section>
  );
}

export default function AgentPipeline() {
  const q = useLoad(api.agentPipeline, [], 15_000);
  const snap = q.data?.snapshot;
  const cap = q.data?.capital || {};
  const sw = q.data?.swing;
  // a swing position in the shape the trade tables read
  const swingTrades = (sw?.positions || []).map((p) => ({ ...p, symbol: p.instrument, entry_time: p.day === sw.today ? p.entry_at : p.day.slice(5), entry_ref: p.entry, sl: p.stop, exit_ref: p.exit, net: p.net ?? (p.ltp - p.entry) * p.qty, r: p.r ?? 0 }));          // an open one is marked from its entry
  const optionTrades = snap?.option_trades || [];
  const cashTrades = snap?.trades || [];
  const all = [...cashTrades, ...optionTrades, ...swingTrades];
  const { ticks, live } = useTicks(all.filter((t) => t.status === "open").map((t) => t.key));

  if (q.loading && !q.data) return <section className="card orb-loading"><Spinner /></section>;
  return (
    <div className="tt-page">
      <AgentTerminal id="pipeline" />
      <ErrorNote error={q.error} />
      {snap?.scan_note && <ErrorNote error={`Scanner: ${snap.scan_note}`} />}
      {q.data?.installed === false && <ErrorNote error="The trader skills aren't installed in .claude/skills." />}
      <Book rows={cashTrades} ticks={ticks} live={live} title={`Share book · ${inr(cap.cash || 0, 0)}`} sub="shares bought intraday · up to ₹1 lakh a trade · out by 15:15 · after charges and slippage" />
      <Book rows={optionTrades} ticks={ticks} live={live} option title={`Option book · ${inr(cap.options || 0, 0)}`} sub="a call on a break up, a put on a break down · intraday · after charges and slippage" />
      {sw && (
        <Book rows={swingTrades} ticks={ticks} live={live} option title={`Swing option book · ${inr(cap.swing || 0, 0)}`}
          sub={`${sw.status?.scanning ? "scanning now" : sw.day?.at ? `scanned ${sw.day.scanned} charts at ${sw.day.at}, ${sw.day.setups} setup${sw.day.setups === 1 ? "" : "s"}` : `scans by itself at ${sw.limits.scan_at}`} · held up to ${sw.limits.hold_sessions} sessions · expiry ${sw.limits.min_dte}+ days out · delta ${sw.limits.delta[0]} to ${sw.limits.delta[1]} · realised ${rupee(sw.book.realized)}`} />
      )}
      <p className="muted small tt-note">This team stands alone: its own paper book, its own records, and no shared limits with the multi-agent teams. Time Machine, Budget and Risk are gates: a candidate that fails one stops there and never reaches the Broker. A share trade uses up to ₹1 lakh, and an option buy is sized so its stop loses about ₹5,000 at most with charges. Long only: shares are bought and options are bought; nothing is sold short or written. Intraday trades are out by 15:15. A swing option is bought once a day at most twice, on a stock in a 30-week and daily trend that confirms today with room to run, with at least 3 weeks to expiry and a delta of 0.50 to 0.70; it leaves at its stop, its target, 15 sessions, or a week before expiry. Every new trade goes to Telegram.</p>
    </div>
  );
}
