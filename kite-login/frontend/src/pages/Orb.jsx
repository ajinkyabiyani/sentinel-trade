import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api.js";
import { inr, num, signed, tone } from "../lib/format.js";
import { Badge, DataTable, Empty, ErrorNote, Segmented, Spinner, useLoad } from "../components/ui.jsx";

// ORB strategy (spec v1.1): buy-only 09:15-09:30 opening-range breakout on liquid F&O stocks, Nifty-confirmed.
// The backend replays the session from 5-minute candles; everything here is a paper signal. Buying goes through
// the Trade ticket, which you confirm yourself.
const PHASE = {
  "pre-open": "Before the open",
  "opening range": "Building the opening range (09:15–09:30)",
  "entry window": "Entry window: breakouts until the 11:25 candle",
  managing: "Entry window closed: managing open trades to 15:10",
  closed: "Session closed",
};
const STATE_TONE = { ACTIVE: "buy", CLOSED: "", HALTED_PROFIT: "buy", HALTED_LOSS: "sell", HALTED_MANUAL: "sell" };
const STATE_TEXT = { ACTIVE: "Active", CLOSED: "Closed for the day", HALTED_PROFIT: "Halted: ₹7,000 profit stop", HALTED_LOSS: "Halted: 5% loss stop", HALTED_MANUAL: "Stopped by you (emergency stop)" };

const Check = ({ ok, children }) => <span className={`orb-check ${ok ? "ok" : "no"}`}>{ok ? "✓" : "✗"} {children}</span>;

function PnlMeter({ c }) {
  if (!c.loss_amount) return null;
  const lo = -c.loss_amount;
  const hi = c.profit_stop;
  const pos = Math.min(1, Math.max(0, (c.net - lo) / (hi - lo)));
  const zero = -lo / (hi - lo);
  return (
    <div className="orb-meter" title="Daily net ORB P&L between the 5% loss stop and the ₹7,000 profit stop">
      <span className="orb-meter-zero" style={{ left: `${zero * 100}%` }} />
      <span className={`orb-meter-dot ${tone(c.net)}`} style={{ left: `${pos * 100}%` }} />
      <div className="orb-meter-ends">
        <span className="down">−{inr(c.loss_amount, 0)}</span>
        <span className="up">+{inr(hi, 0)}</span>
      </div>
    </div>
  );
}

function Capital({ c, frozen, onSave }) {
  const [edit, setEdit] = useState(false);
  const [v, setV] = useState("");
  if (edit)
    return (
      <form className="orb-cap-edit" onSubmit={(e) => { e.preventDefault(); onSave(Number(v)).then(() => setEdit(false)); }}>
        <input type="number" min="1000" step="1000" value={v} onChange={(e) => setV(e.target.value)} autoFocus aria-label="Starting capital" />
        <button type="submit" className="small">Save</button>
        <button type="button" className="ghost small" onClick={() => setEdit(false)}>Cancel</button>
      </form>
    );
  return (
    <>
      <b>{c.c0 ? inr(c.c0, 0) : "Unknown"}</b>
      <span className="hint">
        {c.c0_source || "Kite equity funds show ₹0: set your paper capital, or no trade can be sized"}
        {!frozen && (
          <button type="button" className="linkish" onClick={() => { setV(c.c0 || ""); setEdit(true); }}>
            change
          </button>
        )}
      </span>
    </>
  );
}

