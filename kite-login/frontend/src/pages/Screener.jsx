import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api.js";
import { PriceChart } from "../components/charts.jsx";
import { num, roundTick, shortSector, signed, tone } from "../lib/format.js";
import { useTicks } from "../lib/ticks.js";
import { Badge, ErrorNote, Segmented, Spinner, useLoad } from "../components/ui.jsx";
import { Logo } from "./Insight.jsx";

// Strategy Screener: each strategy's candidates as setups with entry, stop, target and R:R; the detail panel shows
// the chart, the checks behind the setup, a simulated position size and paper / Kite buttons.
const STRATS = [
  { id: "orb", name: "ORB breakout", kind: "Intraday", product: "MIS" },
  { id: "ready", name: "Darvas buy-ready (quant)", short: "Buy-ready", kind: "Swing · trailing box stop", product: "CNC" },
  { id: "buys", name: "Darvas box breakouts (quant)", short: "Breakouts", kind: "Swing · trailing box stop", product: "CNC" },
  { id: "sells", name: "Darvas box breakdowns", short: "Breakdowns (exit)", kind: "Exit / avoid", product: "CNC" },
];
const STATUS_TONE = { Triggered: "buy", Active: "buy", Forming: "", Watching: "", Closed: "", Avoid: "sell" };
const RISK_PER_TRADE = 0.01;   // simulated size: 1% of paper capital at risk, capped by the per-trade amount

const dd = (iso) => new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short" });

function setups(sel, darvas, orb) {
  if (sel === "orb") {
    const trades = Object.fromEntries((orb?.trades || []).map((t) => [t.symbol, t]));
    const sigs = Object.fromEntries((orb?.signals || []).map((s) => [s.symbol, s]));
    return (orb?.shortlist || []).filter((r) => r.or_high).map((r) => {
      const t = trades[r.symbol];
      const s = sigs[r.symbol];
      const entry = t?.entry_ref ?? s?.close ?? r.or_high;
      const sl = t?.sl ?? s?.sl_est ?? null;
      const target = t?.target ?? s?.target_est ?? null;
      const status = t ? (t.status === "open" ? "Triggered" : "Closed") : s?.status === "pending" ? "Triggered" : (r.dist_pct ?? -9) > -1 && orb.phase === "entry window" ? "Forming" : "Watching";
      return {
        symbol: r.symbol, dir: "LONG", entry, sl, target, status, ltp: r.ltp, note: r.status,
        checks: [
          [r.checks?.above_or, `Close above the opening-range high ${num(r.or_high)}`],
          [r.rvol >= 2, `Opening volume ${num(r.rvol, 1)}× normal (≥ 2×)`],
          [r.checks?.vwap, "Above VWAP"],
          [r.checks?.trend, "EMA 20 above EMA 50 (5-min)"],
          [orb.nifty?.ok, "Nifty confirms (above its average, EMA 20 > 50)"],
          [r.mom_ok, `Momentum: ${r.source}${r.rs != null ? ` · RS ${r.rs}` : ""}${r.ret20 != null ? ` · 20D ${signed(r.ret20, 1)}%` : ""}${orb.settings?.momentum ? "" : " (check off)"}`],
        ],
        trade: t, orbRow: r,
      };
    });
  }
  const passFirst = (a, b) => (a.status === "Watching") - (b.status === "Watching");
  if (sel === "ready") return (darvas?.ready || []).map((r) => ({
    symbol: r.symbol, dir: "LONG", entry: r.trigger, sl: r.stop, target: r.t1, target2: r.t2, ltp: r.last_close,
    status: r.filters === "PASS" ? "Forming" : "Watching",
    note: `${num(r.distance_pct)}% below the trigger · ${shortSector(r.sector)}`,
    checks: [[true, `Box ${num(r.box_bottom ?? r.stop)} – ${num(r.trigger)} formed`], [r.distance_pct <= 3, `Within 3% of the trigger (${num(r.distance_pct)}%)`],
      [r.filters === "PASS", r.filters === "PASS" ? "Every filter passes" : r.filters], [r.bullishness >= 60, `Bullishness score ${num(r.bullishness, 0)}`]],
  })).sort(passFirst);
  if (sel === "buys") return (darvas?.breakouts || []).filter((b) => b.signal === "BUY").map((r) => ({
    symbol: r.symbol, dir: "LONG", entry: r.entry || r.last_close, sl: r.stop, target: r.t1, ltp: r.last_close,
    status: r.filters !== "PASS" || r.bars_ago > 3 ? "Watching" : "Triggered",
    note: `broke out ${r.breakout_date} · ${r.status}`,
    checks: [[true, `Closed above the box top ${num(r.box_top)}`], [r.vol_ratio >= 1.5, `Volume ${num(r.vol_ratio, 1)}× average`],
      [r.filters === "PASS", r.filters === "PASS" ? "Every filter passes" : r.filters?.split(";")[0]], [r.bars_ago <= 3, `${r.bars_ago ?? 0} days since the breakout`]],
  })).sort(passFirst);
  if (sel === "sells") return (darvas?.breakouts || []).filter((b) => b.signal === "SELL").map((r) => ({
    symbol: r.symbol, dir: "EXIT", entry: r.last_close, sl: null, target: null, ltp: r.last_close, status: "Avoid",
    note: `closed below the box ${r.breakout_date}`,
    checks: [[false, `Closed ${num(r.beyond_level_pct)}% below the box low ${num(r.box_bottom)}`], [r.vol_ratio < 1.5, `Volume ${num(r.vol_ratio, 1)}× average`]],
  }));
  return [];
}

