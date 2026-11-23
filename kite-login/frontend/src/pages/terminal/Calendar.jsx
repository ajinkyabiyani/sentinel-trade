import { useMemo, useState } from "react";
import { api } from "../../lib/api.js";
import { DataTable, ErrorNote, Spinner, useLoad } from "../../components/ui.jsx";
import { SourceNote } from "./shared.jsx";
import { Logo, WithLogo } from "../../components/Logo.jsx";

// Calendar for the Indian market: RBI policy, F&O expiries, holidays and flows; results dates; dividends and other corporate
// actions by ex-date; IPOs by opening date.
// Layout: what is coming up next (countdowns), then a week board where every event sits on its day in its category's colour,
// a month you can jump around in, and the full agenda for the day or week you pick.
const KINDS = [
  { id: "economic", label: "Market events", short: "Market", source: "NSE and Kite" },
  { id: "earnings", label: "Results", short: "Results", source: "NSE" },
  { id: "dividends", label: "Corporate actions", short: "Actions", source: "NSE" },
  { id: "ipo", label: "IPOs", short: "IPO", source: "NSE" },
];
const DAY_MS = 86_400_000;
const MAX_CHIPS = 5;

const istToday = () => new Date(Date.now() + (330 + new Date().getTimezoneOffset()) * 60_000).toISOString().slice(0, 10);
const addDays = (iso, n) => new Date(Date.parse(`${iso}T00:00:00Z`) + n * DAY_MS).toISOString().slice(0, 10);
const monday = (iso) => addDays(iso, -((new Date(`${iso}T00:00:00Z`).getUTCDay() + 6) % 7));
const fmt = (iso, opts) => new Date(`${iso}T00:00:00Z`).toLocaleDateString("en-IN", { timeZone: "UTC", ...opts });
const short = (iso) => fmt(iso, { day: "numeric", month: "short" });
const days = (a, b) => Math.round((Date.parse(`${b}T00:00:00Z`) - Date.parse(`${a}T00:00:00Z`)) / DAY_MS);
const until = (from, iso) => { const n = days(from, iso); return n === 0 ? "Today" : n === 1 ? "Tomorrow" : n < 0 ? `${-n} d ago` : `in ${n} days`; };
const name = (s) => String(s || "").replace(/\s+(Limited|Ltd\.?)$/i, "");
// The trading week to open on: this one on a weekday, the coming one on a Saturday or Sunday.
const tradingWeek = (iso) => { const dow = new Date(`${iso}T00:00:00Z`).getUTCDay(); return dow === 0 || dow === 6 ? addDays(monday(iso), 7) : monday(iso); };
const KEY = "kd.cal.kinds";
const readKinds = () => { try { const v = JSON.parse(localStorage.getItem(KEY)); return Array.isArray(v) && v.length ? v : KINDS.map((k) => k.id); } catch { return KINDS.map((k) => k.id); } };

// Every record as one event: its kind, a short label for the board and a tone (holidays and expiries stand out).
function events(d) {
  const out = [];
  for (const r of d?.economic || []) {
    const t = r.group === "Holiday" ? "holiday" : r.group === "F&O expiry" ? "expiry" : r.group === "RBI" ? "rbi" : "econ";
    out.push({ kind: "economic", tone: t, date: r.date, label: r.event, sub: r.group, r });
  }
  for (const r of d?.earnings || []) out.push({ kind: "earnings", tone: "results", date: r.date, label: r.symbol || name(r.company), symbol: r.symbol, sub: "Results", r });
  for (const r of d?.dividends || []) out.push({ kind: "dividends", tone: "actions", date: r.date, label: r.symbol || name(r.company), symbol: r.symbol, sub: r.kind, r });
  for (const r of d?.ipo || []) out.push({ kind: "ipo", tone: "ipo", date: r.date, label: name(r.company), symbol: null, sub: r.board || "IPO", r });
  return out;
}

function Chip({ e, openStock }) {
  const go = e.symbol ? () => openStock(e.symbol) : undefined;
  const title = e.kind === "dividends" ? `${e.r.company}: ${e.r.detail}` : e.kind === "earnings" ? `${e.r.company}: ${e.r.detail}` : e.kind === "ipo" ? `${e.r.company} · ${e.r.price || ""} · closes ${e.r.end ? short(e.r.end) : "–"}` : e.label;
  return (
    <button type="button" className={`cal-chip ${e.tone}`} title={title} onClick={go} disabled={!go}>
      {e.symbol && <Logo symbol={e.symbol} size={16} />}
      <span>{e.label}</span>
      {e.kind === "dividends" && e.sub !== "Dividend" && <em>{e.sub}</em>}
      {e.kind === "dividends" && e.sub === "Dividend" && /Rs\.?\s*([\d.]+)/i.test(e.r.detail || "") && <em>₹{e.r.detail.match(/Rs\.?\s*([\d.]+)/i)[1]}</em>}
    </button>
  );
}

