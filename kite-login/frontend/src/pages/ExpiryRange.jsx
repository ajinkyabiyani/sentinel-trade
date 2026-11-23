import { useEffect, useState } from "react";
import { api } from "../lib/api.js";
import { num, signed, tone } from "../lib/format.js";
import { useTicks } from "../lib/ticks.js";
import { Badge, DataTable, Empty, ErrorNote, Segmented, Spinner, useLoad } from "../components/ui.jsx";
import { Logo, WithLogo } from "../components/Logo.jsx";

// Expiry Range Agent: each week it predicts where Nifty and Bank Nifty close on Nifty's weekly expiry, three ways, and checks itself.
const KEY = { NIFTY: "NSE:NIFTY 50", BANKNIFTY: "NSE:NIFTY BANK" };
const TONE = { iv: "accent", straddle: "amber", oi: "violet" };
const DAY = new Intl.DateTimeFormat("en-IN", { weekday: "short", day: "numeric", month: "short" });
const day = (iso) => DAY.format(new Date(`${iso.slice(0, 10)}T00:00:00`));

function RangeChart({ x, methods, live }) {
  const rs = Object.entries(x.ranges);
  const lo = Math.min(...rs.map(([, r]) => r.lo), live ?? x.spot), hi = Math.max(...rs.map(([, r]) => r.hi), live ?? x.spot);
  const pad = (hi - lo) * 0.14, a = lo - pad, b = hi + pad;   // room at both ends for labels set outside a narrow band
  const pos = (v) => `${((v - a) / (b - a)) * 100}%`;
  const now = live ?? x.spot;
  return (
    <div className="er-chart">
      {rs.map(([m, r]) => {
        const inside = now >= r.lo && now <= r.hi;
        return (
          <div key={m} className="er-row">
            <span className="er-mlabel">{methods[m]}</span>
            <div className="er-track">
              <span className={`er-band ${TONE[m]}`} style={{ left: pos(r.lo), width: `calc(${pos(r.hi)} - ${pos(r.lo)})` }}>
                <em className="er-lo">{num(r.lo, 0)}</em><em className="er-hi">{num(r.hi, 0)}</em>
              </span>
              <span className="er-now" style={{ left: pos(now) }} />
            </div>
            <span className={`er-in ${inside ? "up" : "down"}`}>{inside ? "inside now" : "outside now"}</span>
          </div>
        );
      })}
      <div className="er-row er-axis"><span /><div className="er-track bare"><span className="er-now-label" style={{ left: pos(now) }}>{num(now, 0)}</span></div><span /></div>
    </div>
  );
}

function WeekCard({ sym, rec, d, ticks, preview }) {
  const x = rec.indices[sym];
  const live = ticks[KEY[sym]]?.last_price;
  const move = live && x.spot ? (live / x.spot - 1) * 100 : null;
  return (
    <section className="card er-card">
      <header className="card-head">
        <div className="row"><Logo symbol={sym} size={26} /><h3>{d.indices[sym]}</h3></div>
        <span className="muted small">close on <b>{day(rec.expiry)}</b>{sym === "BANKNIFTY" && x.own_expiry && x.own_expiry !== rec.expiry ? ` · its own expiry is ${day(x.own_expiry)}` : ""}</span>
      </header>
      <div className="er-facts">
        <span><small>{preview ? "Spot now" : "Spot at prediction"}</small><b>{num(x.spot)}</b></span>
        <span><small>Live</small><b className={tone(move)}>{live ? num(live) : "–"}</b>{move != null && <em className={tone(move)}>{signed(move)}%</em>}</span>
        <span><small>ATM IV</small><b>{x.atm_iv != null ? `${num(x.atm_iv, 1)}%` : "–"}</b></span>
        <span><small>Straddle</small><b>{x.straddle != null ? num(x.straddle) : "–"}</b></span>
        <span><small>PCR</small><b>{x.pcr != null ? num(x.pcr) : "–"}</b></span>
        <span><small>Max pain</small><b>{x.max_pain != null ? num(x.max_pain, 0) : "–"}</b></span>
      </div>
      <RangeChart x={x} methods={d.methods} live={live} />
      {x.result && <p className="small">Closed at <b>{num(x.result.close)}</b>: {Object.entries(x.result.by).map(([m, r]) => `${d.methods[m]} ${r.won ? "won" : "missed"}`).join(" · ")}</p>}
    </section>
  );
}

