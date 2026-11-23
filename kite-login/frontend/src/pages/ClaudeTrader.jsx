import { useEffect, useState } from "react";
import { api } from "../lib/api.js";
import { inr, num, rupee, signed } from "../lib/format.js";
import { useTicks } from "../lib/ticks.js";
import { ErrorNote, Spinner, useLoad } from "../components/ui.jsx";
import "../styles/claude-trader.css";

// Claude Trader: a desk of its own. No strategy is configured; every 15 minutes Claude reads live Kite data and decides by itself
// whether to buy a call or a put. Everything here is the desk's real state from /api/claude-trader: the ring counts down to the
// real next look, a step glows only while Claude is writing it. Paper only: nothing on this page sends an order.

const IST = { timeZone: "Asia/Kolkata", hour12: false };
const istClock = (d) => d.toLocaleTimeString("en-GB", IST);
const secOfDay = (d) => { const [h, m, s] = istClock(d).split(":").map(Number); return h * 3600 + m * 60 + s; };
const hhmm = (s) => { const [h, m] = s.split(":").map(Number); return h * 3600 + m * 60; };
const pnlCls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
const short = (sym) => String(sym || "").replace(/^([A-Z&-]+?)(\d{2}[A-Z]{3}|\d{2}[1-9OND]\d{2})(\d+(?:\.\d+)?)(CE|PE)$/, "$1 $3 $4");

function useNow() {
  const [t, setT] = useState(() => new Date());
  useEffect(() => { const i = setInterval(() => setT(new Date()), 1000); return () => clearInterval(i); }, []);
  return t;
}

function livePrice(e, ticks) {
  const tk = ticks[e.key];
  const fresh = tk?.last_price != null && tk.time && Date.now() - new Date(tk.time).getTime() < 60_000;
  return fresh ? tk.last_price : e.ltp;
}

const markOf = (e, ticks) => {
  if (e.status !== "open") return { px: e.exit, pnl: e.net };
  const px = livePrice(e, ticks);
  return { px, pnl: px == null ? null : (px * (1 - (e.slip || 0)) - e.entry) * e.qty };
};

function Ring({ left, total, busy, label, value }) {
  const r = 52, c = 2 * Math.PI * r;
  const frac = busy ? 0.22 : left == null ? 0 : Math.min(1, Math.max(0, 1 - left / total));
  return (
    <div className={`ctt-ring ${busy ? "busy" : ""}`} aria-label={label}>
      <svg viewBox="0 0 120 120">
        <circle className="track" cx="60" cy="60" r={r} fill="none" strokeWidth="2" />
        <circle className="arc" cx="60" cy="60" r={r} fill="none" strokeWidth="3" strokeDasharray={c} strokeDashoffset={c * (1 - frac)} />
      </svg>
      <div className="in"><b>{value}</b><span>{label}</span></div>
    </div>
  );
}

function Reasoning({ steps, roles, writing, live, decision, checks, paper, error }) {
  const by = Object.fromEntries((roles || []).map((r) => [r.role, r]));
  return (
    <>
      <ol className="ctt-steps">
        {steps.map((s) => {
          const r = by[s.id];
          const w = writing?.role === s.id ? writing : null;
          if (!r && !w) return live ? <li key={s.id} className="ctt-step wait"><div className="lbl"><span className="ctt-cap">{s.label}</span></div></li> : null;
          return (
            <li key={s.id} className={`ctt-step ${r ? r.verdict : "writing"}`}>
              <div className="lbl"><span className="ctt-cap">{s.label}</span>{r && <span className="v">{r.verdict}</span>}</div>
              <p>{r ? r.finding : w.finding}{!r && live && <i className="ctt-cur" />}</p>
            </li>
          );
        })}
      </ol>
      {!live && (error || decision) && (
        <div className="ctt-outcome">
          {error ? <span className="down">{error}</span> : decision.action !== "BUY" ? (
            <span><b>Stayed flat.</b> <span className="dim">{decision.thesis}</span></span>
          ) : (
            <>
              <div className="row">
                <span><b className={paper?.taken ? "acc" : "down"}>{paper?.taken ? "Bought" : "Refused"}</b> <b>{short(decision.instrument)}</b></span>
                <span className="dim">entry <b>{num(decision.entry)}</b></span>
                <span className="dim">stop <b>{num(decision.stop_loss)}</b></span>
                <span className="dim">target <b>{num(decision.target)}</b></span>
                <span className="dim">confidence <b>{decision.confidence}%</b></span>
              </div>
              {decision.invalidation && <div className="dim" style={{ marginTop: 6 }}>Wrong if: {decision.invalidation}</div>}
            </>
          )}
          {checks?.length > 0 && (
            <ul className="ctt-checks">
              {checks.map((c) => <li key={c.name}><span className={c.ok ? "ok" : "no"}>{c.ok ? "✓" : "✗"}</span> {c.name} · {c.note}</li>)}
            </ul>
          )}
        </div>
      )}
    </>
  );
}