const rr = (s) => (s.sl && s.target && s.entry && s.entry !== s.sl && (s.target - s.entry) / (s.entry - s.sl) > 0 ? (s.target - s.entry) / (s.entry - s.sl) : null);

function Detail({ s, strat, orb, openStock, openTicket, capital, notional }) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const intraday = strat.id === "orb" || !!s.bars;
  const ch = useLoad(() => (s.bars ? Promise.resolve(null) : strat.id === "orb" ? api.orbChart(s.symbol) : api.stock(s.symbol, "NSE", "6M")), [s.symbol, strat.id]);
  const tk = `${s.ticket?.exchange || "NSE"}:${s.symbol}`;
  const { ticks } = useTicks([tk]);
  const t = ticks[tk];
  const last = t?.last_price ?? s.ltp;
  const risk1 = s.sl && s.entry && s.entry !== s.sl && (s.dir === "SHORT" ? s.sl > s.entry : s.entry > s.sl) ? Math.abs(s.entry - s.sl) : null;
  const qty = s.ticket?.quantity ?? s.trade?.qty ?? (risk1 ? Math.max(1, Math.min(Math.floor((capital * RISK_PER_TRADE) / risk1), Math.floor(notional / s.entry))) : Math.max(1, Math.floor(notional / (s.entry || 1))));
  const candles = s.bars ? s.bars : strat.id === "orb" ? ch.data?.bars?.map((b) => [`${ch.data.session}T${b.t}:00+05:30`, b.o, b.h, b.l, b.c, b.v]) : ch.data?.candles;
  const lv = (v) => (v ? candles?.map(() => v) : null);
  const overlays = [s.entry && { label: "Trigger", cls: "lv-entry", values: lv(s.entry) }, s.sl && { label: "SL", cls: "lv-sl", values: lv(s.sl) },
    s.target && { label: "Target", cls: "lv-tg", values: lv(s.target) }].filter((o) => o && o.values);
  useEffect(() => { setMsg(""); setErr(""); }, [s.symbol]);

  const paper = async () => {
    setBusy(true);
    setErr("");
    setMsg("");
    try {
      const p = await api.paperEquity({ symbol: s.symbol, side: s.dir === "EXIT" || s.dir === "SHORT" ? "SELL" : "BUY", qty, product: strat.product,
        sl: s.sl || undefined, target: s.target || undefined, strategy: strat.name });
      setMsg(`Paper ${p.legs[0].side === "BUY" ? "bought" : "sold"} ${qty} ${s.symbol} at ${num(p.legs[0].entry)} (${strat.product}). Stop and target are watched every 10 s; see Portfolio & Risk.`);
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <aside className="card scr-detail">
      <header>
        <button type="button" className="linkish" onClick={() => openStock(s.symbol)}><b>{s.symbol}</b></button>
        <span className="muted"> · {strat.name}</span>
        <em className={tone(t?.change_pct)}>{t?.change_pct != null ? `${signed(t.change_pct)}%` : ""}</em>
      </header>
      <div className="scr-px"><b>₹{num(last)}</b>{t && <span className={tone(t.change)}>{signed(t.change)} ({signed(t.change_pct)}%)</span>}</div>
      {candles?.length > 1 ? <PriceChart candles={candles} intraday={intraday} height={200} overlays={overlays} kind="candle" />
        : <div className="chart-empty" style={{ height: 200 }}>{ch.loading ? <Spinner /> : "No chart yet"}</div>}
      {s.history?.length > 0 && (
        <div className="scr-hist">
          <small className="muted">{strat.name} calls on {s.symbol}, last year</small>
          {s.history.slice().reverse().map((h) => (
            <span key={h.entry_date}>
              <em>{dd(h.entry_date)}</em><span>{h.exit_reason}</span><b className={tone(h.r)}>{signed(h.r, 1)}R</b>
            </span>
          ))}
        </div>
      )}
      <ul className="checks">
        {s.checks.map(([ok, text], i) => <li key={i} className={ok ? "ok" : "no"}><i>{ok ? "✓" : "✗"}</i>{text}</li>)}
      </ul>
      <div className="scr-levels">
        <span><small>Entry</small><b>{s.entry ? `₹${num(s.entry)}` : "–"}</b></span>
        <span><small>Stop loss</small><b className="down">{s.sl ? `₹${num(s.sl)}` : "–"}</b></span>
        <span><small>Target</small><b className="up">{s.target ? `₹${num(s.target)}` : "–"}</b></span>
      </div>
      <div className="scr-levels two">
        <span><small>Position qty (simulated)</small><b>{qty}</b></span>
        <span><small>Risk (₹)</small><b>{risk1 ? num(risk1 * qty, 0) : "–"}</b></span>
      </div>
      <details className="scr-more">
        <summary>More targets & rules</summary>
        <p className="muted small">
          {strat.id === "ready" || strat.id === "buys"
            ? `Quant Darvas: a box of at least 10 days, no taller than 12% (large caps) / 15%, its top within 5% of the 52-week high. Signal = daily close above the top on ≥ 1.5× the 20-day volume; buy with a stop-limit 0.05% above the breakout day's high (not chased past +0.5%). Stop at the box bottom, never more than 7% away; no fixed target — the stop moves up to the bottom of each new higher box. ${s.note || ""}`
            : intraday
            ? `ORB spec v1.1: entry at the next 5-minute open after a close above the range high; stop = entry − 1.5 × ATR(5-min); target 3R; exit by 15:10. The ORB bot already paper-trades these automatically.`
            : `Size: ${RISK_PER_TRADE * 100}% of paper capital (₹${num(capital, 0)}) at risk, capped at ₹${num(notional, 0)} per trade.${s.target2 ? ` Second target ₹${num(s.target2)}.` : ""} ${s.note || ""}`}
        </p>
      </details>
      <div className="scr-act">
        <button type="button" className="ghost" onClick={() => openStock(s.symbol)}>View chart</button>
        {s.dir !== "EXIT" && !s.noPaper && <button type="button" className="primary" disabled={busy || !qty} onClick={paper}>{busy ? "Opening…" : "Paper trade"}</button>}
      </div>
      {s.dir !== "EXIT" && (
        <button type="button" className="linkish small scr-kite" onClick={() => openTicket(s.ticket ? { ...s.ticket, order_type: "MARKET" } : { exchange: "NSE", tradingsymbol: s.symbol, transaction_type: "BUY", product: strat.product,
          order_type: strat.id === "ready" || strat.id === "buys" ? "SL" : "LIMIT", trigger_price: strat.id === "ready" || strat.id === "buys" ? s.entry : undefined,
          price: strat.id === "ready" || strat.id === "buys" ? roundTick(s.entry * 1.005) : last, quantity: qty })}>
          Real order: open a Kite ticket (you confirm it there) →
        </button>
      )}
      {msg && <p className="ok-note">{msg}</p>}
      <ErrorNote error={err} />
    </aside>
  );
}

