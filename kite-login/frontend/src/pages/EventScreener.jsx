import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api.js";
import { num, signed, tone } from "../lib/format.js";
import { Badge, DataTable, ErrorNote, Spinner, useLoad } from "../components/ui.jsx";
import { Logo } from "../components/Logo.jsx";

// Event Screener: from world events to the less obvious Indian companies they may touch, with the evidence and what is missing.
// Research only: there is deliberately no buy, sell or size control on this page.
// Layout: a flow strip (events -> signals -> ideas -> your review) that doubles as navigation, then one section at a time.
// Ideas is a list beside a detail pane, so every idea's key signals read at a glance and the full case opens on the right.
const CLASS_TONE = { Direct: "buy", Indirect: "accent", Speculative: "" };
const PRICED = { Early: 1, "Partially priced": 2, "Largely priced": 3, Unclear: 0 };
const PRICED_SHORT = { Early: "Early", "Partially priced": "Partly priced", "Largely priced": "Largely priced", Unclear: "Unclear" };
const CONF = { High: 3, Medium: 2, Low: 1 };
const TABS = [
  { id: "ideas", label: "Company ideas", hint: "Every company a live theme points to, with why it was picked, how far it has run and what would break it." },
  { id: "themes", label: "World themes", hint: "What the world is doing right now: each theme two named outlets report, with the headlines for and against." },
  { id: "signals", label: "Signals", hint: "The filings and headlines read and how each scored, orders clustering in one industry, and events no theme covers yet." },
  { id: "library", label: "Theme library", hint: "The chains that link an event to an industry: Claude's new drafts for you to approve, and the links you have reviewed." },
  { id: "record", label: "Track record", hint: "Whether the confidence labels mean anything yet, input costs for context, and how the screen picks companies." },
];
const TYPES = ["Dark horses", "All", "Direct", "Indirect", "Speculative"];
const KEY = "kd.events.tab";
const readTab = () => { try { return localStorage.getItem(KEY) || "ideas"; } catch { return "ideas"; } };

// When the app first saw a headline, for those it saw arrive; otherwise the day the feed gave.
const stamp = (h) => (h.seen_at ? new Date(h.seen_at).toLocaleString("en-IN", { dateStyle: "short", timeStyle: "short" }) : h.date);
const three = (t) => t.moves?.periods?.find((p) => p.label === "3 months") || t.moves?.periods?.[0];
const short = (n) => String(n || "").replace(/\s+(Limited|Ltd\.?)$/i, "");
const link = (u, text) => (/^https?:/.test(u || "") ? <a href={u} target="_blank" rel="noreferrer">{text}</a> : text);

function Meter({ value, of = 3, kind }) {
  return <span className={`es-meter ${kind}`} aria-hidden="true">{Array.from({ length: of }, (_, i) => <i key={i} className={i < value ? "on" : ""} />)}</span>;
}

function Priced({ state }) {
  const v = PRICED[state] ?? 0;
  return <span className={`es-priced p${v}`} title={`Priced in: ${state}`}><Meter value={v} kind="priced" /><span>{PRICED_SHORT[state] || state}</span></span>;
}

function Conf({ level }) {
  return <span className={`es-conf c${CONF[level] || 0}`} title={`${level} confidence: how well supported the analysis is, not how likely the stock is to rise`}><Meter value={CONF[level] || 0} kind="conf" />{level}</span>;
}

// ---------------------------------------------------------------- ideas list and detail

function IdeaRow({ t, on, onPick, review }) {
  const m = three(t);
  const warn = t.plain?.warnings?.length || 0;
  const state = t.review === "needed" ? (review?.decision || "review") : null;
  return (
    <li>
      <button type="button" className={`es-row${on ? " on" : ""}`} onClick={onPick} aria-pressed={on}>
        <Logo symbol={t.company.symbol} size={30} />
        <span className="es-row-main">
          <span className="es-row-name"><b title={t.company.name}>{short(t.company.name)}</b>{t.dark_horse && <i className="es-dh" title="Dark horse: passed the less-followed screen">DH</i>}</span>
          <span className="es-row-sub">{t.company.symbol} · {t.theme}</span>
        </span>
        <span className="es-row-sig">
          <span className={`es-cls ${CLASS_TONE[t.classification] || "spec"}`}>{t.classification}</span>
          <Priced state={t.priced_in.state} />
        </span>
        <span className="es-row-num">
          <b className={m ? tone(m.abnormal_pct) : "muted"}>{m ? `${signed(m.abnormal_pct)}%` : "–"}</b>
          <small title={m ? `ahead of or behind Nifty 50 over ${m.label}` : undefined}>{m ? `vs Nifty · ${m.label.replace(/ months?$/, "M")}` : "no prices"}</small>
        </span>
        <span className="es-row-end">
          <Conf level={t.confidence} />
          <span className="es-row-flags">
            {warn > 0 && <em className="warn" title={`${warn} warning sign${warn > 1 ? "s" : ""}`}>⚠ {warn}</em>}
            {state && <em className={state === "approved" ? "ok" : state === "rejected" ? "no" : "todo"}>{state === "review" ? "To review" : state}</em>}
          </span>
        </span>
      </button>
    </li>
  );
}