function StatsTable({ live, replay, d }) {
  const keys = [...new Set([...replay, ...live].map((s) => `${s.index}|${s.method}`))];
  const get = (rows, k) => rows.find((s) => `${s.index}|${s.method}` === k);
  return (
    <div className="table-wrap">
      <table className="table er-stats">
        <thead>
          <tr><th>Index</th><th>Method</th><th className="num">Live · won</th><th className="num">Live win rate</th><th className="num">Replay · won</th><th style={{ width: 150 }}>Replay win rate</th><th className="num">Never left range</th><th className="num">Avg width</th><th className="num">Avg miss when wrong</th></tr>
        </thead>
        <tbody>
          {keys.map((k) => {
            const l = get(live, k), r = get(replay, k), any = r || l;
            return (
              <tr key={k}>
                <td><b>{any.label}</b></td>
                <td><span className={`er-dot ${TONE[any.method]}`} />{any.method_label}</td>
                <td className="num">{l ? `${l.won}/${l.weeks}` : "–"}</td>
                <td className="num">{l ? <b>{num(l.win_rate, 0)}%</b> : <span className="muted">no week yet</span>}</td>
                <td className="num">{r ? `${r.won}/${r.weeks}` : "–"}</td>
                <td>{r ? <span className="cw-rate-cell"><span className="cw-track"><span className="cw-won" style={{ width: `${r.win_rate}%` }} /><span className="cw-lost" style={{ width: `${100 - r.win_rate}%` }} /></span><b>{num(r.win_rate, 0)}%</b></span> : <span className="muted">not replayable</span>}</td>
                <td className="num">{(l || r) ? `${num((l || r).stayed_inside, 0)}%` : "–"}</td>
                <td className="num">{num(any.avg_width_pct, 1)}%</td>
                <td className="num">{num(any.avg_miss, 0)} pts</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

const rg = (x, m) => (x.ranges?.[m] ? `${num(x.ranges[m].lo, 0)}–${num(x.ranges[m].hi, 0)}` : "–");
const pct = (v) => (v == null ? "–" : `${num(v, 0)}%`);

function Stocks({ methods }) {
  const [fast, setFast] = useState(false);
  const q = useLoad(api.expiryRangeStocks, [fast], fast ? 5_000 : 60_000);
  const [find, setFind] = useState("");
  const d = q.data;
  const building = Boolean(d?.job?.running);
  useEffect(() => { setFast(building); }, [building]);
  const rows = (d?.rows || []).filter((r) => !find || r.symbol.includes(find.toUpperCase()));
  const cols = [
    { key: "symbol", label: "Stock", render: (r) => <WithLogo symbol={r.symbol} size={20}><b>{r.symbol}</b></WithLogo> },
    { key: "spot", label: "Spot", align: "num", render: (r) => num(r.spot), sort: (r) => r.spot },
    { key: "atm_iv", label: "ATM IV", align: "num", render: (r) => (r.atm_iv != null ? `${num(r.atm_iv, 1)}%` : "–"), sort: (r) => r.atm_iv ?? -1 },
    { key: "iv", label: methods.iv, align: "num", render: (r) => rg(r, "iv"), sort: (r) => (r.ranges?.iv ? (r.ranges.iv.hi - r.ranges.iv.lo) / r.spot : 0), nosort: false },
    { key: "straddle", label: methods.straddle, align: "num", render: (r) => rg(r, "straddle"), nosort: true },
    { key: "oi", label: methods.oi, align: "num", render: (r) => rg(r, "oi"), nosort: true },
    { key: "result", label: "Expiry close", align: "num", render: (r) => (r.result ? <>{num(r.result.close)} {Object.entries(r.result.by).map(([m, x]) => <Badge key={m} tone={x.won ? "buy" : "sell"}>{m === "iv" ? "IV" : m === "oi" ? "OI" : "S"} {x.won ? "✓" : "✗"}</Badge>)}</> : <span className="muted">open</span>), nosort: true },
    { key: "iv_win", label: "1-yr IV win", align: "num", render: (r) => <b>{pct(r.replay?.iv_win)}</b>, sort: (r) => r.replay?.iv_win ?? -1 },
    { key: "straddle_win", label: "1-yr straddle win", align: "num", render: (r) => pct(r.replay?.straddle_win), sort: (r) => r.replay?.straddle_win ?? -1 },
    { key: "width", label: "Avg IV width", align: "num", render: (r) => (r.replay ? `${num(r.replay.avg_width_pct, 1)}%` : "–"), sort: (r) => r.replay?.avg_width_pct ?? 0 },
  ];
  return (
    <section className="card">
      <header className="card-head">
        <div>
          <h3>F&amp;O stocks</h3>
          <span className="muted small">{d?.week ? `${d.week.recorded ? "This week's prediction" : "Preview from the live chains"} · close on ${day(d.week.expiry)} · ${d.rows.length} stocks${d.week.failed?.length ? ` · ${d.week.failed.length} without a chain` : ""}` : "every stock with options, on the same weekly window"}</span>
        </div>
        <input className="er-find" placeholder="Find a stock" value={find} onChange={(e) => setFind(e.target.value)} aria-label="Find a stock" />
      </header>
      {building && (
        <div className="er-job"><span className="er-job-bar"><span style={{ width: `${d.job.total ? (d.job.done / d.job.total) * 100 : 0}%` }} /></span>
          <span className="muted small">Reading option chains: {d.job.done} of {d.job.total}{d.job.purpose === "preview" ? " (preview, not recorded)" : ""}</span></div>
      )}
      {d && (
        <div className="table-wrap">
          <table className="table er-stats">
            <thead><tr><th>Method</th><th className="num">Live · won</th><th className="num">Live win rate</th><th className="num">Replay · won</th><th style={{ width: 150 }}>Replay win rate</th><th className="num">Never left range</th></tr></thead>
            <tbody>
              {Object.entries(methods).map(([m, label]) => {
                const l = d.live_stats.find((x) => x.method === m), r = d.replay_stats.find((x) => x.method === m);
                return (
                  <tr key={m}>
                    <td><span className={`er-dot ${TONE[m]}`} />{label}</td>
                    <td className="num">{l ? `${l.won}/${l.weeks}` : "–"}</td>
                    <td className="num">{l ? <b>{num(l.win_rate, 0)}%</b> : <span className="muted">no week yet</span>}</td>
                    <td className="num">{r ? `${num(r.won, 0)}/${num(r.weeks, 0)}` : "–"}</td>
                    <td>{r ? <span className="cw-rate-cell"><span className="cw-track"><span className="cw-won" style={{ width: `${r.win_rate}%` }} /><span className="cw-lost" style={{ width: `${100 - r.win_rate}%` }} /></span><b>{num(r.win_rate, 0)}%</b></span> : <span className="muted">not replayable</span>}</td>
                    <td className="num">{(l || r) ? `${num((l || r).stayed_inside, 0)}%` : "–"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <p className="muted small er-note">Replay: {d.replay_stocks} stocks over the last {d.replay_weeks} weekly expiries. A stock's implied volatility there is its own 20-day realised volatility times the day's market premium (India VIX over Nifty's realised volatility), since old option chains aren't stored.</p>
        </div>
      )}
      {q.loading && !d ? <Spinner /> : rows.length ? <DataTable columns={cols} rows={rows} rowKey={(r) => r.symbol} initialSort={{ key: "iv_win", dir: "desc" }} limit={60} />
        : <Empty>{building ? "The table fills in when every chain has been read." : "No stock prediction yet. It needs a Kite session."}</Empty>}
      <ErrorNote error={q.error} />
    </section>
  );
}

export default function ExpiryRange() {
  const q = useLoad(api.expiryRange, [], 60_000);
  const [hist, setHist] = useState("replay");
  const { ticks } = useTicks(Object.values(KEY));
  const d = q.data;
  if (q.loading && !d) return <section className="card orb-loading"><Spinner /></section>;
  if (!d) return <ErrorNote error={q.error} />;
  const week = d.current || d.preview;
  const rows = hist === "live" ? d.live : d.replay;
  return (
    <div className="tt-page er-page">
      <section className="er-banner">
        {d.current ? <span>This week's prediction, made {day(d.current.made_at)} at {d.current.made_at.slice(11, 16)}. It settles after {d.schedule.settle_at} on {day(d.current.expiry)}.</span>
          : <span><b>Preview</b> from the live option chain. The agent records its prediction by itself at {d.schedule.predict_at} on the first trading day of each expiry week, and settles it after {d.schedule.settle_at} on expiry day.</span>}
      </section>
      {week ? <div className="er-week">{Object.keys(d.indices).map((s) => week.indices[s] && <WeekCard key={s} sym={s} rec={week} d={d} ticks={ticks} preview={!d.current} />)}</div>
        : <Empty>No option chain right now. It needs a Kite session.</Empty>}

      <section className="card">
        <header className="card-head"><div><h3>Win rate: did the expiry close land inside the range?</h3><span className="muted small">live weeks since the agent started, and a one-year replay on daily candles (India VIX as the volatility; OI walls can't be replayed)</span></div></header>
        <StatsTable live={d.live_stats} replay={d.replay_stats} d={d} />
        <p className="muted small er-note">The 1σ range should hold about 68% of the time if implied volatility is fair; a higher win rate means options have been priced for more movement than happened. "Never left range" counts weeks where price stayed inside all week, not just at the close.</p>
      </section>

      <Stocks methods={d.methods} />

      <section className="card">
        <header className="card-head">
          <h3>Week by week</h3>
          <Segmented size="small" label="History" value={hist} onChange={setHist} options={[{ id: "replay", label: `Replay · ${d.replay.length}` }, { id: "live", label: `Live · ${d.live.length}` }]} />
        </header>
        {rows.length ? (
          <div className="table-wrap er-hist">
            <table className="table">
              <thead><tr><th>Expiry</th><th>Index</th><th className="num">Start</th><th className="num">IV</th>{Object.entries(d.methods).map(([m, l]) => <th key={m} className="num">{l}</th>)}<th className="num">Close</th><th className="num">Move</th></tr></thead>
              <tbody>
                {rows.flatMap((r) => Object.entries(r.indices).map(([s, x]) => (
                  <tr key={r.id + s}>
                    <td className="small">{day(r.expiry)}</td>
                    <td><b>{d.indices[s]}</b></td>
                    <td className="num">{num(x.spot, 0)}</td>
                    <td className="num muted">{x.atm_iv != null ? `${num(x.atm_iv, 1)}%` : "–"}</td>
                    {Object.keys(d.methods).map((m) => {
                      const rg = x.ranges[m], res = x.result?.by?.[m];
                      return <td key={m} className="num small">{rg ? <>{num(rg.lo, 0)}–{num(rg.hi, 0)} {res ? <Badge tone={res.won ? "buy" : "sell"}>{res.won ? "✓" : "✗"}</Badge> : null}</> : <span className="muted">–</span>}</td>;
                    })}
                    <td className="num">{x.result ? <b>{num(x.result.close, 0)}</b> : <span className="muted">open</span>}</td>
                    <td className={`num ${tone(x.result ? x.result.close - x.spot : null)}`}>{x.result ? `${signed((x.result.close / x.spot - 1) * 100)}%` : "–"}</td>
                  </tr>
                )))}
              </tbody>
            </table>
          </div>
        ) : <Empty>{hist === "live" ? "The first live week is recorded on the next trading day at 09:30." : d.replay_error || "No replay yet."}</Empty>}
      </section>
    </div>
  );
}
