import { Fragment, useMemo, useState } from "react";
import { api } from "../lib/api.js";
import { Sparkline } from "../components/charts.jsx";
import { inr, num, rupee, signed, tone } from "../lib/format.js";
import { useTicks } from "../lib/ticks.js";
import { Badge, DataTable, Empty, ErrorNote, Modal, Spinner, useLoad } from "../components/ui.jsx";
import { EquityChart } from "./Backtest.jsx";

// Portfolio & Risk: one paper portfolio made of the strategy desks, ₹1 lakh each (ORB, Darvas). Every
// figure is combined across them and marked live on Kite ticks: equity, today's P&L, open positions, closed trades,
// the equity curve and the risk controls. Option and screener trades paper-traded by hand are listed with their own
// P&L, outside the strategies' capital. Paper only: nothing here sends an order to Kite.
const STATE = { ACTIVE: ["accent", "Trading"], CLOSED: ["", "Day closed"], HALTED_PROFIT: ["buy", "Profit stop hit"],
  HALTED_LOSS: ["sell", "Loss stop hit"], HALTED_MANUAL: ["sell", "Stopped by you"] };
const day = (s) => (s && s !== "start" ? new Date(`${s.slice(0, 10)}T00:00:00`).toLocaleDateString("en-IN", { day: "numeric", month: "short" }) : "start");
// IST wall clock, whatever the browser's time zone: UTC + 5:30
const istIso = () => new Date(Date.now() + 330 * 60_000).toISOString();
const todayIso = () => istIso().slice(0, 10);
const sgn = (l) => (l.side === "BUY" ? 1 : -1);
const dirOf = (t) => (t.side === "SHORT" ? -1 : 1);
const pctOf = (v, c) => (c ? (v / c) * 100 : 0);
const pctCell = (v) => (v == null || !Number.isFinite(v) ? <span className="muted">–</span> : <span className={tone(v)}>{signed(v)}%</span>);
const gainPct = (t, pnl) => (t.entry && t.qty ? (pnl / (t.entry * t.qty)) * 100 : null);
const flash = (v) => (v >= 0 ? "flash-up" : "flash-down");

// Live mark of a strategy trade: intraday trades move their last net P&L by the tick since the engine's last price,
// on the share still open; swing positions are marked from entry.
export function markTrade(t, tk) {
  const px = tk?.last_price ?? t.ltp;
  const pnl = t.kind === "intraday"
    ? t.net + (px != null && t.ltp != null ? (px - t.ltp) * t.qty * (t.left ?? 1) * dirOf(t) : 0)
    : (px - t.entry) * t.qty;
  const stop = t.stop_now ?? t.stop;
  const risk = stop != null && px != null ? Math.max(0, (px - stop) * dirOf(t)) * t.qty * (t.left ?? 1) : 0;
  return { px, pnl, chg: tk?.change, pct: gainPct(t, pnl), risk, value: (px ?? t.entry) * t.qty * (t.left ?? 1) };
}

// Every desk's open trades, and each desk's equity / today's P&L moved by the live marks.
export function liveDesks(data, ticks) {
  const desks = (data?.desks || []).filter((d) => !d.error);
  const opens = desks.flatMap((d) => d.open.map((t) => ({ ...t, desk: d.id, deskName: d.name, kind: d.kind })));
  const rows = desks.map((d) => {
    const ms = opens.filter((t) => t.desk === d.id).map((t) => markTrade(t, ticks[`NSE:${t.symbol}`]));
    const unreal = ms.reduce((a, m) => a + m.pnl, 0);
    const moved = unreal - d.unrealized;
    return { ...d, unrealLive: unreal, equityLive: d.equity + moved, todayLive: (d.today ?? 0) + moved,
      risk: ms.reduce((a, m) => a + m.risk, 0), value: ms.reduce((a, m) => a + m.value, 0) };
  });
  const sum = (f) => rows.reduce((a, r) => a + f(r), 0);
  return { desks, opens, rows, sum, capital: sum((r) => r.capital), equity: sum((r) => r.equityLive), today: sum((r) => r.todayLive) };
}