function Moves({ periods }) {
  const max = Math.max(1, ...periods.flatMap((p) => [Math.abs(p.stock_pct ?? 0), Math.abs(p.benchmark_pct ?? 0)]));
  const bar = (v, cls) => (
    <span className="es-bar"><span className={`es-bar-fill ${cls} ${v < 0 ? "neg" : "pos"}`} style={{ width: `${(Math.abs(v ?? 0) / max) * 50}%` }} /></span>
  );
  return (
    <div className="es-moves">
      <div className="es-moves-legend"><span><i className="stock" /> Stock</span><span><i className="nifty" /> Nifty 50</span><span>Ahead or behind</span></div>
      {periods.map((p) => (
        <div key={p.label} className="es-moves-row">
          <span className="es-moves-label">{p.label}</span>
          <span className="es-moves-bars">{bar(p.stock_pct, "stock")}{bar(p.benchmark_pct, "nifty")}</span>
          <span className="es-moves-vals"><span className={tone(p.stock_pct)}>{signed(p.stock_pct)}%</span><span className="muted">{signed(p.benchmark_pct)}%</span></span>
          <b className={`es-moves-ab ${tone(p.abnormal_pct)}`}>{signed(p.abnormal_pct)}%</b>
        </div>
      ))}
    </div>
  );
}

const DETAIL_TABS = [{ id: "case", label: "The case" }, { id: "evidence", label: "Evidence" }, { id: "price", label: "Price and value" }, { id: "risks", label: "Risks" }];