function Position({ e, ticks }) {
  const open = e.status === "open";
  const { px, pnl } = markOf(e, ticks);
  const span = e.target - e.stop || 1;
  const at = (v) => `${Math.max(0, Math.min(100, ((v - e.stop) / span) * 100))}%`;
  return (
    <div className="ctt-pos">
      <div className="ctt-pos-h">
        <span><span className="ctt-side">{e.option_type === "PE" ? "PUT" : "CALL"}</span><b>{short(e.instrument)}</b></span>
        <span className={`pnl ${pnlCls(pnl)}`}>{pnl == null ? "–" : rupee(pnl)}</span>
      </div>
      <div className="sub">
        {open ? "open" : e.exit_reason} · {e.entry_at}{e.exit_at ? ` → ${e.exit_at}` : ""} · {e.lots} lot{e.lots === 1 ? "" : "s"} · max loss {inr(e.max_loss, 0)}{e.r != null ? ` · ${signed(e.r)}R` : ""}
      </div>
      <div className="ctt-track">
        <em style={{ left: at(e.entry) }} />
        {px != null && <i style={{ left: at(px) }} title={`${open ? "Last" : "Exit"} ${num(px)}`} />}
      </div>
      <div className="ctt-track-l"><span>stop {num(e.stop)}</span><span>entry {num(e.entry)} · {open ? "now" : "exit"} {num(px)}</span><span>target {num(e.target)}</span></div>
      {e.thesis && <p className="ctt-thesis">{e.thesis}</p>}
    </div>
  );
}

function journal(d) {
  const rows = d.runs.map((r) => {
    const dec = r.decision || {};
    const kind = r.error ? ["err", "error"] : dec.action === "BUY" ? (r.paper?.taken ? ["buy", "bought"] : ["err", "refused"]) : ["", "flat"];
    const text = r.error || (dec.action === "BUY" ? `${short(dec.instrument)} · ${r.paper?.taken ? dec.thesis : r.paper?.why}` : dec.thesis);
    return { at: r.at, key: r.id, cls: kind[0], tag: r.early ? `${kind[1]} · early` : kind[1], text: r.early ? `${r.early.join("; ")} → ${text || ""}` : text, run: r };
  });
  for (const a of d.activity) {
    if (a.kind !== "exit") continue;
    const loss = /net ₹-/.test(a.text);
    rows.push({ at: a.at, key: `x${a.at}${a.text}`, cls: loss ? "loss" : "win", tag: "exit", text: a.text.replace(/\.$/, "") });
  }
  return rows.sort((a, b) => (a.at < b.at ? 1 : a.at > b.at ? -1 : a.tag === "exit" ? -1 : 1));
}

