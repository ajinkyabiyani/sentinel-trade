import { useState } from "react";
import { api } from "../lib/api.js";
import { num, signed, tone } from "../lib/format.js";
import { ErrorNote, Spinner, useLoad } from "../components/ui.jsx";
import { BandColumns, Dumbbells, FinBars, GainStrip, GmpScatter, OddsRing } from "../components/IpoCharts.jsx";

// IPO Agent: each upcoming, open or just-closed IPO with a signal (Apply, Listing gain only, Neutral, Avoid), the odds from past
// issues like it, and the checks behind it: fundamentals from the offer document, demand (GMP, subscription) and market timing.
// Read only: nothing here applies for an IPO.
const SIG = { Apply: "apply", "Listing gain only": "listing", Neutral: "neutral", Avoid: "avoid", Waiting: "waiting", Listed: "neutral" };
const day = (iso) => (iso ? new Date(`${iso}T00:00:00Z`).toLocaleDateString("en-IN", { timeZone: "UTC", day: "numeric", month: "short" }) : "–");
const today = () => new Date(Date.now() + (330 + new Date().getTimezoneOffset()) * 60_000).toISOString().slice(0, 10);
const cr = (v) => (v == null ? "–" : `₹${num(v, v >= 100 ? 0 : 2)} cr`);
const rupees = (v) => (v == null ? "–" : `₹${num(v, 0)}`);

function Bar({ label, value }) {
  const cls = value == null ? "" : value >= 65 ? "good" : value >= 45 ? "warn" : "bad";
  return (
    <div className={`ipo-bar ${cls}`}>
      <span>{label}</span>
      <span className="ipo-bar-track"><span style={{ width: `${value ?? 0}%` }} /></span>
      <b>{value == null ? "–" : value}</b>
    </div>
  );
}

function Timeline({ a }) {
  const t = today();
  const steps = [["Opens", a.open], ["Closes", a.close], ["Allotment", a.allotment], ["Lists", a.listing]];
  return (
    <ol className="ipo-time">
      {steps.map(([k, d]) => <li key={k} className={d && d <= t ? "done" : ""}><i /><span>{k}</span><b>{day(d)}</b></li>)}
    </ol>
  );
}

function Checks({ title, list, score }) {
  return (
    <section className="ipo-checks">
      <h4>{title}<em>{score == null ? "no data" : `${score}/100`}</em></h4>
      {list.length ? (
        <ul>{list.map((c) => <li key={c.name} className={c.verdict} title={c.why}><i aria-label={c.verdict} /><span>{c.name}</span><b>{c.value}</b></li>)}</ul>
      ) : <p className="muted small">Not available yet.</p>}
    </section>
  );
}


