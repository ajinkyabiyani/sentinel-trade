import { useMemo, useState } from "react";
import { num, signed, tone } from "../../lib/format.js";
import { DataTable, ErrorNote, Spinner } from "../../components/ui.jsx";
import { LoadNote, useStocks } from "./shared.jsx";

// Scanner over the Nifty 500: one-click setups and free filters, on live prices. A row opens the stock or an order
// ticket; nothing is ever sent without you reviewing it on the Trade page.
const PRESETS = [
  { id: "all", label: "All stocks", about: "Every Nifty 500 stock.", test: () => true, sort: "value_cr" },
  { id: "gainers", label: "Top gainers", about: "Up 2% or more today.", test: (r) => r.chg >= 2, sort: "chg" },
  { id: "losers", label: "Top losers", about: "Down 2% or more today.", test: (r) => r.chg <= -2, sort: "chg", dir: "asc" },
  { id: "gapup", label: "Gap up", about: "Opened at least 1% above the previous close.", test: (r) => r.gap >= 1, sort: "gap" },
  { id: "gapdown", label: "Gap down", about: "Opened at least 1% below the previous close.", test: (r) => r.gap <= -1, sort: "gap", dir: "asc" },
  { id: "volume", label: "Volume surge", about: "Relative volume of 2 or more.", test: (r) => r.rvol >= 2, sort: "rvol" },
  { id: "high", label: "Near 52-week high", about: "Within 2% of the 52-week high.", test: (r) => r.from_hi52 != null && r.from_hi52 >= -2, sort: "from_hi52" },
  { id: "low", label: "Near 52-week low", about: "Within 2% of the 52-week low.", test: (r) => r.from_lo52 != null && r.from_lo52 <= 2, sort: "from_lo52", dir: "asc" },
  { id: "trend", label: "Strong trend", about: "Above the 20, 50 and 200-day averages.", test: (r) => r.vs_sma20 > 0 && r.vs_sma50 > 0 && r.vs_sma200 > 0, sort: "chg" },
  { id: "overbought", label: "RSI above 70", about: "14-day RSI above 70.", test: (r) => r.rsi > 70, sort: "rsi" },
  { id: "oversold", label: "RSI below 30", about: "14-day RSI below 30.", test: (r) => r.rsi != null && r.rsi < 30, sort: "rsi", dir: "asc" },
  { id: "volatile", label: "High volatility", about: "ATR of 4% of the price or more.", test: (r) => r.atr_pct >= 4, sort: "atr_pct" },
];
// [key in the row, label, step]: minimum and maximum boxes for each
const RANGES = [["last", "Price (₹)", 1], ["chg", "Change %", 0.1], ["gap", "Gap %", 0.1], ["rvol", "Relative volume", 0.1], ["value_cr", "Value today (₹ Cr)", 1],
  ["mcap_cr", "Market cap (₹ Cr)", 100], ["atr_pct", "ATR %", 0.1], ["rsi", "RSI", 1], ["from_hi52", "From 52-week high %", 1], ["beta", "Beta", 0.1]];
const MA_FILTERS = [["vs_sma20", "20-day"], ["vs_sma50", "50-day"], ["vs_sma200", "200-day"]];

const pct = (v, d = 2) => (v == null ? "–" : <span className={tone(v)}>{signed(v, d)}%</span>);
const plain = (v, d = 2) => (v == null ? "–" : num(v, d));