export default function ClaudeTrader() {
  const [day, setDay] = useState(null);
  const q = useLoad(() => api.claudeTrader(day), [day], 4_000);
  const now = useNow();
  const d = q.data;
  const held = d?.positions || [];
  const { ticks } = useTicks(held.filter((e) => e.status === "open").map((e) => e.key));
  if (q.loading && !d) return <section className="card orb-loading"><Spinner /></section>;
  if (!d) return <ErrorNote error={q.error} />;

  const b = d.book;
  const today = d.day === d.today;
  const live = today ? d.live.run : null;
  const busy = !!live && live.stage !== "done";
  const shown = live || d.runs[0] || null;
  const t = istClock(now);
  const marketOpen = d.market_day && t >= "09:15:00" && t <= "15:30:00";
  const openPos = held.filter((e) => e.status === "open");
  const left = d.live.next.day === d.today ? hhmm(d.live.next.at) - secOfDay(now) : null;
  const countdown = left != null && left > 0 ? `${String(Math.floor(left / 60)).padStart(2, "0")}:${String(left % 60).padStart(2, "0")}` : null;
  const bookedToday = held.filter((e) => e.status === "closed" && e.exit_day === d.day).reduce((s, e) => s + (e.net || 0), 0);

  let stance, why;
  if (busy) {
    stance = <>{live.early ? "Early look" : "Reading the market"} <em>· {live.stage}</em></>;
    why = live.writing?.finding || (live.early ? `Woken by: ${live.early.join("; ")}` : null) || (live.market?.read ? `Universe: ${live.market.read.join(", ")}` : "Pulling live Kite data.");
  } else if (openPos.length) {
    const tot = openPos.reduce((s, e) => s + (markOf(e, ticks).pnl || 0), 0);
    stance = <>Holding {openPos.length === 1 ? short(openPos[0].instrument) : `${openPos.length} trades`} <span className={pnlCls(tot)}>{rupee(tot)}</span></>;
    why = openPos[0].thesis;
  } else if (!d.signed_in) {
    stance = <>Offline <em>· Claude isn't signed in</em></>;
    why = "Open the Claude app on this computer and sign in.";
  } else if (!today || !marketOpen) {
    stance = <>{bookedToday ? <>Done for the day <span className={pnlCls(bookedToday)}>{rupee(bookedToday)}</span></> : <>Market closed</>} <em>· next look {d.live.next.day === d.today ? "" : `${d.live.next.day.slice(5)} `}{d.live.next.at}</em></>;
    why = shown?.decision?.thesis || "Claude looks at the market every 15 minutes from 09:30 to 14:30 on trading days.";
  } else {
    stance = <>Flat <em>· watching</em>{bookedToday ? <> <span className={pnlCls(bookedToday)}>{rupee(bookedToday)}</span></> : null}</>;
    why = shown?.decision?.thesis || "Waiting for the first look.";
  }
  const state = !d.signed_in ? "" : busy ? "busy" : marketOpen ? "live" : "";
  const rows = journal(d);

  return (
    <div className="tt-page">
      <div className="ctt">
        <header className="ctt-head">
          <span className={`ctt-mark ${state}`}><i />claude-trader</span>
          <span className="meta">{d.model} · paper · NSE options{d.telegram ? " · telegram on" : ""}</span>
          <span className="sp" />
          <span className="ctt-cap">{busy ? "thinking" : marketOpen ? "live" : "closed"}</span>
          <span className="clock">{t}</span>
        </header>

        <section className="ctt-hero">
          <div>
            <div className="ctt-cap">Claude's stance</div>
            <h2 className="ctt-stance">{stance}</h2>
            {why && <p className="ctt-why">{why}</p>}
          </div>
          <Ring busy={busy} left={countdown ? left : null} total={d.schedule.every * 60}
                value={busy ? "…" : countdown || d.live.next.at} label={busy ? "thinking" : countdown ? "next look" : "next look"} />
        </section>

        <section className="ctt-metrics">
          <div className="ctt-metric"><span className="ctt-cap">Equity</span><b>{inr(b.equity, 0)}</b><small>of {inr(b.capital, 0)}</small></div>
          <div className="ctt-metric"><span className="ctt-cap">Today</span><b className={pnlCls(b.today)}>{rupee(b.today)}</b><small>stops at {inr(-b.day_loss, 0)}</small></div>
          <div className="ctt-metric"><span className="ctt-cap">Booked</span><b className={pnlCls(b.realized)}>{rupee(b.realized)}</b><small>{b.closed} trade{b.closed === 1 ? "" : "s"}{b.closed ? ` · ${b.won} won` : ""}</small></div>
          <div className="ctt-metric"><span className="ctt-cap">Looks today</span><b>{d.schedule.done}<span className="faint"> / {d.schedule.looks}</span></b><small>{d.schedule.early ? `+${d.schedule.early} early · ` : `every ${d.schedule.every} min · `}{openPos.length}/{b.max_open} open{d.schedule.watching ? ` · watching ${d.schedule.watching} F&O` : ""}</small></div>
        </section>

        <div className="ctt-body">
          <section className="ctt-col">
            <div className="ctt-col-h"><span className="ctt-cap">Reasoning{shown ? ` · ${shown.at || String(shown.since || "").slice(0, 5)} look` : ""}</span>{shown?.market?.read && <span className="faint">{shown.market.read.length} names read{shown.market.vix != null ? ` · VIX ${num(shown.market.vix)}` : ""}</span>}</div>
            {shown ? (
              busy ? <Reasoning steps={d.live.steps} roles={live.roles} writing={live.writing} live /> : (
                <Reasoning steps={d.live.steps} roles={shown.decision?.walkthrough || shown.roles} decision={shown.decision} checks={shown.checks} paper={shown.paper} error={shown.error} />
              )
            ) : <p className="ctt-empty">No look yet{today ? `; the first is at ${d.schedule.first}` : " on this date"}.</p>}
          </section>
          <section className="ctt-col">
            <div className="ctt-col-h"><span className="ctt-cap">Positions</span><span className="faint">{inr(b.max_risk, 0)} risk a trade</span></div>
            {held.length ? held.map((e) => <Position key={e.id} e={e} ticks={ticks} />) : <p className="ctt-empty">Flat on this date.</p>}
          </section>
        </div>

        <section className="ctt-journal">
          <div className="ctt-col-h">
            <span className="ctt-cap">Journal</span>
            <select aria-label="Session date" value={d.day} onChange={(e) => setDay(e.target.value)}>
              {[...new Set([d.today, d.day, ...(d.sessions || [])])].map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
          {rows.length ? rows.map((r) => (
            <details key={r.key} className="ctt-jr">
              <summary><span className="t">{r.at}</span><span className={`ctt-jtag ${r.cls}`}>{r.tag}</span><span className="txt">{r.text}</span></summary>
              {r.run && !r.run.error && (
                <div className="body"><Reasoning steps={d.live.steps} roles={r.run.decision?.walkthrough} decision={r.run.decision} checks={r.run.checks} paper={r.run.paper} /></div>
              )}
            </details>
          )) : <p className="ctt-empty">Nothing on this date yet.</p>}
        </section>

        <footer className="ctt-foot">
          <details>
            <summary>Rules the code enforces ↓</summary>
            <ul>{d.rules.map((x) => <li key={x}>{x}</li>)}</ul>
          </details>
          <span>paper only · no order is ever sent</span>
        </footer>
      </div>
      <ErrorNote error={q.error} />
    </div>
  );
}