function IpoCard({ a }) {
  const [open, setOpen] = useState(false);
  const o = a.odds;
  const fresh = Number(String(a.issue?.["Fresh Issue"] || "").replace(/[^\d.]/g, "")) || 0;
  const ofs = Number(String(a.issue?.["Offer for Sale"] || "").replace(/[^\d.]/g, "")) || 0;
  const range = a.gmp_high > 0 ? Math.max(0, Math.min(100, ((a.gmp ?? 0) / a.gmp_high) * 100)) : null;
  return (
    <article className={`card ipo-card ${SIG[a.signal]}`}>
      <header className="ipo-head">
        <div className="ipo-id">
          <span className="ipo-mono" aria-hidden="true">{a.name.split(/\s+/).slice(0, 2).map((w) => w[0]).join("")}</span>
          <div>
            <h3>{a.name}</h3>
            <span className="ipo-meta"><em className={a.board === "SME" ? "sme" : "main"}>{a.board}</em><em className={`st ${a.status}`}>{a.status_label}</em>{a.price ? `₹${num(a.price, 0)} a share · ${num(a.lot, 0)} shares a lot · ${rupees(a.lot_cost)} a lot` : "Price band not out yet"}</span>
          </div>
        </div>
        <div className="ipo-signal">
          <b>{a.signal}</b>
          <span>{a.reason}</span>
        </div>
      </header>

      <div className="ipo-grid">
        <div className="ipo-odds">
          <span className="ipo-k">Listed above price</span>
          {o ? <>
            <div className="ipo-odds-viz">
              <OddsRing pct={o.win_pct} />
              <span className="ipo-sub">of {o.n} {o.basis.replace(/^(Mainboard|SME) issues/, "past $1 issues")}</span>
            </div>
            <GainStrip p10={o.p10_gain} med={o.median_gain} p90={o.p90_gain} />
          </> : <><OddsRing pct={null} /><span className="ipo-sub">Odds come once there is a price band and a GMP.</span></>}
        </div>
        <div className="ipo-gmp">
          <span className="ipo-k">Grey market premium</span>
          {a.gmp != null && a.price ? <>
            <b className="ipo-big">{signed(a.gmp_pct, 1)}%</b>
            <span className="ipo-sub">₹{num(a.gmp)} over ₹{num(a.price, 0)} · may list near ₹{num(a.est_listing)}</span>
            {range != null && <span className="ipo-range" title={`Low ₹${a.gmp_low}, high ₹${a.gmp_high}`}><span className="ipo-range-track"><span style={{ width: `${range}%` }} /></span><small>₹{a.gmp_low} low · ₹{a.gmp_high} high</small></span>}
          </> : <><b className="ipo-big muted">–</b><span className="ipo-sub">{a.gmp != null ? `₹${num(a.gmp)} quoted, no price band to compare` : "No quote yet"}</span></>}
          {a.sub ? <span className="ipo-subs">Subscribed <b>{num(a.sub)}x</b></span> : null}
        </div>
        <div className="ipo-scores">
          <Bar label="Fundamentals" value={a.fundamentals_score} />
          <Bar label="Demand" value={a.demand_score} />
          <Bar label="Timing" value={a.timing_score} />
        </div>
        <div className="ipo-when">
          <Timeline a={a} />
          <span className="ipo-sub">Issue {cr(a.size_cr)}{a.market_cap ? ` · valued at ${cr(a.market_cap)}` : ""}</span>
        </div>
      </div>

      <button type="button" className="ipo-more" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? "Hide the full analysis" : "Full analysis: every check, financials, peers"}</button>
      {open && (
        <div className="ipo-body">
          <div className="ipo-check-grid">
            <Checks title="Fundamentals" list={a.fundamentals} score={a.fundamentals_score} />
            <Checks title="Demand" list={a.demand} score={a.demand_score} />
            <Checks title="Timing" list={a.timing} score={a.timing_score} />
          </div>
          <div className="ipo-detail-grid">
            <section className="ipo-fin"><h4>Financials</h4><FinBars fin={a.financials} />{a.financials?.note && <p className="muted small">{a.financials.note}</p>}</section>
            <section className="ipo-issue">
              {(fresh || ofs) > 0 && (
                <>
                  <h4>Where the money goes</h4>
                  <div className="ipo-split"><span className="fresh" style={{ flex: fresh || 0.0001 }} /><span className="ofs" style={{ flex: ofs || 0.0001 }} /></div>
                  <div className="ipo-split-legend"><span><i className="fresh" />New shares (to the company) {a.issue["Fresh Issue"] || "–"}</span><span><i className="ofs" />Offer for sale (to sellers) {a.issue["Offer for Sale"] || "–"}</span></div>
                </>
              )}
              {a.peers?.length > 1 && (
                <>
                  <h4>Listed peers</h4>
                  <table>
                    <thead><tr><th>Company</th><th>P/E</th><th>RoNW</th></tr></thead>
                    <tbody>{a.peers.map((p, i) => <tr key={p.name} className={i === 0 ? "self" : ""}><td>{p.name}</td><td>{p.pe == null ? "–" : num(p.pe, 1)}</td><td>{p.ronw == null ? "–" : `${num(p.ronw, 1)}%`}</td></tr>)}</tbody>
                  </table>
                </>
              )}
              {a.link && <p className="small"><a href={a.link} target="_blank" rel="noreferrer">Issue details and GMP history</a>{a.gmp_updated ? <span className="muted"> · GMP updated {a.gmp_updated}</span> : null}</p>}
            </section>
          </div>
        </div>
      )}
    </article>
  );
}


