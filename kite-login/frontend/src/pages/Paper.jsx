import { Fragment, useMemo, useState } from "react";
import { api } from "../lib/api.js";
import { Sparkline } from "../components/charts.jsx";
import { inr, num, rupee, signed, tone } from "../lib/format.js";
import { useTicks } from "../lib/ticks.js";
import { Badge, DataTable, Empty, ErrorNote, Spinner, useLoad } from "../components/ui.jsx";
import { EquityChart } from "../components/charts.jsx";
import { WithLogo } from "../components/Logo.jsx";
import SideTag from "../components/SideTag.jsx";

// Portfolio & Risk: one paper portfolio made of the strategy desks: ₹1 lakh for the Multi Agent Team, ₹5 lakh and ₹2 lakh for the Agent Pipeline. Every
// figure is combined across them and marked live on Kite ticks: equity, today's P&L, open positions, closed trades,
// and the equity curve. Option and stock trades paper-traded by hand are listed with their own
// P&L, outside the strategies' capital. Paper only: nothing here sends an order to Kite.
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

// Live mark of a strategy trade: its last net P&L moved by the tick since the engine's last price, on the share still open.
// A stock trade streams as NSE:SYMBOL; an option trade carries its own key.
export const tickKey = (t) => t.key || `NSE:${t.symbol}`;
// A spread streams both its legs, and its live price is what it would cost to close: the short leg less the long one.
export const tickKeys = (t) => (t.legs ? t.legs.map((l) => l.key) : [tickKey(t)]);
export const tickOf = (t, ticks) => {
  if (!t.legs) return ticks[tickKey(t)];
  const prices = t.legs.map((l) => ticks[l.key]?.last_price);
  return prices.every((p) => p != null) ? { last_price: t.legs.reduce((a, l, i) => a - l.sign * prices[i], 0) } : undefined;
};

export function markTrade(t, tk) {
  const px = tk?.last_price ?? t.ltp;
  const pnl = t.net + (px != null && t.ltp != null ? (px - t.ltp) * t.qty * (t.left ?? 1) * dirOf(t) : 0);
  const stop = t.stop_now ?? t.stop;
  const risk = stop != null && px != null ? Math.max(0, (px - stop) * dirOf(t)) * t.qty * (t.left ?? 1) : 0;
  return { px, pnl, chg: tk?.change, pct: gainPct(t, pnl), risk, value: (px ?? t.entry) * t.qty * (t.left ?? 1) };
}