// Equity of the desks combined, by date: each desk's last mark on or before the date (its capital before its first).
export function combinedCurve(desks) {
  const curves = desks.map((d) => ({ cap: d.capital, pts: d.curve.filter((c) => c[0] !== "start") }));
  const dates = [...new Set(curves.flatMap((c) => c.pts.map((p) => p[0])))].sort();
  const start = curves.reduce((a, c) => a + c.cap, 0);
  return [["start", start], ...dates.map((dt) => [dt, curves.reduce((a, c) => a + ([...c.pts].reverse().find((p) => p[0] <= dt)?.[1] ?? c.cap), 0)])];
}

function Meter({ label, tip, value, max, text, sub, cls }) {
  const pct = max > 0 ? Math.max(0, Math.min(100, (value / max) * 100)) : 0;
  return (
    <div className="rc-row" title={tip}>
      <div className="rc-top"><span>{label}</span><b className={cls}>{text}</b></div>
      <div className={`rc-bar ${cls || ""}`}><i style={{ width: `${pct}%` }} /></div>
      <small className="muted">{sub ?? `${num(pct, 0)}%`}</small>
    </div>
  );
}

export default function Paper({ go }) {
  const pf = useLoad(api.portfolio, [], 15_000);
  const pp = useLoad(api.orbPaper, [], 15_000);
  const po = useLoad(api.paperOptions, [], 15_000);
  const [only, setOnly] = useState(null);
  const [open, setOpen] = useState({});
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");

  const broken = (pf.data?.desks || []).filter((d) => d.error);
  const pfOpen = (pf.data?.desks || []).flatMap((d) => d.open || []);
  const optPos = po.data?.positions || [];
  const optOpen = optPos.filter((p) => p.status === "open");
  const { ticks, live } = useTicks([...pfOpen.map((t) => `NSE:${t.symbol}`), ...optOpen.flatMap((p) => p.legs.map((l) => l.key))]);

  const mark = (t) => markTrade(t, ticks[`NSE:${t.symbol}`]);
  const legPx = (l) => ticks[l.key]?.last_price ?? l.ltp ?? l.entry;
  const optNet = (p) => (p.status !== "open" ? p.net
    : (p.net ?? 0) + p.legs.reduce((a, l) => a + sgn(l) * (legPx(l) - (l.ltp ?? l.entry)) * l.qty, 0));

  const { desks, opens, rows, sum, capital, equity, today: todayPnl } = liveDesks(pf.data, ticks);
  const realized = sum((r) => r.realized);
  const unreal = sum((r) => r.unrealLive);
  const openRisk = sum((r) => r.risk);
  const exposure = sum((r) => r.value);
  const manualOpen = optOpen.reduce((a, p) => a + optNet(p), 0);
  const manualClosed = optPos.filter((p) => p.status === "closed").reduce((a, p) => a + p.net, 0);

  const curve = useMemo(() => {
    if (!desks.length) return [];
    const c = combinedCurve(desks);
    // the starting point sits the day before the first session, so the axis never shows that date twice
    const d0 = new Date(`${desks.map((d) => d.started).sort()[0]}T00:00:00Z`);
    const first = new Date(d0.getTime() - 86_400_000).toISOString().slice(0, 10);
    return c.map((p) => [p[0] === "start" ? first : p[0], p[1]]);
  }, [pf.data]); // eslint-disable-line react-hooks/exhaustive-deps
  const t0 = todayIso();
  const curveLive = curve.length ? [...curve.filter((p) => p[0] < t0), [t0, equity]] : [];
  const maxDd = useMemo(() => {
    let peak = 0, dd = 0;
    for (const [, v] of curveLive) { peak = Math.max(peak, v); dd = Math.max(dd, peak ? (peak - v) / peak : 0); }
    return dd * 100;
  }, [curveLive]);

  // day-by-day net per strategy, from each desk's equity marks
  const daily = useMemo(() => {
    const by = {};
    for (const d of desks) {
      let prev = d.capital;
      for (const [dt, eq] of d.curve) {
        if (dt === "start") continue;
        (by[dt] ||= { day: dt, total: 0 })[d.id] = eq - prev;
        by[dt].total += eq - prev;
        prev = eq;
      }
    }
    return Object.values(by);
  }, [pf.data]); // eslint-disable-line react-hooks/exhaustive-deps

  const closed = useMemo(() => [
    ...desks.flatMap((d) => d.closed.map((t) => ({ ...t, desk: d.id, deskName: d.name, when: t.session || t.exit_date,
      id: `${d.id}${t.symbol}${t.session || t.entry_date}${t.entry_time}${t.exit}`, pct: gainPct(t, t.net) }))),
    ...optPos.filter((p) => p.status === "closed").map((p) => ({ id: p.id, desk: "manual", deskName: p.kind === "equity" ? "Screener (manual)" : "Options (manual)",
      symbol: `${p.underlying} · ${p.name}`, when: p.closed.slice(0, 10), entry_time: p.opened.slice(11, 16), exit_time: p.closed.slice(11, 16),
      net: p.net, exit_reason: p.exit_reason || "closed by you", pct: p.premium ? (p.net / Math.abs(p.premium)) * 100 : null, opt: p })),
  ], [pf.data, po.data]); // eslint-disable-line react-hooks/exhaustive-deps

  // ORB's own controller: daily profit pause, loss limit, pause and emergency stop
  const orbDay0 = pp.data?.days?.[0];
  const orbLive = Boolean(orbDay0 && !orbDay0.final && orbDay0.session === t0);
  const paused = Boolean(orbDay0?.settings?.paused);
  const halted = orbDay0?.state?.startsWith("HALTED");
  const orbToday = rows.find((r) => r.id === "orb")?.todayLive ?? 0;
  const orbCap = rows.find((r) => r.id === "orb")?.capital || 100000;
  const lossAmt = orbCap * 0.05;

  // Darvas desk: 1% of equity at risk a trade, at most 20% of equity in one stock, at most 5 open positions
  const dv = rows.find((r) => r.id === "darvas");
  const dEq = dv?.equityLive || dv?.capital || 100000;
  const dOpen = opens.filter((t) => t.desk === "darvas");
  const dLargest = dOpen.reduce((a, t) => Math.max(a, mark(t).value), 0);
  const dPending = desks.find((d) => d.id === "darvas")?.pending?.length ?? 0;

  const act = async (name, fn) => {
    setBusy(name);
    setErr("");
    try {
      await fn();
      await Promise.all([pf.reload(true), pp.reload(true), po.reload(true)]);
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy("");
    }
  };
  const emergency = () => act("kill", async () => {
    const errs = [];
    if (orbLive && !halted) await api.orbKill().catch((e) => errs.push(`ORB: ${e.message}`));
    for (const p of optOpen) await api.paperOptionsExit(p.id).catch((e) => errs.push(`${p.name}: ${e.message}`));
    setConfirm(false);
    if (errs.length) throw new Error(errs.join(" · "));
  });

  if (!pf.data) return <section className="card orb-loading">{pf.error ? <ErrorNote error={pf.error} /> : <Spinner />}</section>;
  const shown = (r) => !only || r.desk === only;
  const openShown = opens.filter(shown);
  const pending = desks.flatMap((d) => d.pending.map((o) => ({ ...o, desk: d.id, deskName: d.name }))).filter(shown);
  const manualShown = !only || only === "manual" ? optOpen : [];

  return (
    <div className="paper-desk pf">
      <div className="pd-main">
        <section className="card pd-kpis pf-kpis">
          <span><small>Total equity</small><b>{inr(equity, 0)}</b><em className="muted">capital {inr(capital, 0)} · <span className={tone(equity - capital)}>{rupee(equity - capital)} ({signed(pctOf(equity - capital, capital))}%)</span></em></span>
          <span><small>Today's P&L</small><b key={Math.round(todayPnl)} className={`${tone(todayPnl)} ${flash(todayPnl)}`}>{rupee(todayPnl)}</b><em className={tone(todayPnl)}>{signed(pctOf(todayPnl, capital))}% of capital</em></span>
          <span><small>Realised</small><b className={tone(realized)}>{rupee(realized)}</b><em className={tone(realized)}>{signed(pctOf(realized, capital))}%</em></span>
          <span><small>Open P&L</small><b key={Math.round(unreal)} className={`${tone(unreal)} ${flash(unreal)}`}>{rupee(unreal)}</b><em className="muted">{opens.length} open · <span className={tone(unreal)}>{signed(pctOf(unreal, capital))}%</span></em></span>
          <span className="pd-acct">
            <Badge tone="accent">Paper</Badge>
            {opens.length + optOpen.length > 0 ? <span className={live ? "live-chip" : "muted small"}>{live ? <><span className="live-dot" /> Live ticks</> : "ticks connecting…"}</span> : <span className="muted small">no open position</span>}
          </span>
        </section>

        <section className="pdk-cards pf-strats">
          {rows.map((r) => {
            const chg = r.equityLive - r.capital;
            return (
              <button type="button" key={r.id} className={`pdk-card${only === r.id ? " active" : ""}`} onClick={() => setOnly(only === r.id ? null : r.id)}
                title={only === r.id ? "Show every strategy" : `Show only ${r.name} below`}>
                <span className="pdk-name">{r.name} · {inr(r.capital, 0)}</span>
                <b className="pdk-eq">{inr(r.equityLive, 0)}</b>
                <span className={`small ${tone(chg)}`}>{rupee(chg)} · {signed(pctOf(chg, r.capital))}%</span>
                <span className="small">today <b className={tone(r.todayLive)}>{rupee(r.todayLive)}</b>{r.open.length ? ` · ${r.open.length} open` : ""}{r.pending.length ? ` · ${r.pending.length} orders` : ""}</span>
                <div className="pdk-spark">{r.curve.length > 1 ? <Sparkline values={r.curve.map((c) => c[1])} height={34} /> : <span className="muted small">no trades yet</span>}</div>
                <span className="muted small">{r.stats.trades} trades{r.stats.win_rate != null ? ` · ${num(r.stats.win_rate, 0)}% won` : ""}{r.stats.profit_factor != null ? ` · PF ${num(r.stats.profit_factor)}` : ""}</span>
              </button>
            );
          })}
        </section>
        {only && (
          <div className="pf-filter muted small">
            Showing {only === "manual" ? "manual trades" : rows.find((r) => r.id === only)?.name} only ·{" "}
            <button type="button" className="linkish" onClick={() => setOnly(null)}>show all strategies</button>
          </div>
        )}

        <section className="card">
          <header className="card-head">
            <h3>Open positions</h3>
            <span className="muted small">{openShown.length + manualShown.length} open · LTP and P&L on live ticks</span>
          </header>
          {openShown.length + manualShown.length ? (
            <div className="table-wrap">
              <table className="table pd-table">
                <thead>
                  <tr><th>Stock</th><th>Strategy</th><th>Entry</th><th className="num">Qty</th><th className="num">Stop / target</th><th className="num">LTP</th><th className="num">P&L</th><th className="num">% gain</th></tr>
                </thead>
                <tbody>
                  {openShown.map((t) => {
                    const m = mark(t);
                    return (
                      <tr key={t.desk + t.symbol + (t.entry_date || t.session) + t.entry_time}>
                        <td><b>{t.symbol}</b> <span className={`dir ${(t.side || "LONG").toLowerCase()}`}>{t.side || "LONG"}</span></td>
                        <td><span className="src bot">{t.deskName}</span></td>
                        <td className="small">{t.entry_date ? `${day(t.entry_date)} ` : ""}{t.entry_time || ""} @ {num(t.entry)}</td>
                        <td className="num">{t.left != null && t.left < 1 ? <>{Math.round(t.qty * t.left)} <small className="muted">of {t.qty}</small></> : t.qty}</td>
                        <td className="num small">{num(t.stop_now ?? t.stop)} / {t.target ? num(t.target) : "trail"}</td>
                        <td className="num"><b key={m.px} className={m.chg == null ? "" : flash(m.chg)}>{num(m.px)}</b></td>
                        <td className="num"><b key={Math.round(m.pnl)} className={`${tone(m.pnl)} ${flash(m.pnl)}`}>{rupee(m.pnl)}</b></td>
                        <td className="num">{pctCell(m.pct)}</td>
                      </tr>
                    );
                  })}
                  {manualShown.map((p) => {
                    const net = optNet(p);
                    return (
                      <Fragment key={p.id}>
                        <tr className="opt-row">
                          <td>
                            <button type="button" className="linkish strong" onClick={() => setOpen((o) => ({ ...o, [p.id]: !o[p.id] }))} aria-expanded={Boolean(open[p.id])}>
                              {open[p.id] ? "▾" : "▸"} {p.underlying} · {p.name}
                            </button>
                          </td>
                          <td><span className="src opt">{p.kind === "equity" ? "Screener (manual)" : "Options (manual)"}</span></td>
                          <td className="small">{p.opened?.slice(11, 16)} · {p.premium >= 0 ? "credit" : "debit"} ₹{num(Math.abs(p.premium), 0)}</td>
                          <td className="num">{p.legs.reduce((a, l) => a + l.lots, 0)} lots</td>
                          <td className="num">
                            <button type="button" className="ghost small" disabled={busy === p.id} onClick={() => act(p.id, () => api.paperOptionsExit(p.id))}>
                              {busy === p.id ? "Exiting…" : "Exit"}
                            </button>
                          </td>
                          <td className="num muted">–</td>
                          <td className="num"><b key={Math.round(net)} className={`${tone(net)} ${flash(net)}`}>{rupee(net)}</b></td>
                          <td className="num">{pctCell(p.premium ? (net / Math.abs(p.premium)) * 100 : null)}</td>
                        </tr>
                        {open[p.id] && p.legs.map((l) => (
                          <tr key={l.key} className="leg-row">
                            <td><span className={`side-tag ${l.side === "BUY" ? "b" : "s"}`}>{l.side === "BUY" ? "B" : "S"}</span> {l.tradingsymbol}</td>
                            <td className="muted small">{l.expiry}</td>
                            <td className="small">@ {num(l.entry)}</td>
                            <td className="num">{l.qty}</td>
                            <td />
                            <td className="num"><span key={legPx(l)} className={ticks[l.key] ? flash(ticks[l.key].change) : ""}>{num(legPx(l))}</span></td>
                            <td className="num"><span className={tone(sgn(l) * (legPx(l) - l.entry))}>{rupee(sgn(l) * (legPx(l) - l.entry) * l.qty)}</span></td>
                            <td className="num">{pctCell(l.entry ? (sgn(l) * (legPx(l) - l.entry) / l.entry) * 100 : null)}</td>
                          </tr>
                        ))}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : <Empty>No open position. Each strategy opens its paper trades by itself on its own ₹1 lakh.</Empty>}
          {pending.length > 0 && (
            <ul className="pdk-orders">
              {pending.map((o) => (
                <li key={o.desk + o.symbol + o.signal_date}><b>{o.symbol}</b> <span className="muted">({o.deskName})</span> buy-stop {num(o.trigger)} (limit {num(o.limit)}), stop {num(o.stop)} · breakout {day(o.signal_date)} · {3 - o.valid_days} sessions left</li>
              ))}
            </ul>
          )}
        </section>

        <section className="card">
          <header className="card-head">
            <h3>Equity curve</h3>
            <span className="muted small">all strategies together · {inr(capital, 0)} capital · after charges · max drawdown {num(maxDd, 1)}%</span>
          </header>
          {curveLive.length > 1 ? <EquityChart points={curveLive} base={capital} height={210} /> : <Empty>The curve starts with the first trade.</Empty>}
        </section>

        <section className="card">
          <header className="card-head"><h3>Daily P&L</h3><span className="muted small">net per strategy, after charges</span></header>
          <DataTable rowKey={(r) => r.day} rows={daily} initialSort={{ key: "day", dir: "desc" }} empty="No trading day recorded yet."
            columns={[
              { key: "day", label: "Day", render: (r) => day(r.day) },
              ...desks.map((d) => ({ key: d.id, label: d.name, align: "num", sort: (r) => r[d.id] ?? 0,
                render: (r) => (r[d.id] == null ? <span className="muted">–</span> : <span className={tone(r[d.id])}>{rupee(r[d.id])}</span>) })),
              { key: "total", label: "Total", align: "num", render: (r) => <b className={tone(r.total)}>{rupee(r.total)}</b> },
              { key: "pct", label: "% of capital", align: "num", sort: (r) => r.total, render: (r) => pctCell(pctOf(r.total, capital)) },
            ]} />
        </section>

        <section className="card">
          <header className="card-head">
            <h3>Closed trades</h3>
            <span className="muted small">{closed.filter(shown).length} trades{manualClosed ? ` · manual option / screener trades ${rupee(manualClosed)}, outside the strategies' capital` : ""}</span>
          </header>
          <DataTable rowKey={(r) => r.id} rows={closed.filter(shown)} initialSort={{ key: "when", dir: "desc" }} empty="No closed paper trade yet."
            columns={[
              { key: "when", label: "Day", render: (r) => day(r.when) },
              { key: "symbol", label: "Stock", render: (r) => <><b>{r.symbol}</b>{r.side && <> <span className={`dir ${r.side.toLowerCase()}`}>{r.side}</span></>}</> },
              { key: "deskName", label: "Strategy", render: (r) => <span className={`src ${r.desk === "manual" ? "opt" : "bot"}`}>{r.deskName}</span> },
              { key: "entry_time", label: "In → out", render: (r) => (r.opt ? `${r.entry_time} → ${r.exit_time}`
                : `${r.entry_date && r.entry_date !== r.when ? `${day(r.entry_date)} ` : ""}${r.entry_time || ""} @ ${num(r.entry)} → ${r.exit_time || ""} @ ${num(r.exit)}`) },
              { key: "r", label: "R", align: "num", render: (r) => (r.r == null ? "–" : <span className={tone(r.r)}>{signed(r.r)}R</span>) },
              { key: "net", label: "Net P&L", align: "num", render: (r) => <b className={tone(r.net)}>{rupee(r.net)}</b> },
              { key: "pct", label: "% gain", align: "num", render: (r) => pctCell(r.pct) },
              { key: "exit_reason", label: "Exit", render: (r) => (r.exit_reason ? <Badge tone={r.net >= 0 ? "buy" : "sell"}>{r.exit_reason}</Badge> : "–") },
              { key: "x", label: "", nosort: true, render: (r) => r.opt && (
                <button type="button" className="leg-x" title="Remove from the journal" onClick={() => act(r.id, () => api.paperOptionsDelete(r.id))}>×</button>) },
            ]} />
        </section>
      </div>

      <aside className="card pd-risk">
        <h3>Risk controls</h3>
        <p className="muted small">One ₹{num(capital / 1e5, 0)} lakh paper portfolio: ₹1 lakh for each strategy, each with its own limits, marked on live prices.</p>

        <h4 className="rc-group">Portfolio</h4>
        <Meter label="Exposure" tip="Market value of the open positions as a share of total capital" value={exposure} max={capital}
          text={`${num(pctOf(exposure, capital), 1)}%`} sub={`${inr(exposure, 0)} of ${inr(capital, 0)}`} />
        <Meter label="Open risk" tip="Loss if every open position hits its current stop" value={openRisk} max={capital * 0.03}
          text={inr(openRisk, 0)} cls={openRisk > capital * 0.03 ? "down" : ""} sub={`${num(pctOf(openRisk, capital), 2)}% of capital to the stops · limit 3%`} />
        <Meter label="Drawdown from peak" tip="Fall of the combined equity from its highest mark" value={maxDd} max={12}
          text={`${num(maxDd, 1)}%`} cls={maxDd > 8 ? "down" : ""} sub="largest so far · target ≤ 12%" />

        <h4 className="rc-group">ORB <small className="muted">₹{num(orbCap / 1e5, 0)} lakh · intraday</small></h4>
        <Meter label="Daily profit pause" tip="ORB stops for the day once its net P&L reaches ₹7,000" value={Math.max(0, orbToday)} max={7000}
          text={`${inr(Math.max(0, orbToday), 0)} / ₹7,000`} cls="up" />
        <Meter label="Daily loss limit" tip="ORB stops for the day at a loss of 5% of its capital" value={Math.max(0, -orbToday)} max={lossAmt}
          text={`5% (${inr(lossAmt, 0)})`} cls="down" sub={`used ${inr(Math.max(0, -orbToday), 0)}`} />
        <div className="rc-state">
          <span>ORB today</span>
          <Badge tone={paused ? "amber" : STATE[orbDay0?.state]?.[0]}>{paused && !halted ? "Entries paused" : STATE[orbDay0?.state]?.[1] || "–"}</Badge>
        </div>

        <h4 className="rc-group">Darvas <small className="muted">₹{num((dv?.capital || 100000) / 1e5, 0)} lakh · swing</small></h4>
        <Meter label="Open positions" tip="At most 5 Darvas positions at a time; a sixth breakout is skipped" value={dOpen.length} max={5}
          text={`${dOpen.length} / 5`} cls={dOpen.length >= 5 ? "down" : ""} sub={`${dPending} buy order${dPending === 1 ? "" : "s"} pending (valid 3 sessions)`} />
        <Meter label="Largest position" tip="No more than 20% of the desk's equity in one stock" value={dLargest} max={dEq * 0.2}
          text={`${num(pctOf(dLargest, dEq), 1)}% / 20%`} cls={dLargest > dEq * 0.2 ? "down" : ""} sub={`risk 1% of equity (${inr(dEq * 0.01, 0)}) a trade · trailing box stop`} />
        <div className="rc-actions">
          <button type="button" className="ghost" disabled={!orbLive || halted || busy === "pause"} onClick={() => act("pause", () => api.orbPause(!paused))}
            title={orbLive ? "Open ORB trades keep running; no new ORB entry is taken while paused" : "Available during the live session"}>
            {paused ? "▶ Resume ORB entries" : "❚❚ Pause ORB entries"}
          </button>
          <button type="button" className="danger" disabled={busy === "kill" || (!(orbLive && !halted) && !optOpen.length)} onClick={() => setConfirm(true)}>
            ⚠ Emergency stop
          </button>
        </div>
        <button type="button" className="ghost small" onClick={() => go("options")}>Paper-trade an option strategy →</button>
        {manualOpen !== 0 && <p className="muted small">Manual option / screener trades open: <b className={tone(manualOpen)}>{rupee(manualOpen)}</b> (not part of the ₹1 lakh strategy capital).</p>}
        <ErrorNote error={err || po.error || pp.error} />
        {broken.map((d) => <ErrorNote key={d.id} error={`${d.name}: ${d.error}`} />)}
      </aside>

      {confirm && (
        <Modal title="Emergency stop" onClose={() => setConfirm(false)}
          footer={(
            <>
              <button type="button" className="ghost" onClick={() => setConfirm(false)}>Cancel</button>
              <button type="button" className="danger" disabled={busy === "kill"} onClick={emergency}>{busy === "kill" ? "Stopping…" : "Stop everything"}</button>
            </>
          )}>
          <p>This closes now:</p>
          <ul className="plain">
            {orbLive && !halted && <li>Every open ORB trade, at the last price, and ORB halted for the rest of today.</li>}
            {optOpen.length > 0 && <li>{optOpen.length} manual option / screener position{optOpen.length === 1 ? "" : "s"}, at the live bid / ask.</li>}
          </ul>
          <p className="muted small">Darvas positions exit on their trailing box stop and are not closed here. Paper only: no Kite order is sent. This cannot be undone for today.</p>
        </Modal>
      )}
    </div>
  );
}