// 5-minute candles with the opening range, VWAP, EMA20/50 and the trade's entry, stop and target.
function OrbChart({ symbol }) {
  const ch = useLoad(() => api.orbChart(symbol), [symbol], 30_000);
  const d = ch.data;
  if (!d) return <div className="chart-empty">{ch.error ? <ErrorNote error={ch.error} /> : <Spinner />}</div>;
  if (!d.bars.length) return <Empty>No candles yet for {symbol} this session.</Empty>;
  const W = 760;
  const H = 300;
  const pad = { l: 8, r: 64, t: 12, b: 22 };
  const tr = d.trades.at(-1);
  const levels = [d.or_high, d.or_low, tr?.sl, tr?.target, tr?.entry_ref].filter(Boolean);
  const vals = d.bars.flatMap((b) => [b.h, b.l, b.vwap, b.ema20, b.ema50].filter((v) => v != null)).concat(levels);
  const lo = Math.min(...vals);
  const hi = Math.max(...vals);
  const y = (v) => pad.t + (1 - (v - lo) / (hi - lo || 1)) * (H - pad.t - pad.b);
  const n = 72;
  const step = (W - pad.l - pad.r) / n;
  const idx = (t) => {
    const [h, m] = t.split(":").map(Number);
    return (h * 60 + m - 555) / 5;
  };
  const x = (t) => pad.l + idx(t) * step + step / 2;
  const line = (k) =>
    d.bars.filter((b) => b[k] != null).map((b, i) => `${i ? "L" : "M"}${x(b.t).toFixed(1)},${y(b[k]).toFixed(1)}`).join("");
  const hline = (v, cls, label) =>
    v ? (
      <g key={label}>
        <line x1={pad.l} x2={W - pad.r} y1={y(v)} y2={y(v)} className={cls} />
        <text x={W - pad.r + 4} y={y(v) + 4} className={`orb-axis ${cls}`}>{label} {num(v)}</text>
      </g>
    ) : null;
  return (
    <div className="orb-chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${symbol} 5-minute chart with opening range`}>
        <rect x={x("09:15") - step / 2} width={step * 3} y={pad.t} height={H - pad.t - pad.b} className="orb-or-zone" />
        {hline(d.or_high, "orb-l-orh", "OR H")}
        {hline(d.or_low, "orb-l-orl", "OR L")}
        {tr && hline(tr.sl, "orb-l-sl", "SL")}
        {tr?.target && hline(tr.target, "orb-l-tg", "3R")}
        {d.bars.map((b) => (
          <g key={b.t} className={b.c >= b.o ? "orb-c-up" : "orb-c-dn"}>
            <line x1={x(b.t)} x2={x(b.t)} y1={y(b.h)} y2={y(b.l)} />
            <rect x={x(b.t) - step * 0.32} width={step * 0.64} y={y(Math.max(b.o, b.c))} height={Math.max(1, Math.abs(y(b.o) - y(b.c)))} />
          </g>
        ))}
        <path d={line("vwap")} className="orb-p-vwap" />
        <path d={line("ema20")} className="orb-p-e20" />
        <path d={line("ema50")} className="orb-p-e50" />
        {d.trades.map((t) => (
          <g key={t.entry_time}>
            <path d={`M${x(t.entry_time)},${y(t.entry_ref) + 14}l-6,9h12z`} className="orb-buy-mark" />
            {t.exit_time && <circle cx={x(t.exit_time.slice(0, 5))} cy={y(t.exit_ref)} r="4.5" className="orb-exit-mark" />}
          </g>
        ))}
        {["09:30", "11:30", "15:10"].map((t) => (
          <g key={t}>
            <line x1={x(t) - step / 2} x2={x(t) - step / 2} y1={pad.t} y2={H - pad.b} className="orb-vline" />
            <text x={x(t) - step / 2 + 3} y={H - 6} className="orb-axis">{t}</text>
          </g>
        ))}
      </svg>
      <div className="orb-legend">
        <span className="vwap">VWAP</span>
        <span className="e20">EMA 20</span>
        <span className="e50">EMA 50</span>
        <span className="orh">Opening range</span>
        {tr && <span className="sl">Stop</span>}
        {tr?.target && <span className="tg">3R target</span>}
      </div>
    </div>
  );
}