// `only`: the strategy ids this tab shows (one per tab; Darvas groups its three views behind a switch)
export default function Screener({ darvas, orb, orbError, orbProgress, openStock, openTicket, only = ["orb"] }) {
  const key = `st.scr.${only.join("-")}`;
  const [sel, setSel] = useState(() => {
    try { const v = localStorage.getItem(key); return only.includes(v) ? v : only[0]; } catch { return only[0]; }
  });
  const [pick, setPick] = useState(null);
  useEffect(() => { try { localStorage.setItem(key, sel); } catch { /* private window */ } }, [key, sel]);
  const strat = STRATS.find((x) => x.id === sel) || STRATS[0];
  const rows = useMemo(() => setups(strat.id, darvas, orb), [strat.id, darvas, orb]);
  const cur = rows.find((r) => r.symbol === pick) || rows[0];
  const counts = { q: rows.length, f: rows.filter((r) => r.status === "Forming").length, t: rows.filter((r) => r.status === "Triggered").length };
  const capital = orb?.controller?.c0 || 500000;
  const notional = orb?.settings?.notional || 100000;
  const empty = sel === "orb"
    ? orbError ? "ORB needs a Kite session." : !orb ? (orbProgress?.running ? `Loading 5-minute history ${orbProgress.done}/${orbProgress.total}…` : "Loading today's ORB shortlist…")
      : "No stock passed today's opening filters." : "Nothing in the latest daily scan.";

  return (
    <div className="scr fade-in">
      <section className="card scr-filters">
        {only.length > 1 ? (
          <Segmented value={sel} onChange={(v) => { setSel(v); setPick(null); }} label="View"
            options={STRATS.filter((x) => only.includes(x.id)).map((x) => ({ id: x.id, label: x.short || x.name }))} />
        ) : <b className="scr-name">{strat.name}</b>}
        <span className="chip-btn on">{strat.kind}</span>
        <span className="chip-btn">NSE Cash</span>
        <span className="chip-btn">{{ orb: "F&O + momentum stocks", ready: "Top 500 liquid", buys: "Top 500 liquid", sells: "Top 500 liquid" }[sel] || "Scanned universe"}</span>
        <span className="muted small scr-asof">{sel === "orb" ? (orb ? `ORB · ${orb.phase} · auto every 15 s` : "") : darvas?.generated_at ? `Daily scan ${new Date(darvas.generated_at).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" })}` : ""}</span>
      </section>

      <div className="scr-grid">
        <div className="scr-main">
          <div className="scr-counts">
            <div className="card"><small>Qualified</small><b>{counts.q}</b></div>
            <div className="card"><small>Forming</small><b>{counts.f}</b></div>
            <div className="card"><small>Triggered</small><b className={counts.t ? "up" : ""}>{counts.t}</b></div>
          </div>
          <section className="card scr-table">
            {rows.length ? (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr><th>Stock</th><th>Direction</th><th className="num">Entry</th><th className="num">Stop loss</th><th className="num">Target</th><th className="num">R : R</th><th>Status</th><th /></tr>
                  </thead>
                  <tbody>
                    {rows.map((r) => (
                      <tr key={r.symbol} className={`clickable${cur?.symbol === r.symbol ? " sel" : ""}`} onClick={() => setPick(r.symbol)}>
                        <td><span className="scr-sym"><Logo symbol={r.label || r.symbol} size={26} /><b>{r.symbol}</b></span></td>
                        <td><span className={`dir ${r.dir.toLowerCase()}`}>{r.dir}</span></td>
                        <td className="num">{r.entry != null ? num(r.entry) : "–"}</td>
                        <td className="num">{r.sl ? num(r.sl) : "–"}</td>
                        <td className="num">{r.target ? num(r.target) : "–"}</td>
                        <td className="num">{rr(r) ? num(rr(r), 1) : "–"}</td>
                        <td><Badge tone={r.badgeTone ?? STATUS_TONE[r.status]}>{r.badge || r.status}</Badge></td>
                        <td className="num muted">›</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : <div className="empty">{empty}</div>}
          </section>
        </div>
        {cur ? <Detail key={sel + cur.symbol} s={cur} strat={strat} orb={orb} openStock={openStock} openTicket={openTicket} capital={capital} notional={notional} />
          : <aside className="card scr-detail"><p className="muted">Pick a setup to see its chart, checks and position size.</p></aside>}
      </div>
      <p className="footnote">
        Paper trade records a simulated position at the live bid / ask (stop and target watched every 10 s; MIS squared off at 15:20). Real orders only go through the Kite ticket, which you confirm.
        Triggered = the signal fired or the trade is on; Forming = one step from a signal (near the trigger, or one filter short); Watching = being tracked.
      </p>
    </div>
  );
}