function Detail({ t, review, onReview, openStock, disclaimer }) {
  const [sec, setSec] = useState("case");
  useEffect(() => setSec("case"), [t.id]);
  const pi = t.priced_in;
  const b = pi.basis;
  const pl = t.plain;
  const f = t.facts || {};
  const m = three(t);
  const needs = t.review === "needed";
  const kpis = [
    { k: "Market value", v: f.mcap_cr ?? t.dark_horse?.mcap_cr, fmt: (x) => `₹${num(x, 0)} cr` },
    { k: "Sales growth", v: f.sales_growth_pct ?? t.dark_horse?.growth_pct, fmt: (x) => `${signed(x)}%`, tone: true },
    { k: "ROCE", v: f.roce ?? t.dark_horse?.roce, fmt: (x) => `${num(x, 0)}%` },
    { k: m ? `${m.label} vs Nifty` : "vs Nifty", v: m?.abnormal_pct, fmt: (x) => `${signed(x)}%`, tone: true },
    { k: "P/E now · median", v: b.pe_now, fmt: (x) => `${num(x, 1)} · ${b.pe_median != null ? num(b.pe_median, 1) : "–"}` },
    { k: "Order value", v: t.exposure?.order_value_crore, fmt: (x) => `₹${num(x, 0)} cr` },
  ].filter((x) => x.v != null);
  return (
    <article className="card es-detail">
      <header className="es-d-head">
        <Logo symbol={t.company.symbol} size={44} />
        <div className="es-d-title">
          <h3>{t.company.name}</h3>
          <span className="muted small">{t.company.symbol} · {t.sub_sector}{t.company.in_nifty500 === false ? " · outside the Nifty 500" : ""}</span>
        </div>
        {openStock && <button type="button" className="ghost small" onClick={() => openStock(t.company.symbol)}>Open chart</button>}
      </header>
      <div className="es-d-tags">
        {t.dark_horse && <Badge tone="accent">Dark horse</Badge>}
        {t.contested && <Badge tone="amber">Theme contested</Badge>}
        <Badge tone={CLASS_TONE[t.classification]}>{t.classification}</Badge>
        <Priced state={pi.state} />
        <Conf level={t.confidence} />
      </div>
      <div className="es-event">
        <span className="es-event-k">The event · {t.event.date}{t.event.publisher ? ` · ${t.event.publisher}` : ""}</span>
        <p>{t.event.description}</p>
        <span className="es-event-theme">Theme: <b>{t.theme}</b>{t.event.source && <> · {link(t.event.source, "source document")}</>}</span>
      </div>
      {kpis.length > 0 && <div className="es-kpis">{kpis.map((x) => <div key={x.k}><span>{x.k}</span><b className={x.tone ? tone(x.v) : ""}>{x.fmt(x.v)}</b></div>)}</div>}

      <nav className="es-d-tabs" aria-label="Idea details">
        {DETAIL_TABS.map((s) => <button key={s.id} type="button" className={sec === s.id ? "on" : ""} aria-pressed={sec === s.id} onClick={() => setSec(s.id)}>{s.label}{s.id === "risks" && pl?.warnings?.length ? <em>{pl.warnings.length}</em> : null}</button>)}
      </nav>

      <div className="es-d-body">
        {sec === "case" && (
          <>
            {f.about && <section><h4>What the company does</h4><p>{f.about}</p></section>}
            <section><h4>Why this theme could matter</h4><p>{pl?.theme || t.theme_plain}</p>{pl?.evidence && <p className="muted">{pl.evidence}</p>}</section>
            {pl?.why_selected?.length > 0 && <section><h4>Why this company was picked</h4><ul className="es-ticks">{pl.why_selected.map((r) => <li key={r}>{r}</li>)}</ul></section>}
            {!pl && t.dark_horse && <section><h4>Why it is less obvious</h4><p>{t.dark_horse.reason}.</p></section>}
            {pl?.check?.length > 0 && <section className="es-check"><h4>Before you act on this</h4><ol>{pl.check.map((r) => <li key={r}>{r}</li>)}</ol></section>}
          </>
        )}
        {sec === "evidence" && (
          <>
            <section>
              <h4>Causal chain</h4>
              <ol className="es-chain">
                {t.causal_chain.map((l) => (
                  <li key={l.link} className={l.score >= 2 ? "ok" : "weak"}>
                    <span>{l.link}</span>
                    <span className="es-chain-src">{l.score >= 2 ? `source ${l.score}/3` : "unverified"}{l.source ? <> · {link(l.source, "open")}</> : null}</span>
                  </li>
                ))}
              </ol>
            </section>
            <section><h4>Exposure</h4><p>{t.exposure.text} Capacity and pricing position: {t.exposure.capacity}.</p></section>
            <section><h4>Why {t.confidence} confidence</h4><ul className="es-ticks">{t.confidence_reasons.map((r) => <li key={r}>{r}</li>)}</ul><p className="muted">{t.confidence_note}</p></section>
          </>
        )}
        {sec === "price" && (
          <>
            {pl?.ran && <section><h4>How much it has already moved</h4><p>{pl.ran}</p></section>}
            {t.moves?.periods?.length > 0 && <Moves periods={t.moves.periods} />}
            <section>
              <h4>Priced in: {pi.state}</h4>
              <p>
                {b.abnormal ? <>{b.window ? b.window[0].toUpperCase() + b.window.slice(1) : "Since the event"} the stock moved {signed(b.abnormal.stock_pct)}% and Nifty 50 {signed(b.abnormal.benchmark_pct)}%, so <b className={tone(b.abnormal.abnormal_pct)}>{signed(b.abnormal.abnormal_pct)}%</b> against the benchmark (to {b.abnormal.as_of}). </> : "No price response available. "}
                {b.pe_now != null ? `P/E ${num(b.pe_now, 1)}` : "No current P/E"}{b.pe_median != null ? ` against a median of ${num(b.pe_median, 1)} and a high of ${num(b.pe_high, 1)} over ${b.pe_points} quarters.` : "."}
              </p>
              <p className="muted">Press coverage: {b.coverage}. Thresholds: {b.thresholds}.{pi.missing.length ? ` Missing: ${pi.missing.join(", ")}.` : ""}</p>
            </section>
            {pl?.room && <section><h4>How much room, on its own past valuations</h4><p>{pl.room}</p></section>}
          </>
        )}
        {sec === "risks" && (
          <>
            {pl?.warnings?.length > 0 && <section className="es-warn"><h4>Warning signs</h4><ul>{pl.warnings.map((r) => <li key={r}>{r}</li>)}</ul></section>}
            <section><h4>Key risks</h4><ul className="es-dash">{t.risks.map((r) => <li key={r}>{r}</li>)}</ul></section>
            <section><h4>What would break this thesis</h4><ul className="es-dash">{t.invalidators.map((r) => <li key={r}>{r}</li>)}</ul></section>
            <section><h4>Horizon</h4><p>{t.horizon}</p></section>
          </>
        )}
      </div>

      <footer className="es-d-foot">
        <span className="es-dates">{Object.entries(t.staleness).map(([k, v]) => <span key={k} className={v.stale ? "stale" : ""}>{k} {v.as_of ? String(v.as_of).slice(0, 10) : "missing"}</span>)}</span>
        {needs && (
          <span className="es-review">
            {review?.decision ? <Badge tone={review.decision === "approved" ? "buy" : "sell"}>{review.decision === "approved" ? "Reviewed" : "Rejected"}</Badge> : <span className="muted small">Awaiting your review</span>}
            {review?.decision !== "approved" && <button type="button" className="primary small" onClick={() => onReview(t.id, "approved")}>Mark reviewed</button>}
            {review?.decision !== "rejected" && <button type="button" className="ghost small" onClick={() => onReview(t.id, "rejected")}>Reject</button>}
          </span>
        )}
      </footer>
      <p className="es-fine">{t.disclaimer || disclaimer}</p>
    </article>
  );
}