// Every desk's open trades, and each desk's equity / today's P&L moved by the live marks.
export function liveDesks(data, ticks) {
  const desks = (data?.desks || []).filter((d) => !d.error);
  const opens = desks.flatMap((d) => d.open.map((t) => ({ ...t, desk: d.id, deskName: d.name })));
  const rows = desks.map((d) => {
    const ms = opens.filter((t) => t.desk === d.id).map((t) => markTrade(t, tickOf(t, ticks)));
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

// Your paper capital and risk limits, shared by the multi-agent teams. They size nothing until you confirm them.
function RiskSettings() {
  const q = useLoad(api.riskSettings, []);
  const cur = q.data?.settings;
  const limits = q.data?.limits;
  const [editing, setEditing] = useState(false);
  const [capital, setCapital] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  if (!limits) return null;
  const n = Number(capital) || 0;
  const save = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.saveRiskSettings({ capital: n, confirmed });
      await q.reload(true);
      setEditing(false);
      setConfirmed(false);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="card">
      <header className="card-head">
        <h3>Team risk settings</h3>
        <span className="muted small">used by the Multi Agent Team</span>
      </header>
      {cur && !editing ? (
        <p className="muted small">
          Paper capital <b>{inr(cur.capital, 0)}</b> · at most <b>{inr(cur.max_risk_inr, 0)}</b> a trade ({cur.max_risk_pct}%) · daily loss limit <b>{inr(cur.daily_loss_inr, 0)}</b> ({cur.daily_loss_pct}%){" "}
          <button type="button" className="linklike" onClick={() => { setCapital(cur.capital); setEditing(true); }}>Change</button>
        </p>
      ) : (
        <form className="jd-form" onSubmit={save}>
          {!cur && <p className="small">The teams take no paper trade until you set your capital and confirm the limits.</p>}
          <label><span>Paper trading capital (₹)</span><input type="number" min="1" step="any" required value={capital} onChange={(e) => setCapital(e.target.value)} /></label>
          <label className="jd-inline">
            <input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />
            <span>I confirm {limits.max_risk_pct}% per trade{n ? ` (${inr(n * limits.max_risk_pct / 100, 0)})` : ""} and a {limits.daily_loss_pct}% daily loss limit{n ? ` (${inr(n * limits.daily_loss_pct / 100, 0)})` : ""}</span>
          </label>
          <button type="submit" className="primary" disabled={busy || !confirmed}>{busy ? "Saving…" : "Save"}</button>
          {cur && <button type="button" className="ghost" onClick={() => setEditing(false)}>Cancel</button>}
          <ErrorNote error={error || q.error} />
        </form>
      )}
    </section>
  );
}

// Telegram: new calls from every desk and the end-of-day report. The bot's token and chat id stay in signal_bot/secrets.env.
function TelegramCard() {
  const q = useLoad(api.telegramStatus, [], 30_000);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState(null);
  const t = q.data;
  if (!t) return null;
  const act = async (name, fn) => {
    setBusy(name);
    setError(null);
    try {
      await fn();
      await q.reload(true);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy("");
    }
  };
  return (
    <section className="card">
      <header className="card-head">
        <h3>Telegram</h3>
        <span className="muted small">{!t.configured ? "not set up: add the bot token and chat id to signal_bot/secrets.env" : t.enabled ? `on${t.channel ? " · chat and channel" : " · chat only"}` : "off"}</span>
      </header>
      <p className="muted small">
        New calls from every desk and the end-of-day report.
        {t.last_time && <> Last send {t.last_time.slice(11, 16)}: {t.last_ok ? "delivered" : (t.last_error || "failed")}.</>}
        {t.queued > 0 && <> {t.queued} waiting.</>}
      </p>
      <div className="row-actions">
        <button type="button" className={t.enabled ? "ghost" : "primary"} disabled={!t.configured || Boolean(busy)} onClick={() => act("toggle", () => api.telegramEnabled(!t.enabled))}>
          {busy === "toggle" ? "Saving…" : t.enabled ? "Turn off" : "Turn on"}
        </button>
        <button type="button" className="ghost" disabled={!t.configured || Boolean(busy)} onClick={() => act("test", api.telegramTest)}>{busy === "test" ? "Sending…" : "Send a test"}</button>
      </div>
      <ErrorNote error={error || q.error} />
    </section>
  );
}

export default function Paper() {
  const pf = useLoad(api.portfolio, [], 15_000);
  const po = useLoad(api.paperOptions, [], 15_000);
  const [only, setOnly] = useState(null);
  const [open, setOpen] = useState({});
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");

  const broken = (pf.data?.desks || []).filter((d) => d.error);
  const pfOpen = (pf.data?.desks || []).flatMap((d) => d.open || []);
  const optPos = po.data?.positions || [];
  const optOpen = optPos.filter((p) => p.status === "open");
  const { ticks, live } = useTicks([...pfOpen.flatMap(tickKeys), ...optOpen.flatMap((p) => p.legs.map((l) => l.key))]);

  const mark = (t) => markTrade(t, tickOf(t, ticks));
  const legPx = (l) => ticks[l.key]?.last_price ?? l.ltp ?? l.entry;
  const optNet = (p) => (p.status !== "open" ? p.net
    : (p.net ?? 0) + p.legs.reduce((a, l) => a + sgn(l) * (legPx(l) - (l.ltp ?? l.entry)) * l.qty, 0));

  const { desks, opens, rows, sum, capital, equity, today: todayPnl } = liveDesks(pf.data, ticks);
  const realized = sum((r) => r.realized);
  const unreal = sum((r) => r.unrealLive);
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
    ...optPos.filter((p) => p.status === "closed").map((p) => ({ id: p.id, desk: "manual", deskName: p.kind === "equity" ? "Stocks (manual)" : "Options (manual)",
      symbol: `${p.underlying} · ${p.name}`, when: p.closed.slice(0, 10), entry_time: p.opened.slice(11, 16), exit_time: p.closed.slice(11, 16),
      net: p.net, exit_reason: p.exit_reason || "closed by you", pct: p.premium ? (p.net / Math.abs(p.premium)) * 100 : null, opt: p })),
  ], [pf.data, po.data]); // eslint-disable-line react-hooks/exhaustive-deps

  const act = async (name, fn) => {
    setBusy(name);
    setErr("");
    try {
      await fn();
      await Promise.all([pf.reload(true), po.reload(true)]);
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy("");
    }
  };

  if (!pf.data) return <section className="card orb-loading">{pf.error ? <ErrorNote error={pf.error} /> : <Spinner />}</section>;
  const shown = (r) => !only || r.desk === only;
  const openShown = opens.filter(shown);
  const manualShown = !only || only === "manual" ? optOpen : [];

  return (
    <div className="paper-desk pf no-side">
      <div className="pd-main">
        <ErrorNote error={err || po.error} />
        {broken.map((d) => <ErrorNote key={d.id} error={`${d.name}: ${d.error}`} />)}
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
                <span className="small">today <b className={tone(r.todayLive)}>{rupee(r.todayLive)}</b>{r.open.length ? ` · ${r.open.length} open` : ""}</span>
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
                        <td><WithLogo symbol={t.symbol}><b>{t.symbol}</b></WithLogo> <SideTag symbol={t.symbol} side={t.side} /></td>
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
                          <td><span className="src opt">{p.kind === "equity" ? "Stocks (manual)" : "Options (manual)"}</span></td>
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
          ) : <Empty>No open position. Each strategy opens its paper trades by itself on its own capital.</Empty>}
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
            <span className="muted small">{closed.filter(shown).length} trades{manualClosed ? ` · manual option / stock trades ${rupee(manualClosed)}, outside the strategies' capital` : ""}</span>
          </header>
          <DataTable rowKey={(r) => r.id} rows={closed.filter(shown)} initialSort={{ key: "when", dir: "desc" }} empty="No closed paper trade yet."
            columns={[
              { key: "when", label: "Day", render: (r) => day(r.when) },
              { key: "symbol", label: "Stock", render: (r) => <><WithLogo symbol={r.symbol}><b>{r.symbol}</b></WithLogo>{r.side && <> <SideTag symbol={r.symbol} side={r.side} /></>}</> },
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
        <RiskSettings />
        <TelegramCard />
      </div>

    </div>
  );
}