function DayColumn({ iso, list, today, on, onPick, openStock, holiday }) {
  const byKind = KINDS.map((k) => [k, list.filter((e) => e.kind === k.id)]).filter(([, l]) => l.length);
  const past = iso < today;
  return (
    <div className={`cal-col${iso === today ? " today" : ""}${on ? " on" : ""}${past ? " past" : ""}${holiday ? " holiday" : ""}`}>
      <button type="button" className="cal-col-head" onClick={onPick} aria-pressed={on}>
        <span className="cal-dow">{fmt(iso, { weekday: "short" })}</span>
        <b>{fmt(iso, { day: "numeric" })}</b>
        <span className="cal-mon">{fmt(iso, { month: "short" })}</span>
        {iso === today && <i className="cal-today">Today</i>}
        <em className="cal-n">{list.length || ""}</em>
      </button>
      {holiday && <div className="cal-holiday">Market holiday<small>{holiday.label.replace(/^Market holiday:?\s*/i, "")}</small></div>}
      <div className="cal-col-body">
        {byKind.map(([k, l]) => (
          <div key={k.id} className="cal-group">
            <span className="cal-group-k">{k.short}<em>{l.length}</em></span>
            {l.slice(0, MAX_CHIPS).map((e, i) => <Chip key={`${e.label}-${i}`} e={e} openStock={openStock} />)}
            {l.length > MAX_CHIPS && <button type="button" className="cal-more" onClick={onPick}>+{l.length - MAX_CHIPS} more</button>}
          </div>
        ))}
        {!list.length && !holiday && <span className="cal-none">Nothing listed</span>}
      </div>
    </div>
  );
}

function Month({ month, setMonth, all, today, weekStart, onPick }) {
  const first = `${month}-01`;
  const startCell = monday(first);
  const cells = Array.from({ length: 42 }, (_, i) => addDays(startCell, i));
  const byDay = useMemo(() => all.reduce((a, e) => { (a[e.date] ||= new Set()).add(e.tone === "holiday" ? "holiday" : e.kind); return a; }, {}), [all]);
  const shift = (n) => { const d = new Date(`${first}T00:00:00Z`); d.setUTCMonth(d.getUTCMonth() + n); setMonth(d.toISOString().slice(0, 7)); };
  return (
    <section className="card cal-month">
      <header>
        <button type="button" className="cal-arrow" aria-label="Previous month" onClick={() => shift(-1)}>‹</button>
        <b>{fmt(first, { month: "long", year: "numeric" })}</b>
        <button type="button" className="cal-arrow" aria-label="Next month" onClick={() => shift(1)}>›</button>
      </header>
      <div className="cal-grid">
        {["M", "T", "W", "T", "F", "S", "S"].map((x, i) => <span key={i} className="cal-grid-h">{x}</span>)}
        {cells.map((iso) => {
          const kinds = byDay[iso];
          const inWeek = iso >= weekStart && iso <= addDays(weekStart, 6);
          return (
            <button key={iso} type="button" onClick={() => onPick(iso)}
              className={`${iso.slice(0, 7) === month ? "" : "out"}${iso === today ? " today" : ""}${inWeek ? " week" : ""}${kinds?.has("holiday") ? " hol" : ""}`}>
              <span className="cal-d">{Number(iso.slice(8))}</span>
              <span className="cal-dots">{kinds && ["economic", "earnings", "dividends", "ipo"].filter((k) => kinds.has(k)).map((k) => <i key={k} className={k} />)}</span>
            </button>
          );
        })}
      </div>
      <div className="cal-legend">
        <span><i className="economic" />Market</span><span><i className="earnings" />Results</span><span><i className="dividends" />Actions</span><span><i className="ipo" />IPO</span>
      </div>
    </section>
  );
}

