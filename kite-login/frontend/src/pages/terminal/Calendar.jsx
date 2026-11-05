import { useMemo, useState } from "react";
import { api } from "../../lib/api.js";
import { DataTable, Empty, ErrorNote, Segmented, Spinner, useLoad } from "../../components/ui.jsx";
import { SourceNote } from "./shared.jsx";

// Calendar for the Indian market: RBI policy, F&O expiries, holidays and flows; results dates; dividends and other
// corporate actions by ex-date; IPOs by opening date. One week at a time, Monday to Friday.
const KINDS = [{ id: "economic", label: "Economic" }, { id: "earnings", label: "Earnings" }, { id: "dividends", label: "Dividends" }, { id: "ipo", label: "IPO" }];
const SOURCES = { economic: "NSE and Kite", earnings: "NSE", dividends: "NSE", ipo: "NSE" };
const DAY_MS = 86_400_000;

const istToday = () => new Date(Date.now() + (330 + new Date().getTimezoneOffset()) * 60_000).toISOString().slice(0, 10);
const addDays = (iso, n) => new Date(Date.parse(`${iso}T00:00:00Z`) + n * DAY_MS).toISOString().slice(0, 10);
const monday = (iso) => addDays(iso, -((new Date(`${iso}T00:00:00Z`).getUTCDay() + 6) % 7));
const fmt = (iso, opts) => new Date(`${iso}T00:00:00Z`).toLocaleDateString("en-IN", { timeZone: "UTC", ...opts });
const dayLabel = (iso) => fmt(iso, { weekday: "long", day: "numeric", month: "long" });
const short = (iso) => fmt(iso, { day: "numeric", month: "short" });

const Stars = ({ n }) => <span className="cal-stars" aria-label={`Importance ${n} of 3`}>{"★".repeat(n)}<i>{"★".repeat(3 - n)}</i></span>;

export default function Calendar({ openStock }) {
  const q = useLoad(api.terminalCalendar, [], 600_000);
  const [kind, setKind] = useState("earnings");
  const today = istToday();
  const [day, setDay] = useState(today);
  const [whole, setWhole] = useState(true);
  const [search, setSearch] = useState("");
  const start = monday(day);
  const week = useMemo(() => Array.from({ length: 5 }, (_, i) => addDays(start, i)), [start]);
  const d = q.data;

  const counts = (iso) => Object.fromEntries(KINDS.map((k) => [k.id, (d?.[k.id] || []).filter((r) => r.date === iso).length]));
  const rows = useMemo(() => {
    const words = search.trim().toLowerCase();
    return (d?.[kind] || []).filter((r) => (whole ? r.date >= week[0] && r.date <= addDays(week[4], 2) : r.date === day))
      .filter((r) => !words || JSON.stringify(r).toLowerCase().includes(words));
  }, [d, kind, whole, week, day, search]);

  const pick = (iso, all = false) => { setDay(iso); setWhole(all); };
  const stock = (r) => (r.symbol ? <button type="button" className="linkish strong" onClick={() => openStock(r.symbol)}>{r.symbol}</button> : "–");
  const dateCol = { key: "date", label: "Date", render: (r) => short(r.date) };
  const columns = {
    economic: [dateCol,
      { key: "group", label: "Type" },
      { key: "impact", label: "Importance", render: (r) => <Stars n={r.impact} /> },
      { key: "event", label: "Event" },
      { key: "actual", label: "Actual", align: "num", render: (r) => r.actual || "–" }],
    earnings: [dateCol,
      { key: "symbol", label: "Symbol", render: stock },
      { key: "company", label: "Company" },
      { key: "detail", label: "Agenda", render: (r) => <span className="muted small">{r.detail}</span> }],
    dividends: [dateCol,
      { key: "symbol", label: "Symbol", render: stock },
      { key: "company", label: "Company" },
      { key: "kind", label: "Type" },
      { key: "detail", label: "Detail" },
      { key: "record_date", label: "Record date", render: (r) => (r.record_date ? short(r.record_date) : "–") }],
    ipo: [{ ...dateCol, label: "Opens" },
      { key: "end", label: "Closes", render: (r) => (r.end ? short(r.end) : "–") },
      { key: "symbol", label: "Symbol" },
      { key: "company", label: "Company" },
      { key: "board", label: "Board", render: (r) => r.board || "–" },
      { key: "price", label: "Price band", render: (r) => r.price || "–" },
      { key: "listing", label: "Listing", render: (r) => (r.listing ? short(r.listing) : "–") }],
  };

  if (!d) return <section className="card orb-loading">{q.error ? <ErrorNote error={q.error} /> : <Spinner />}</section>;
  return (
    <div className="tt-page">
      <div className="cal-bar">
        <b>{short(week[0])} to {short(week[4])}, {week[4].slice(0, 4)}</b>
        <div className="cal-nav">
          <button type="button" className="ghost small" onClick={() => pick(addDays(today, -1))}>Yesterday</button>
          <button type="button" className={`ghost small${day === today && !whole ? " on" : ""}`} onClick={() => pick(today)}>Today</button>
          <button type="button" className="ghost small" onClick={() => pick(addDays(today, 1))}>Tomorrow</button>
          <button type="button" className={`ghost small${whole && start === monday(today) ? " on" : ""}`} onClick={() => pick(today, true)}>This week</button>
          <button type="button" className={`ghost small${whole && start === addDays(monday(today), 7) ? " on" : ""}`} onClick={() => pick(addDays(monday(today), 7), true)}>Next week</button>
          <button type="button" className="ghost small" aria-label="Previous week" onClick={() => pick(addDays(start, -7), true)}>‹</button>
          <button type="button" className="ghost small" aria-label="Next week" onClick={() => pick(addDays(start, 7), true)}>›</button>
        </div>
        <Segmented value={kind} onChange={setKind} options={KINDS} label="Calendar" />
      </div>
      <div className="cal-week">
        {week.map((iso) => {
          const c = counts(iso);
          return (
            <button type="button" key={iso} className={`cal-day${iso === day && !whole ? " on" : ""}${iso === today ? " today" : ""}`} onClick={() => pick(iso)}>
              <b>{fmt(iso, { weekday: "short" })} <small className="muted">{short(iso)}</small></b>
              {KINDS.map((k) => <span key={k.id} className={k.id === kind ? "cur" : ""}>{k.label}<em>{c[k.id]}</em></span>)}
            </button>
          );
        })}
      </div>
      <section className="card tt-card">
        <header className="card-head">
          <h3>{whole ? `Week of ${short(week[0])}` : dayLabel(day)}</h3>
          <SourceNote status={d.status[kind]} source={SOURCES[kind]} />
          <input className="tt-search" type="search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search this list" aria-label="Search this list" />
        </header>
        {kind === "economic" && <p className="muted small tt-note">RBI policy dates, F&O expiries, market holidays and FII/DII flows. CPI, GDP, IIP and PMI dates with estimates aren't shown: no free source I can read automatically publishes them.</p>}
        {rows.length ? <DataTable rowKey={(r) => JSON.stringify(r)} rows={rows} columns={columns[kind]} initialSort={{ key: "date", dir: "asc" }} />
          : <Empty>{`Nothing listed for ${whole ? "this week" : "this day"}.`}</Empty>}
      </section>
    </div>
  );
}