export default function Orb({ openTicket }) {
  const st = useLoad(api.orbState, [], 10_000);
  const sw = useLoad(api.momentum, [], 60_000);
  const [sel, setSel] = useState(null);
  const [showEx, setShowEx] = useState(false);
  const [err, setErr] = useState("");
  const s = st.data?.snapshot;
  const prog = st.data?.progress;

  useEffect(() => {
    if (!sel && s) setSel(s.trades.at(-1)?.symbol || s.shortlist.find((r) => r.checks)?.symbol || null);
  }, [s, sel]);

  const settings = async (body) => {
    setErr("");
    try {
      await api.orbSettings(body);
      await st.reload(true);
    } catch (e) {
      setErr(e.message);
      throw e;
    }
  };
  const buy = (t) =>
    openTicket({ exchange: "NSE", tradingsymbol: t.symbol, transaction_type: "BUY", product: "MIS", order_type: "LIMIT",
      price: t.ltp || t.entry_ref || t.close, quantity: t.qty || undefined });

  const watched = useMemo(() => (s ? s.shortlist.filter((r) => r.checks) : []), [s]);
  const others = (s?.signals || []).filter((x) => x.status !== "entered" && x.status !== "pending");
  const pend = (s?.signals || []).filter((x) => x.status === "pending");

  if (!s)
    return (
      <section className="card orb-loading">
        <Spinner />
        <div>
          <h3>{prog?.running ? prog.stage : "Starting the ORB scanner"}</h3>
          <p className="muted">
            {prog?.total ? `${prog.done} of ${prog.total} symbols · ` : ""}About 40 sessions of 5-minute candles for every F&O stock and Nifty
            are loaded once a day (Kite allows about 3 requests a second, so this takes a minute or two).
          </p>
          {prog?.total > 0 && <div className="orb-prog"><i style={{ width: `${(prog.done / prog.total) * 100}%` }} /></div>}
          <ErrorNote error={st.error || st.data?.error} />
        </div>
      </section>
    );

  const c = s.controller;
  return (
    <div className="stack orb">
      <section className="orb-status">
        <div className="card orb-tile">
          <span className="eyebrow">Session {s.session}{s.live ? "" : " (last trading day)"}</span>
          <b>{PHASE[s.phase]}</b>
          <span className="hint">
            {s.last_bar ? `Last closed candle ${s.last_bar}` : "No candle closed yet"}
            {prog?.running ? ` · refreshing ${prog.done}/${prog.total}` : ""}
          </span>
          {s.live && s.phase !== "closed" && (
            <span className="live-chip" title="The backend rescans every 15 s and fetches candles after each 5-minute close; this page refreshes every 10 s">
              <span className="live-dot" /> Auto-scanning · {new Date(s.asof).toLocaleTimeString("en-IN", { timeStyle: "medium" })}
            </span>
          )}
        </div>
        <div className="card orb-tile">
          <span className="eyebrow">Nifty confirmation</span>
          {s.nifty ? (
            <>
              <b className={s.nifty.ok ? "up" : "down"}>{s.nifty.ok ? "Confirms buys" : "Not confirming"}</b>
              <span className="orb-checks">
                <Check ok={s.nifty.close > s.nifty.avg}>close {num(s.nifty.close)} vs avg {num(s.nifty.avg)}</Check>
                <Check ok={s.nifty.ema20 > s.nifty.ema50}>EMA20 &gt; EMA50</Check>
              </span>
            </>
          ) : (
            <b className="muted">Waiting for candles</b>
          )}
        </div>
        <div className="card orb-tile">
          <span className="eyebrow">Starting capital (C0)</span>
          <Capital c={c} frozen={s.trades.length > 0} onSave={(v) => settings({ capital: v })} />
        </div>
        <div className="card orb-tile">
          <span className="eyebrow">Daily ORB net P&L</span>
          <b className={tone(c.net)}>{signed(c.net)}</b>
          <span className="hint">
            realised {signed(c.realized)} · open {signed(c.unrealized)}
          </span>
          <PnlMeter c={c} />
        </div>
        <div className="card orb-tile">
          <span className="eyebrow">Daily controller</span>
          <Badge tone={STATE_TONE[c.state]}>{STATE_TEXT[c.state] || c.state}</Badge>
          <span className="hint">
            {c.halt ? `at ${c.halt.time} on ${c.halt.source}, P&L ${signed(c.halt.pnl)}` : `stops at +₹7,000 or −${c.loss_amount ? inr(c.loss_amount, 0) : "5% of C0"}`}
          </span>
        </div>
      </section>

      <section className="card orb-controls">
        <div className="field">
          Exit mode (applies to new entries)
          <Segmented size="small" label="Exit mode" value={s.settings.mode} onChange={(m) => settings({ mode: m }).catch(() => {})}
            options={[{ id: "3R", label: "Stop + 3R target" }, { id: "none", label: "Stop only (spec default)" }]} />
        </div>
        <div className="field">
          Per trade
          <Segmented size="small" label="Per-trade notional" value={String(s.settings.notional)}
            onChange={(v) => settings({ notional: Number(v) }).catch(() => {})}
            options={[50000, 100000, 200000, 500000].map((v) => ({ id: String(v), label: inr(v, 0).replace(".00", "") }))} />
        </div>
        <div className="field">
          Universe (from the next session)
          <Segmented size="small" label="Universe" value={s.settings.wide_next ? "wide" : "fno"} onChange={(v) => settings({ wide: v === "wide" }).catch(() => {})}
            options={[{ id: "wide", label: "F&O + momentum stocks" }, { id: "fno", label: "F&O only (spec)" }]} />
        </div>
        <div className="field">
          Momentum check on signals
          <Segmented size="small" label="Momentum check" value={s.settings.momentum ? "on" : "off"} onChange={(v) => settings({ momentum: v === "on" }).catch(() => {})}
            options={[{ id: "on", label: "On" }, { id: "off", label: "Off" }]} />
        </div>
        <p className="hint orb-sweep">
          {sw.data?.progress?.running
            ? `Momentum sweep running: ${sw.data.progress.done} / ${sw.data.progress.total} NSE stocks…`
            : sw.data?.day
              ? `Momentum sweep ${sw.data.day}: ${sw.data.scanned} NSE stocks, ${sw.data.liquid} liquid (≥ ₹10 cr/day), ${sw.data.momentum} in momentum (uptrend, RS ≥ 80); the top 150 join the next session.`
              : "Momentum sweep has not run yet: it reads every NSE stock once a day after the close (about 15 minutes)."}
          {" "}Today: {s.counts.fno} F&O{s.counts.momentum ? ` + ${s.counts.momentum} momentum` : ""} stocks.
        </p>
        <p className="hint orb-rule">
          Buy when a 5-minute candle closes above the opening-range high (previous close at or below it), above VWAP, EMA20 &gt; EMA50 and
          Nifty confirming{s.settings.momentum ? ", and the stock is in momentum (daily close above its 20-day average, 20-day return positive, RS ≥ 50 when known)" : ""}; opening RVOL ≥ 2 and range / daily ATR 0.10–0.80. Entry at the next candle's open, stop = entry − 1.5 × ATR14,
          target = entry + 3R, exit by 15:10.
        </p>
        <ErrorNote error={err} />
      </section>

      <section className="card">
        <header className="card-head">
          <h3>Signals and trades</h3>
          <span className="muted">{s.trades.length} entered · {pend.length} pending · paper P&L incl. charges and 1bp slippage</span>
        </header>
        {pend.map((p) => (
          <div key={p.symbol + p.time} className="orb-pending fade-in">
            <Badge tone="buy">BUY</Badge>
            <b>{p.symbol}</b>
            <span>
              broke {num(p.or_high)} on the {p.time} candle (close {num(p.close)}) · {p.note || "buys at the next candle's open"} · stop ≈ {num(p.sl_est)}, target ≈{" "}
              {num(p.target_est)}
            </span>
            <button type="button" className="buy small" onClick={() => buy(p)}>Buy…</button>
          </div>
        ))}
        <DataTable
          rowKey={(t) => t.symbol + t.entry_time}
          rowClass={(t) => (t.symbol === sel ? "sel" : "")}
          empty={s.phase === "entry window" || s.phase === "opening range" || s.phase === "pre-open" ? "No breakout yet today." : "No ORB trade this session."}
          initialSort={{ key: "entry_time", dir: "desc" }}
          rows={s.trades}
          columns={[
            { key: "symbol", label: "Stock", render: (t) => <button type="button" className="linkish strong" onClick={() => setSel(t.symbol)}>{t.symbol}</button> },
            { key: "entry_time", label: "Entry", render: (t) => <span>{t.entry_time} <span className="hint">signal {t.signal_time}</span></span> },
            { key: "entry_ref", label: "Buy at", align: "num", render: (t) => num(t.entry_ref) },
            { key: "qty", label: "Qty", align: "num" },
            { key: "sl", label: "Stop loss", align: "num", render: (t) => <span className="down">{num(t.sl)}</span> },
            { key: "target", label: "Target", align: "num", render: (t) => (t.target ? <span className="up">{num(t.target)}</span> : <span className="hint">none · 1R {num(t.one_r)}</span>) },
            { key: "ltp", label: "LTP / exit", align: "num", render: (t) => (t.status === "open" ? <span>{num(t.ltp)} <span className={`hint ${tone(t.r_now)}`}>{signed(t.r_now)}R</span></span> : <span>{num(t.exit_ref)} <span className="hint">{t.exit_time}</span></span>) },
            { key: "net", label: "Net P&L", align: "num", render: (t) => <b className={tone(t.net)}>{signed(t.net)}</b> },
            { key: "status", label: "Status", render: (t) => (t.status === "open"
              ? <Badge tone={t.stop_touched ? "sell" : "accent"}>{t.stop_touched ? "at stop" : "open"}</Badge>
              : <Badge tone={t.net >= 0 ? "buy" : "sell"}>{t.exit_reason}</Badge>) },
            { key: "act", label: "", nosort: true, render: (t) => t.status === "open" && s.live && <button type="button" className="ghost small" onClick={() => buy(t)}>Buy…</button> },
          ]}
        />
        {others.length > 0 && (
          <details className="orb-other">
            <summary>{others.length} breakout{others.length > 1 ? "s" : ""} not taken</summary>
            <ul>
              {others.map((x) => (
                <li key={x.symbol + x.time}>
                  <b>{x.symbol}</b> {x.time} close {num(x.close)} &gt; {num(x.or_high)}: <span className="muted">{x.note}</span>
                </li>
              ))}
            </ul>
          </details>
        )}
      </section>

      {sel && (
        <section className="card">
          <header className="card-head">
            <h3>{sel} · 5-minute</h3>
            <span className="muted">opening range, VWAP, EMA20/50, entry ▲, stop and target</span>
          </header>
          <OrbChart symbol={sel} />
        </section>
      )}

      <section className="card">
        <header className="card-head">
          <h3>Shortlist</h3>
          <span className="muted">
            {s.counts.eligible} of {s.counts.universe} stocks ({s.counts.fno} F&O{s.counts.momentum ? ` + ${s.counts.momentum} momentum` : ""}) pass liquidity, gap{watched.length ? " and the opening filters" : ""}
          </span>
        </header>
        <DataTable
          rowKey={(r) => r.symbol}
          rowClass={(r) => (r.symbol === sel ? "sel" : "")}
          limit={50}
          rows={s.shortlist}
          empty="No stock passed the opening filters today."
          columns={[
            { key: "symbol", label: "Stock", render: (r) => <button type="button" className="linkish strong" onClick={() => setSel(r.symbol)} disabled={!r.or_high}>{r.symbol}</button> },
            { key: "source", label: "Source", render: (r) => <Badge tone={r.source === "Momentum" ? "amber" : ""}>{r.source}</Badge> },
            { key: "mom", label: "Momentum", sort: (r) => (r.mom_ok ? 1 : 0) * 100 + (r.rs ?? 0), render: (r) => (
              <span className="orb-checks" title={`Daily close ${r.above20 ? "above" : "below"} its 20-day average · 20-day return ${signed(r.ret20, 1)}%${r.rs != null ? ` · RS ${r.rs}` : ""}`}>
                <Check ok={r.mom_ok}>{r.rs != null ? `RS ${r.rs}` : "trend"}</Check>
                <small className={tone(r.ret20)}>{r.ret20 == null ? "" : `${signed(r.ret20, 1)}% 20D`}</small>
              </span>
            ) },
            { key: "ltp", label: "LTP", align: "num", render: (r) => num(r.ltp) },
            { key: "or_high", label: "OR high / low", align: "num", render: (r) => (r.or_high ? `${num(r.or_high)} / ${num(r.or_low)}` : "–") },
            { key: "dist_pct", label: "vs OR high", align: "num", render: (r) => (r.dist_pct == null ? "–" : <span className={tone(r.dist_pct)}>{signed(r.dist_pct)}%</span>) },
            { key: "rvol", label: "Open RVOL", align: "num", render: (r) => (r.rvol == null ? "–" : `${num(r.rvol)}×`) },
            { key: "ratio", label: "Range/ATR", align: "num", render: (r) => (r.ratio == null ? "–" : num(r.ratio)) },
            { key: "turnover_cr", label: "Turnover", align: "num", render: (r) => `₹${num(r.turnover_cr, 0)} cr` },
            { key: "checks", label: "Filters now", nosort: true, render: (r) => r.checks && (
              <span className="orb-checks">
                <Check ok={r.checks.vwap}>VWAP</Check>
                <Check ok={r.checks.trend}>EMA</Check>
              </span>
            ) },
            { key: "status", label: "Status", render: (r) => <span className={r.status === "in trade" || r.status === "entry pending" ? "up strong" : "muted"}>{r.status}</span> },
          ]}
        />
        <div className="orb-ex">
          <span className="muted">Excluded:</span>
          {Object.entries(s.excluded_reasons).map(([k, n]) => (
            <span key={k} className="chip">{k} · {n}</span>
          ))}
          {s.excluded.length > 0 && (
            <button type="button" className="linkish" onClick={() => setShowEx((v) => !v)}>{showEx ? "hide" : "show"} list</button>
          )}
        </div>
        {showEx && (
          <p className="orb-ex-list">
            {s.excluded.map((e) => (
              <span key={e.symbol} title={e.reason}>{e.symbol}<i>{e.reason}</i></span>
            ))}
          </p>
        )}
      </section>

      <p className="footnote">
        Paper signals only: nothing is ordered for you, the Buy button opens the Trade ticket. Exits are checked on 5-minute candles
        (the spec asks for 1-minute data; when one candle touches both stop and target the stop is assumed) and the daily stop on each close
        and on live prices. The spec's own note: this version (re-entries, ₹7,000 / 5% daily stops) has not been backtested, and the earlier
        one-entry sample had a profit factor of about 1.01, negative at 3bp slippage.
      </p>
    </div>
  );
}