export default function Calendar({ openStock }) {
  const q = useLoad(api.terminalCalendar, [], 600_000);
  const today = istToday();
  const tw = tradingWeek(today);
  const [weekStart, setWeekStart] = useState(tw);
  const [day, setDay] = useState(null);                    // a picked day, or null for the whole week
  const [month, setMonth] = useState(tw.slice(0, 7));
  const [kinds, setKindsState] = useState(readKinds);
  const [search, setSearch] = useState("");
  const d = q.data;
  const setKinds = (v) => { setKindsState(v); try { localStorage.setItem(KEY, JSON.stringify(v)); } catch { /* private window */ } };
  const toggle = (id) => setKinds(kinds.includes(id) ? (kinds.length > 1 ? kinds.filter((k) => k !== id) : kinds) : [...kinds, id]);

  const all = useMemo(() => events(d), [d]);
  const words = search.trim().toLowerCase();
  const shown = useMemo(() => all.filter((e) => kinds.includes(e.kind) && (!words || JSON.stringify(e.r).toLowerCase().includes(words))), [all, kinds, words]);
  const week = Array.from({ length: 7 }, (_, i) => addDays(weekStart, i));
  const weekEnd = week[6];
  const inWeek = shown.filter((e) => e.date >= weekStart && e.date <= weekEnd);
  const weekendHas = inWeek.some((e) => e.date > week[4]);
  const cols = weekendHas ? week : week.slice(0, 5);
  const holidays = Object.fromEntries(all.filter((e) => e.tone === "holiday").map((e) => [e.date, e]));
  const kindCount = (id) => all.filter((e) => e.kind === id && e.date >= weekStart && e.date <= weekEnd).length;

  // what is next: the next expiry, RBI decision and holiday, IPOs open now, and results and ex-dates this week
  const next = (tone) => all.filter((e) => e.tone === tone && e.date >= today).sort((a, b) => a.date.localeCompare(b.date))[0];
  const openIpos = (d?.ipo || []).filter((r) => r.date <= today && (r.end || r.date) >= today);
  const upcoming = [
    { k: "F&O expiry", e: next("expiry"), tone: "expiry" },
    { k: "RBI policy", e: next("rbi"), tone: "rbi" },
    { k: "Market holiday", e: next("holiday"), tone: "holiday" },
  ];
  const thisWeek = (id) => all.filter((e) => e.kind === id && e.date >= tw && e.date <= addDays(tw, 6)).length;

  const go = (iso, pickDay = true) => { setWeekStart(monday(iso)); setDay(pickDay ? iso : null); setMonth(iso.slice(0, 7)); };
  const agenda = shown.filter((e) => (day ? e.date === day : e.date >= weekStart && e.date <= weekEnd));

  const stock = (r) => (r.symbol ? <button type="button" className="linkish strong" onClick={() => openStock(r.symbol)}><WithLogo symbol={r.symbol} size={20}>{r.symbol}</WithLogo></button> : "–");
  const dateCol = { key: "date", label: "Date", render: (r) => fmt(r.date, { weekday: "short", day: "numeric", month: "short" }) };
  const columns = {
    economic: [dateCol,
      { key: "group", label: "Type", render: (r) => <span className={`cal-tag ${r.group === "Holiday" ? "holiday" : r.group === "F&O expiry" ? "expiry" : r.group === "RBI" ? "rbi" : "econ"}`}>{r.group}</span> },
      { key: "event", label: "Event" },
      { key: "impact", label: "Importance", render: (r) => <span className="cal-imp" aria-label={`Importance ${r.impact} of 3`}>{[1, 2, 3].map((i) => <i key={i} className={i <= r.impact ? "on" : ""} />)}</span> },
      { key: "actual", label: "Actual", align: "num", render: (r) => r.actual || "–" }],
    earnings: [dateCol,
      { key: "symbol", label: "Symbol", render: stock },
      { key: "company", label: "Company", render: (r) => name(r.company) },
      { key: "detail", label: "Agenda", render: (r) => <span className="muted small">{r.detail}</span> }],
    dividends: [dateCol,
      { key: "symbol", label: "Symbol", render: stock },
      { key: "company", label: "Company", render: (r) => name(r.company) },
      { key: "kind", label: "Type", render: (r) => <span className="cal-tag actions">{r.kind}</span> },
      { key: "detail", label: "Detail" },
      { key: "record_date", label: "Record date", render: (r) => (r.record_date ? short(r.record_date) : "–") }],
    ipo: [{ ...dateCol, label: "Opens" },
      { key: "end", label: "Closes", render: (r) => (r.end ? short(r.end) : "–") },
      { key: "company", label: "Company", render: (r) => <b>{name(r.company)}</b> },
      { key: "board", label: "Board", render: (r) => <span className="cal-tag ipo">{r.board || "Main"}</span> },
      { key: "price", label: "Price band", render: (r) => r.price || "–" },
      { key: "listing", label: "Listing", render: (r) => (r.listing ? short(r.listing) : "–") }],
  };

  if (!d) return <section className="card orb-loading">{q.error ? <ErrorNote error={q.error} /> : <Spinner />}</section>;
  return (
    <div className="tt-page cal">
      <section className="cal-next">
        {upcoming.map((u) => (
          <button key={u.k} type="button" className={`card cal-next-card ${u.tone}`} disabled={!u.e} onClick={() => u.e && go(u.e.date)}>
            <span className="cal-next-k">{u.k}</span>
            <b>{u.e ? until(today, u.e.date) : "None listed"}</b>
            <small>{u.e ? `${fmt(u.e.date, { weekday: "short", day: "numeric", month: "short" })} · ${u.e.label}` : "–"}</small>
          </button>
        ))}
        <button type="button" className="card cal-next-card results" onClick={() => go(tw, false)}>
          <span className="cal-next-k">{tw === monday(today) ? "This week" : "Coming week"}</span>
          <b>{thisWeek("earnings")} results · {thisWeek("dividends")} ex-dates</b>
          <small>{openIpos.length ? `${openIpos.length} IPO${openIpos.length > 1 ? "s" : ""} open now: ${openIpos.slice(0, 2).map((r) => name(r.company)).join(", ")}` : "No IPO open right now"}</small>
        </button>
      </section>

      <div className="cal-toolbar card">
        <div className="cal-weeknav">
          <button type="button" className="cal-arrow" aria-label="Previous week" onClick={() => go(addDays(weekStart, -7), false)}>‹</button>
          <b>{short(weekStart)} – {short(cols[cols.length - 1])} {weekEnd.slice(0, 4)}</b>
          <button type="button" className="cal-arrow" aria-label="Next week" onClick={() => go(addDays(weekStart, 7), false)}>›</button>
          <button type="button" className={`ghost small${weekStart === tw && !day ? " on" : ""}`} onClick={() => go(tw, false)}>{tw === monday(today) ? "This week" : "Coming week"}</button>
          <button type="button" className={`ghost small${day === today ? " on" : ""}`} onClick={() => go(today)}>Today</button>
        </div>
        <div className="cal-kinds" role="group" aria-label="Show">
          {KINDS.map((k) => (
            <button key={k.id} type="button" className={`cal-kind ${k.id}${kinds.includes(k.id) ? " on" : ""}`} aria-pressed={kinds.includes(k.id)} onClick={() => toggle(k.id)}>
              <i />{k.label}<em>{kindCount(k.id)}</em>
            </button>
          ))}
        </div>
        <input className="cal-search" type="search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Find a stock or event" aria-label="Search the calendar" />
      </div>

      <div className="cal-main">
        <section className={`card cal-board cols${cols.length}`}>
          {cols.map((iso) => (
            <DayColumn key={iso} iso={iso} today={today} on={day === iso} holiday={holidays[iso]} openStock={openStock}
              list={inWeek.filter((e) => e.date === iso && e.tone !== "holiday")} onPick={() => setDay(day === iso ? null : iso)} />
          ))}
        </section>
        <aside className="cal-rail">
          <Month month={month} setMonth={setMonth} all={shown} today={today} weekStart={weekStart} onPick={(iso) => go(iso)} />
          <section className="card cal-src">
            <h4>Sources</h4>
            {KINDS.map((k) => <div key={k.id}><span>{k.label}</span><SourceNote status={d.status[k.id]} source={k.source} /></div>)}
            <p className="muted small">CPI, GDP, IIP and PMI dates with estimates aren't shown: no free source the app can read publishes them.</p>
          </section>
        </aside>
      </div>

      <section className="card tt-card cal-agenda">
        <header className="card-head">
          <h3>{day ? fmt(day, { weekday: "long", day: "numeric", month: "long" }) : `Week of ${short(weekStart)}`}</h3>
          <span className="muted small">{agenda.length} event{agenda.length === 1 ? "" : "s"}{day && <> · <button type="button" className="link-btn small" onClick={() => setDay(null)}>show the whole week</button></>}</span>
        </header>
        {KINDS.filter((k) => kinds.includes(k.id)).map((k) => {
          const rows = agenda.filter((e) => e.kind === k.id).map((e) => e.r);
          if (!rows.length) return null;
          return (
            <div key={k.id} className="cal-agenda-sec">
              <h4 className={`cal-sec-h ${k.id}`}><i />{k.label}<em>{rows.length}</em></h4>
              <DataTable rowKey={(r) => JSON.stringify(r)} rows={rows} columns={columns[k.id]} initialSort={{ key: "date", dir: "asc" }} />
            </div>
          );
        })}
        {!agenda.length && <p className="cal-empty">Nothing listed for {day ? "this day" : "this week"}{words ? ` matching "${search}"` : ""}.</p>}
      </section>
    </div>
  );
}