export default function IpoAgent() {
  const q = useLoad(api.ipoAgent, [], 60_000);
  const [err, setErr] = useState("");
  const [board, setBoard] = useState("Mainboard");
  const d = q.data;
  if (!d || (q.loading && !d.built)) {
    if (d && !d.built) return <section className="card ipo-empty">{d.running ? <><Spinner /> Reading upcoming IPOs, their offer documents and five years of listings…</> : <>The agent starts a minute after the app does. <button type="button" className="primary small" onClick={() => api.ipoAgentRun().then(() => setTimeout(() => q.reload(true), 4000))}>Run now</button></>}<ErrorNote error={d.error} /></section>;
    return <section className="card orb-loading"><Spinner /></section>;
  }
  const active = d.active || [];
  const count = (s) => active.filter((a) => a.signal === s).length;
  const ck = d.check;
  const b = d.backdrop || {};
  const run = async () => { setErr(""); try { await api.ipoAgentRun(); setTimeout(() => q.reload(true), 5000); } catch (e) { setErr(e.message); } };
  const tr = d.track;

  return (
    <div className="ipo">
      <section className="ipo-summary">
        <div className="card ipo-sum-card">
          <span className="ipo-k">In the pipeline</span>
          <b>{active.length} IPO{active.length === 1 ? "" : "s"}</b>
          <span className="ipo-chips">{["Apply", "Listing gain only", "Neutral", "Avoid", "Waiting"].filter(count).map((s) => <em key={s} className={SIG[s]}>{count(s)} {s}</em>)}</span>
        </div>
        <div className="card ipo-sum-card">
          <span className="ipo-k">Does GMP predict the listing?</span>
          {ck ? <>
            <b>{num(ck.high_odds_hit, 0)}% <small>listed above price</small></b>
            <span className="muted small">when the odds were 75% or more: {ck.high_odds_n} issues in the last 12 months, judged with odds from earlier years only</span>
          </> : <span className="muted small">Not enough history yet.</span>}
        </div>
        <div className="card ipo-sum-card">
          <span className="ipo-k">Market it lists into</span>
          <b className="ipo-back">
            <span>Nifty <em className={tone(b.nifty_vs_50d)}>{b.nifty_vs_50d == null ? "–" : `${signed(b.nifty_vs_50d, 1)}%`}</em><small>vs 50-day</small></span>
            <span>VIX <em>{b.vix == null ? "–" : num(b.vix, 1)}</em></span>
          </b>
          <span className="muted small">Last 20 listings above price: Mainboard {num(d.recent?.Mainboard?.win_pct, 0)}%, SME {num(d.recent?.SME?.win_pct, 0)}%</span>
        </div>
        <div className="card ipo-sum-card">
          <span className="ipo-k">Updated</span>
          <b>{d.built ? new Date(d.built).toLocaleTimeString("en-IN", { timeStyle: "short" }) : "–"}</b>
          <button type="button" className="ghost small" disabled={d.running} onClick={run}>{d.running ? "Reading…" : "Refresh now"}</button>
        </div>
      </section>

      {active.length ? active.map((a) => <IpoCard key={a.id} a={a} />) : <p className="card ipo-empty">No IPO is upcoming, open or awaiting listing right now.</p>}

      <div className="ipo-two">
        <section className="card tt-card">
          <header className="card-head">
            <h3>How past IPOs listed</h3>
            <div className="ipo-tabs">{["Mainboard", "SME"].map((x) => <button key={x} type="button" className={board === x ? "on" : ""} onClick={() => setBoard(x)}>{x}</button>)}</div>
          </header>
          <p className="muted small">{num(d.history_n, 0)} IPOs since {day(d.history_from)}. Column height: share that listed above price; under it, the median listing gain.</p>
          {d.table && <div className="ipo-odds-pair"><BandColumns rows={d.table.gmp[board]} title="By GMP the day before listing" /><BandColumns rows={d.table.sub[board]} title="By subscription" /></div>}
          <GmpScatter points={d.scatter} board={board} />
          {ck && <p className="muted small">Checked on the last 12 months ({ck.test} issues) with bands from the years before ({ck.train} issues): Brier score {ck.brier_band} against {ck.brier_base} for the board's plain base rate (lower is better). Issues given under 50% odds listed above price {num(ck.low_odds_hit, 0)}% of the time ({ck.low_odds_n} issues).</p>}
        </section>
        <section className="card tt-card">
          <header className="card-head"><h3>Recently listed</h3><span className="muted small">GMP the day before against the actual listing</span></header>
          <div className="ipo-listed"><Dumbbells rows={(d.listed || []).slice(0, 14)} /></div>
        </section>
      </div>

      <section className="card tt-card">
        <header className="card-head"><h3>The agent's own record</h3><span className="muted small">the signal standing when bidding closed, scored at listing</span></header>
        {tr?.scored ? (
          <div className="ipo-track">
            {Object.entries(tr.by_signal).filter(([, v]) => v.n).map(([s, v]) => <div key={s} className={SIG[s]}><span>{s}</span><b>{num(v.win_pct, 0)}%</b><small>{v.n} issues · average {signed(v.avg_gain, 1)}%</small></div>)}
          </div>
        ) : <p className="muted small">{tr?.waiting ? `${tr.waiting} signal${tr.waiting > 1 ? "s" : ""} logged, waiting for listing. Until some list, this agent's own record is untested.` : "Nothing logged yet."}</p>}
      </section>

      <p className="ipo-fine">{d.disclaimer} Sources: {d.source}.</p>
      <ErrorNote error={err || d.error} />
    </div>
  );
}