function Ideas({ theses, reviews, onReview, openStock, theme, setTheme, todo, setTodo, disclaimer, removed }) {
  const [type, setType] = useState("Dark horses");
  const [priced, setPriced] = useState("any");
  const [q, setQ] = useState("");
  const [pick, setPick] = useState(null);
  const themes = useMemo(() => Object.entries(theses.reduce((a, t) => ({ ...a, [t.theme]: (a[t.theme] || 0) + 1 }), {})).sort((a, b) => b[1] - a[1]), [theses]);
  const words = q.trim().toLowerCase();
  const typeOk = (t) => todo || type === "All" || (type === "Dark horses" ? t.dark_horse : t.classification === type);
  const shown = theses.filter((t) => typeOk(t) && (theme === "all" || t.theme === theme) && (priced === "any" || t.priced_in.state === priced)
    && (!todo || (t.review === "needed" && !reviews?.[t.id]))
    && (!words || `${t.company.name} ${t.company.symbol} ${t.theme} ${t.sub_sector}`.toLowerCase().includes(words)));
  const sel = shown.find((t) => t.id === pick) || shown[0];
  const count = (fn) => theses.filter(fn).length;
  return (
    <div className="es-ideas">
      <div className="es-filters card">
        <div className="es-types" role="radiogroup" aria-label="Kind of idea">
          {TYPES.map((c) => {
            const n = c === "All" ? theses.length : c === "Dark horses" ? count((t) => t.dark_horse) : count((t) => t.classification === c);
            return <button key={c} type="button" role="radio" aria-checked={type === c} className={type === c ? "on" : ""} onClick={() => setType(c)}>{c}<em>{n}</em></button>;
          })}
        </div>
        <select value={theme} onChange={(e) => setTheme(e.target.value)} aria-label="Theme">
          <option value="all">All themes ({themes.length})</option>
          {themes.map(([n, c]) => <option key={n} value={n}>{n} ({c})</option>)}
        </select>
        <select value={priced} onChange={(e) => setPriced(e.target.value)} aria-label="Priced in">
          <option value="any">Any priced-in state</option>
          {Object.keys(PRICED).map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <label className="es-todo"><input type="checkbox" checked={todo} onChange={(e) => setTodo(e.target.checked)} /> Awaiting my review</label>
        <input className="es-search" type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Company, symbol or sector" aria-label="Search ideas" />
      </div>

      <div className="es-split">
        <section className="card es-listcard">
          <header className="es-list-head">
            <span><b>{shown.length}</b> of {theses.length} ideas{theme !== "all" && <> · {theme} <button type="button" className="link-btn small" onClick={() => setTheme("all")}>clear</button></>}</span>
            <span className="muted small">ranked by evidence, then confidence</span>
          </header>
          {shown.length ? <ul className="es-rows">{shown.map((t) => <IdeaRow key={t.id} t={t} on={sel?.id === t.id} onPick={() => setPick(t.id)} review={reviews?.[t.id]} />)}</ul>
            : <p className="es-empty">No idea matches these filters.{type === "Dark horses" ? " Try All." : ""}</p>}
          <p className="es-fine">
            Dark horse is a mechanical screen: not among the largest in its sub-sector, market value under ₹30,000 crore, sales growing, ROCE of at least 10%, and no more than 15% ahead of Nifty over 90 days. These cut-offs are untested.
            {removed ? ` This pass set aside ${removed.leaders} sector leaders, ${removed.too_large} too large, ${removed.weak_or_no_growth} with flat or falling sales, ${removed.low_return} with low returns and ${removed.already_moved} that had already moved.` : ""}
          </p>
        </section>
        {sel ? <Detail t={sel} review={reviews?.[sel.id]} onReview={onReview} openStock={openStock} disclaimer={disclaimer} /> : <section className="card es-detail es-empty">Pick an idea to see its case.</section>}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- world themes

function Themes({ themes, theses, checked, newHeadlines, onCompanies }) {
  const [open, setOpen] = useState(null);
  const linked = (name) => theses.filter((t) => t.theme === name).length;
  const top = Math.max(1, ...themes.map((t) => t.count + t.against_count));
  if (!themes.length) return <p className="card es-empty">No theme is reported by two outlets right now.</p>;
  return (
    <>
      <p className="es-note">{checked ? `Checked ${new Date(checked).toLocaleTimeString("en-IN", { timeStyle: "short" })} · ${newHeadlines ?? 0} new headlines · every 3 minutes.` : "Checked every 3 minutes."} A theme goes live once two named outlets report it.</p>
      <div className="es-themes">
        {themes.map((t) => {
          const n = linked(t.name);
          const isOpen = open === t.id;
          return (
            <article key={t.id} className={`card es-theme${isOpen ? " open" : ""}`}>
              <header>
                <h4>{t.name}</h4>
                {t.contested && <Badge tone="amber">Contested</Badge>}
              </header>
              <div className="es-balance" title={`${t.count} headlines for, ${t.against_count} pointing the other way`}>
                <span className="for" style={{ width: `${(t.count / top) * 100}%` }} />
                <span className="against" style={{ width: `${(t.against_count / top) * 100}%` }} />
              </div>
              <div className="es-theme-nums">
                <span><b className="up">{t.count}</b> for</span>
                <span><b className="down">{t.against_count}</b> against</span>
                <span><b>{t.outlets.length}</b> outlets</span>
                <span className="muted">latest {t.latest}</span>
              </div>
              <div className="es-theme-act">
                <button type="button" className="ghost small" onClick={() => setOpen(isOpen ? null : t.id)} aria-expanded={isOpen}>{isOpen ? "Hide headlines" : "Headlines"}</button>
                <button type="button" className="ghost small es-go" disabled={!n} onClick={() => onCompanies(t.name)}>{n ? `${n} compan${n === 1 ? "y" : "ies"} →` : "No company yet"}</button>
              </div>
              {isOpen && (
                <div className="es-heads">
                  <ul>{t.headlines.map((h) => <li key={h.link}>{link(h.link, h.title)} <span className="muted">{h.source}, {stamp(h)}</span></li>)}</ul>
                  {t.against?.length > 0 && <><b className="small">Pointing the other way</b><ul className="against">{t.against.map((h) => <li key={h.link}>{link(h.link, h.title)} <span className="muted">{h.source}, {h.date}</span></li>)}</ul></>}
                </div>
              )}
            </article>
          );
        })}
      </div>
    </>
  );
}

// ---------------------------------------------------------------- signals, library, track record

function Signals({ latest }) {
  return (
    <>
      <div className="tt-grid2">
        <section className="card tt-card">
          <header className="card-head"><h3>Orders clustering in one industry</h3><span className="muted small">3 or more companies, last 14 days</span></header>
          {(latest.clusters || []).map((c) => <div className="fd-row" key={c.industry}><span>{c.industry} {!c.in_library && <Badge tone="amber">no theme yet</Badge>}</span><b className="small">{c.companies.join(", ")}</b></div>)}
          {!(latest.clusters || []).length && <p className="muted small">None this period.</p>}
        </section>
        <section className="card tt-card">
          <header className="card-head"><h3>Events with no theme yet</h3><span className="muted small">you decide whether each deserves a chain</span></header>
          <ul className="plain small ev-scroll">{(latest.unmapped || []).map((n) => <li key={n.link}>{link(n.link, n.title)} <span className="muted">{n.source}</span></li>)}</ul>
          {!(latest.unmapped || []).length && <p className="muted small">None this week.</p>}
        </section>
      </div>
      <section className="card tt-card">
        <header className="card-head"><h3>Signals read</h3><span className="muted small">primary source + size + breadth + novelty, each 0 to 3</span></header>
        <DataTable rowKey={(s) => (s.source || "") + s.text} rows={latest.signals || []} empty="None yet." columns={[
          { key: "date", label: "Date", render: (s) => String(s.date || "").slice(0, 16) },
          { key: "kind", label: "Type" },
          { key: "text", label: "Signal", render: (s) => <span className="small">{s.text}</span> },
          { key: "total", label: "Score", align: "num", sort: (s) => s.score.total, render: (s) => (
            <span className="es-score" title={`primary ${s.score.primary}, size ${s.score.magnitude}, breadth ${s.score.breadth}, novelty ${s.score.novelty}`}>
              <span className="es-score-bar"><span style={{ width: `${(s.score.total / 12) * 100}%` }} /></span>{s.score.total}/12
            </span>) },
        ]} />
      </section>
    </>
  );
}

function Draft({ c, onDecide }) {
  return (
    <div className="ev-lib">
      <b>{c.name}</b> {c.status !== "draft" && <Badge tone={c.status === "approved" ? "buy" : ""}>{c.status}</Badge>}
      <p className="small">{c.why}</p>
      <p className="muted small">Could fall short: {c.counter} Lead time: {c.lead_months} months.</p>
      <ul className="plain small">{c.nodes.map((n) => <li key={n.id}>{n.name} <span className="muted">{n.peers} companies, such as {n.sample.join(", ")}</span></li>)}</ul>
      <ul className="plain small">{c.evidence.map((h) => <li key={h.link}>{link(h.link, h.title)} <span className="muted">{h.source}, {h.date}</span></li>)}</ul>
      <div className="row ev-review">
        {c.status !== "approved" && <button type="button" className="ghost small" onClick={() => onDecide(c.id, "approve")}>Approve theme</button>}
        {c.status !== "rejected" && <button type="button" className="ghost small" onClick={() => onDecide(c.id, "reject")}>Reject</button>}
        {c.status === "rejected" && <button type="button" className="ghost small" onClick={() => onDecide(c.id, "reopen")}>Back to draft</button>}
      </div>
    </div>
  );
}

function Drafts({ state, drafts, onRun, onDecide }) {
  const open = drafts.filter((c) => c.status === "draft");
  const done = drafts.filter((c) => c.status !== "draft");
  const last = state?.last;
  if (!state) return null;
  return (
    <section className="card tt-card">
      <header className="card-head">
        <h3>New themes from Claude</h3>
        <div className="row">
          <span className="muted small">{state.calls_today} of {state.max_calls} calls today</span>
          <button type="button" className="primary small" disabled={state.running || !state.key_set || state.calls_today >= state.max_calls} onClick={onRun}>{state.running ? "Claude is reading…" : "Ask Claude"}</button>
        </div>
      </header>
      <p className="muted small">Claude reads recent headlines that no theme covers and drafts possible chains; no headline is sent twice. Nothing changes until you approve a draft, and an approved theme's links stay at Low confidence until you mark them reviewed.</p>
      {!state.key_set && <p className="small"><b>Needs an API key.</b> Save your Anthropic API key on its own in <code>C:\Algo\Anthropic_key.txt</code>, then refresh this page. Git ignores that file, and the key is never sent to this page.</p>}
      {state.error && <p className="small"><b>{state.error}</b></p>}
      {last && <p className="muted small">Last read {new Date(last.at).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" })}: {last.headlines} headlines, {last.kept} drafts kept, {last.refused.length} refused by the checks.{last.refused.length ? ` Refused: ${last.refused.join("; ")}.` : ""}</p>}
      {open.map((c) => <Draft key={c.id} c={c} onDecide={onDecide} />)}
      {last && !open.length && <p className="muted small">No drafts waiting.</p>}
      {done.length > 0 && <details className="ev-lib"><summary className="small">Decided ({done.length})</summary>{done.map((c) => <Draft key={c.id} c={c} onDecide={onDecide} />)}</details>}
    </section>
  );
}

function Library({ chains, edges, onEdge }) {
  return (
    <section className="card tt-card">
      <header className="card-head"><h3>Theme library</h3><span className="muted small">{chains.length} chains · a link stays at Low confidence until marked reviewed</span></header>
      <div className="es-lib">
        {chains.map((c) => {
          const done = c.nodes.filter((n) => edges.includes(`${c.id}/${n.id}`)).length;
          return (
            <details key={c.id} className="es-lib-item">
              <summary>
                <b>{c.name}</b>
                <span className="es-lib-prog" title={`${done} of ${c.nodes.length} links reviewed`}><span style={{ width: `${(done / Math.max(1, c.nodes.length)) * 100}%` }} /></span>
                <span className="muted small">{done}/{c.nodes.length} reviewed</span>
              </summary>
              <p className="muted small">{c.counter}</p>
              <ul className="plain">
                {c.nodes.map((n) => {
                  const id = `${c.id}/${n.id}`;
                  const on = edges.includes(id);
                  return <li key={id} className="ev-edge"><span>{n.name} <span className="muted small">edge confidence {n.confidence}</span></span>
                    {on
                      ? <span className="row ev-review"><Badge tone="buy">Reviewed</Badge><button type="button" className="link-btn small muted" onClick={() => onEdge(id, false)}>Undo</button></span>
                      : <button type="button" className="ghost small" onClick={() => onEdge(id, true)}>Mark reviewed</button>}</li>;
                })}
              </ul>
            </details>
          );
        })}
      </div>
    </section>
  );
}

function Record({ tr, cm, feeds }) {
  return (
    <div className="tt-grid2">
      <section className="card tt-card">
        <header className="card-head"><h3>Is confidence meaningful?</h3><span className="muted small">every idea against Nifty 50, from 30 days on</span></header>
        {tr && Object.keys(tr.by_confidence).length ? (
          <DataTable rowKey={(r) => r.k} rows={Object.entries(tr.by_confidence).map(([k, v]) => ({ k, ...v }))} columns={[
            { key: "k", label: "Group" }, { key: "n", label: "Ideas", align: "num" },
            { key: "avg_abnormal_pct", label: "Average vs Nifty", align: "num", render: (r) => <span className={tone(r.avg_abnormal_pct)}>{signed(r.avg_abnormal_pct)}%</span> },
            { key: "beat_benchmark_pct", label: "Beat Nifty", align: "num", render: (r) => `${num(r.beat_benchmark_pct, 0)}%` },
          ]} />
        ) : (
          <div className="es-pending">
            <b>{tr?.logged ?? 0}</b><span>ideas logged</span>
            <b>{tr?.too_new ?? 0}</b><span>too new to score</span>
            <p className="muted small">Each idea is scored against Nifty 50 once it is 30 days old. Until then the confidence labels are untested.</p>
          </div>
        )}
      </section>
      <section className="card tt-card">
        <header className="card-head"><h3>Input costs</h3><span className="muted small">Kite near-month futures · context only</span></header>
        {(cm.data || []).map((c) => <div className="fd-row" key={c.label}><span>{c.label} <span className="muted small">{c.contract}</span></span><b>{num(c.last)} <span className={tone(c.change_pct)}>{c.change_pct == null ? "" : `${signed(c.change_pct)}%`}</span></b></div>)}
        {!cm.data?.length && <p className="muted small">Available once logged in to Kite.</p>}
        <p className="muted small">Not read in this version: government releases, power-grid data, consensus estimates, earnings transcripts, press coverage, company capacity.</p>
        {feeds.some(([, f]) => f.error) && <p className="muted small">Feeds failing, last saved copy used: {feeds.filter(([, f]) => f.error).map(([n]) => n).join(", ")}.</p>}
      </section>
    </div>
  );
}

// ---------------------------------------------------------------- page

export default function EventScreener({ openStock }) {
  const q = useLoad(api.events, [], 10_000);
  const cm = useLoad(api.eventCommodities, [], 60_000);
  const [err, setErr] = useState("");
  const [tab, setTabState] = useState(readTab);
  const [theme, setTheme] = useState("all");
  const [todo, setTodo] = useState(false);
  const setTab = (t) => { setTabState(t); try { localStorage.setItem(KEY, t); } catch { /* private window */ } };
  const d = q.data;
  const latest = d?.latest || {};
  const theses = latest.theses || [];
  const themes = latest.themes || [];
  const reviews = d?.reviews || {};
  const act = async (fn) => { setErr(""); try { await fn(); q.reload(true); } catch (e) { setErr(e.message); } };
  const running = d?.progress?.running;
  const toReview = theses.filter((t) => t.review === "needed" && !reviews[t.id]).length;
  const headlines = themes.reduce((a, t) => a + t.count, 0);
  const cls = (c) => theses.filter((t) => t.classification === c).length;
  const draftsOpen = (d?.drafts || []).filter((c) => c.status === "draft").length;

  if (q.loading && !d) return <section className="card orb-loading"><Spinner /></section>;
  const steps = [
    { id: "themes", n: themes.length, k: "World themes", s: `${headlines} headlines · ${latest.headlines_kept ?? 0} read` },
    { id: "signals", n: (latest.signals || []).length, k: "Signals scored", s: `${num(latest.filings_read ?? 0, 0)} NSE filings read` },
    { id: "ideas", n: theses.length, k: "Company ideas", s: `${cls("Direct")} direct · ${cls("Indirect")} indirect · ${cls("Speculative")} speculative` },
    { id: "review", n: toReview, k: "Awaiting your review", s: draftsOpen ? `${draftsOpen} theme drafts too` : "ideas resting on new links" },
  ];
  const active = TABS.find((t) => t.id === tab) || TABS[0];
  const counts = { ideas: theses.length, themes: themes.length, signals: (latest.signals || []).length, library: d?.chains?.length ?? 0 };

  return (
    <div className="tt-page es">
      <section className="card es-hero">
        <div className="es-hero-top">
          <p className="es-hero-how"><b>How to read it:</b> left to right, from what the world is doing, to the filings and headlines that back it, to the companies it touches and what still needs your eye. Click any step to go there.</p>
          <div className="es-hero-run">
            <span className="muted small">{running ? d.progress.stage : latest.built ? `Updated ${new Date(latest.built).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" })}` : "Not run yet"}</span>
            <button type="button" className="primary small" disabled={running} onClick={() => act(api.eventsRun)}>{running ? "Running…" : "Run now"}</button>
          </div>
        </div>
        <ol className="es-flow">
          {steps.map((s) => {
            const goes = s.id === "review" ? "ideas" : s.id;
            const on = tab === goes && (s.id === "review") === todo;
            return (
              <li key={s.id}>
                <button type="button" className={`es-step ${s.id}${on ? " on" : ""}`} onClick={() => { setTab(goes); setTodo(s.id === "review"); if (s.id === "review") setTheme("all"); }}>
                  <b>{s.n}</b><span>{s.k}</span><small>{s.s}</small>
                </button>
              </li>
            );
          })}
        </ol>
      </section>

      <nav className="es-tabs" aria-label="Event Screener sections">
        {TABS.map((t) => <button key={t.id} type="button" className={tab === t.id ? "on" : ""} aria-current={tab === t.id ? "page" : undefined} onClick={() => setTab(t.id)}>{t.label}{counts[t.id] != null && <em>{counts[t.id]}</em>}</button>)}
      </nav>
      <p className="es-note">{active.hint}</p>

      {tab === "ideas" && (theses.length ? <Ideas theses={theses} reviews={reviews} openStock={openStock} theme={theme} setTheme={setTheme} todo={todo} setTodo={setTodo} disclaimer={d?.disclaimer} removed={latest.dark_screen?.removed}
        onReview={(id, decision) => act(() => api.eventsReview({ id, decision }))} /> : <p className="card es-empty">{latest.built ? "No idea yet." : "Not run yet. Choose Run now, or wait for the next pass."}</p>)}
      {tab === "themes" && <Themes themes={themes} theses={theses} checked={latest.checked} newHeadlines={latest.new_headlines} onCompanies={(name) => { setTheme(name); setTab("ideas"); }} />}
      {tab === "signals" && <Signals latest={latest} />}
      {tab === "library" && d && (
        <>
          <Drafts state={d.drafter} drafts={d.drafts || []} onRun={() => act(api.eventDraftsRun)} onDecide={(id, decision) => act(() => api.eventDraftDecide({ id, decision }))} />
          <Library chains={d.chains} edges={d.edges} onEdge={(edge, approved) => act(() => api.eventsEdge({ edge, approved }))} />
        </>
      )}
      {tab === "record" && <Record tr={latest.tracking} cm={cm} feeds={Object.entries(latest.feeds || {})} />}

      <p className="es-fine">{d?.disclaimer}</p>
      <ErrorNote error={err || d?.progress?.error || q.error} />
    </div>
  );
}