export default function Scanner({ openStock, openTicket }) {
  const { rows, live, meta, loading, error } = useStocks();
  const [preset, setPreset] = useState("gainers");
  const [limits, setLimits] = useState({});
  const [mas, setMas] = useState({});
  const [sector, setSector] = useState("");
  const [search, setSearch] = useState("");
  const [open, setOpen] = useState(false);
  const cur = PRESETS.find((p) => p.id === preset);
  const sectors = useMemo(() => [...new Set(rows.map((r) => r.sector).filter(Boolean))].sort(), [rows]);

  const shown = useMemo(() => {
    const words = search.trim().toLowerCase();
    return rows.filter((r) => {
      if (!cur.test(r)) return false;
      if (sector && r.sector !== sector) return false;
      if (words && !r.symbol.toLowerCase().includes(words) && !(r.name || "").toLowerCase().includes(words)) return false;
      for (const [k, on] of Object.entries(mas)) {
        if (on === "above" && !(r[k] > 0)) return false;
        if (on === "below" && !(r[k] < 0)) return false;
      }
      for (const [k] of RANGES) {
        const lo = limits[`${k}.min`], hi = limits[`${k}.max`];
        if (lo !== undefined && lo !== "" && !(r[k] >= Number(lo))) return false;
        if (hi !== undefined && hi !== "" && !(r[k] <= Number(hi))) return false;
      }
      return true;
    });
  }, [rows, cur, sector, search, mas, limits]);

  const active = Object.values(limits).filter((v) => v !== "").length + Object.values(mas).filter(Boolean).length + (sector ? 1 : 0);
  const clear = () => { setLimits({}); setMas({}); setSector(""); setSearch(""); };
  const set = (key, v) => setLimits((l) => ({ ...l, [key]: v }));

  const columns = [
    { key: "symbol", label: "Stock", render: (r) => <button type="button" className="linkish strong" onClick={() => openStock(r.symbol)} title={r.name}>{r.symbol}</button> },
    { key: "sector", label: "Sector", render: (r) => <span className="muted small">{r.sector}</span> },
    { key: "last", label: "LTP", align: "num", render: (r) => <b key={r.last} className={r.chg >= 0 ? "flash-up" : "flash-down"}>{num(r.last)}</b> },
    { key: "chg", label: "Change", align: "num", render: (r) => pct(r.chg) },
    { key: "gap", label: "Gap", align: "num", render: (r) => pct(r.gap) },
    { key: "rvol", label: "RVOL", align: "num", render: (r) => plain(r.rvol) },
    { key: "value_cr", label: "Value (₹ Cr)", align: "num", render: (r) => plain(r.value_cr, 1) },
    { key: "mcap_cr", label: "Mkt cap (₹ Cr)", align: "num", render: (r) => plain(r.mcap_cr, 0) },
    { key: "atr_pct", label: "ATR %", align: "num", render: (r) => plain(r.atr_pct) },
    { key: "rsi", label: "RSI", align: "num", render: (r) => plain(r.rsi, 1) },
    { key: "from_hi52", label: "From high", align: "num", render: (r) => pct(r.from_hi52, 1) },
    { key: "ret1m", label: "1M", align: "num", sort: (r) => r.ret?.["1M"], render: (r) => pct(r.ret?.["1M"], 1) },
    { key: "x", label: "", nosort: true, render: (r) => <button type="button" className="ghost small" onClick={() => openTicket({ exchange: "NSE", tradingsymbol: r.symbol, transaction_type: "BUY", product: "CNC", order_type: "LIMIT", price: r.last })}>Trade</button> },
  ];

  if (loading) return <section className="card orb-loading">{error ? <ErrorNote error={error} /> : <Spinner />}</section>;
  return (
    <div className="tt-page">
      <div className="scan-presets" role="group" aria-label="Scans">
        {PRESETS.map((p) => (
          <button type="button" key={p.id} className={`scan-chip${preset === p.id ? " on" : ""}`} aria-pressed={preset === p.id} onClick={() => setPreset(p.id)} title={p.about}>{p.label}</button>
        ))}
      </div>
      <section className="card tt-card">
        <header className="card-head">
          <div>
            <div className="row">
              <h3>{cur.label}</h3>
              <span className="count-chip">{shown.length}</span>
              {live && <span className="live-chip"><span className="live-dot" /> Live</span>}
            </div>
            <span className="muted small">{cur.about}</span>
          </div>
          <div className="row">
            <input className="tt-search" type="search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Symbol or company" aria-label="Search stocks" />
            <button type="button" className={`ghost small${open || active ? " on" : ""}`} aria-expanded={open} onClick={() => setOpen((v) => !v)}>Filters{active ? ` (${active})` : ""}</button>
            {active > 0 && <button type="button" className="ghost small" onClick={clear}>Clear</button>}
          </div>
        </header>
        {open && (
          <div className="scan-filters">
            <label>Sector
              <select value={sector} onChange={(e) => setSector(e.target.value)}>
                <option value="">All sectors</option>
                {sectors.map((s) => <option key={s}>{s}</option>)}
              </select>
            </label>
            {MA_FILTERS.map(([k, label]) => (
              <label key={k}>{label} average
                <select value={mas[k] || ""} onChange={(e) => setMas((m) => ({ ...m, [k]: e.target.value }))}>
                  <option value="">Any</option>
                  <option value="above">Price above</option>
                  <option value="below">Price below</option>
                </select>
              </label>
            ))}
            {RANGES.map(([k, label, step]) => (
              <fieldset key={k}>
                <legend>{label}</legend>
                <input type="number" step={step} placeholder="Min" aria-label={`${label} minimum`} value={limits[`${k}.min`] ?? ""} onChange={(e) => set(`${k}.min`, e.target.value)} />
                <input type="number" step={step} placeholder="Max" aria-label={`${label} maximum`} value={limits[`${k}.max`] ?? ""} onChange={(e) => set(`${k}.max`, e.target.value)} />
              </fieldset>
            ))}
          </div>
        )}
        <DataTable key={preset} rowKey={(r) => r.symbol} rows={shown} columns={columns} initialSort={{ key: cur.sort, dir: cur.dir || "desc" }} limit={100} empty="No stock matches this scan right now." />
        <div className="tt-load"><LoadNote meta={meta} /></div>
        <ErrorNote error={error} />
      </section>
    </div>
  );
}
