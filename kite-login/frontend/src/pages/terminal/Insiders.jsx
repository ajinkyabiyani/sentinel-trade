import { useMemo, useState } from "react";
import { api } from "../../lib/api.js";
import { num } from "../../lib/format.js";
import { Badge, DataTable, Empty, ErrorNote, Segmented, Spinner, useLoad } from "../../components/ui.jsx";
import { SourceNote } from "./shared.jsx";
import { WithLogo } from "../../components/Logo.jsx";

// Who is buying and selling in size: bulk, block and short deals reported to NSE, and insider-trading disclosures
// (promoters, directors, key staff) whenever NSE's feed returns them.
const ABOUT = {
  bulk: "A single client trading more than 0.5% of a company's equity in one day.",
  block: "Large trades made in the exchange's separate block-deal window.",
  short: "Shares sold short by institutions, as reported to the exchange.",
  pit: "Trades reported by promoters, directors and key staff under the insider-trading rules.",
};
const value = (r) => (Number(r.qty) && Number(r.watp) ? (Number(r.qty) * Number(r.watp)) / 1e7 : null);

export default function Insiders({ openStock }) {
  const q = useLoad(api.terminalInsiders, [], 300_000);
  const [tab, setTab] = useState("bulk");
  const [search, setSearch] = useState("");
  const [side, setSide] = useState("all");
  const d = q.data;

  const rows = useMemo(() => {
    const words = search.trim().toLowerCase();
    return (d?.[tab] || []).filter((r) => (side === "all" || (r.buySell || "").toUpperCase() === side))
      .filter((r) => !words || JSON.stringify(r).toLowerCase().includes(words));
  }, [d, tab, search, side]);

  if (!d) return <section className="card orb-loading">{q.error ? <ErrorNote error={q.error} /> : <Spinner />}</section>;
  const totals = (list) => list.reduce((a, r) => {
    const v = value(r) || 0;
    if ((r.buySell || "").toUpperCase() === "BUY") a.buy += v; else if ((r.buySell || "").toUpperCase() === "SELL") a.sell += v;
    return a;
  }, { buy: 0, sell: 0 });
  const t = totals(d[tab] || []);
  const stock = (r) => <button type="button" className="linkish strong" onClick={() => openStock(r.symbol)}><WithLogo symbol={r.symbol} size={20}>{r.symbol}</WithLogo></button>;
  const dealColumns = [
    { key: "date", label: "Date" },
    { key: "symbol", label: "Symbol", render: stock },
    { key: "name", label: "Company" },
    { key: "clientName", label: "Client", render: (r) => r.clientName || "–" },
    { key: "buySell", label: "Side", render: (r) => (r.buySell ? <Badge tone={r.buySell === "BUY" ? "buy" : "sell"}>{r.buySell}</Badge> : "–") },
    { key: "qty", label: "Quantity", align: "num", sort: (r) => Number(r.qty), render: (r) => num(Number(r.qty), 0) },
    { key: "watp", label: "Avg price", align: "num", sort: (r) => Number(r.watp), render: (r) => (r.watp ? `₹${num(Number(r.watp))}` : "–") },
    { key: "value", label: "Value (₹ Cr)", align: "num", sort: value, render: (r) => (value(r) == null ? "–" : num(value(r), 2)) },
  ];
  const pitColumns = [
    { key: "date", label: "Filed", render: (r) => r.date || r.intimDt || "–" },
    { key: "symbol", label: "Symbol", render: stock },
    { key: "acqName", label: "Person", render: (r) => r.acqName || "–" },
    { key: "personCategory", label: "Category", render: (r) => r.personCategory || "–" },
    { key: "tdpTransactionType", label: "Type", render: (r) => r.tdpTransactionType || "–" },
    { key: "secAcq", label: "Quantity", align: "num", sort: (r) => Number(r.secAcq), render: (r) => (r.secAcq ? num(Number(r.secAcq), 0) : "–") },
    { key: "secVal", label: "Value (₹)", align: "num", sort: (r) => Number(r.secVal), render: (r) => (r.secVal ? num(Number(r.secVal), 0) : "–") },
  ];

  return (
    <div className="tt-page">
      <div className="cal-bar">
        <Segmented value={tab} onChange={(v) => { setTab(v); setSide("all"); }} label="Deal type"
          options={[{ id: "bulk", label: `Bulk deals (${d.bulk.length})` }, { id: "block", label: `Block deals (${d.block.length})` },
            { id: "short", label: `Short deals (${d.short.length})` }, { id: "pit", label: `Insider filings (${d.pit.length})` }]} />
        {tab !== "pit" && tab !== "short" && (
          <span className="tt-totals small">Bought <b className="up">₹{num(t.buy, 0)} Cr</b> · Sold <b className="down">₹{num(t.sell, 0)} Cr</b></span>
        )}
      </div>
      <section className="card tt-card">
        <header className="card-head">
          <div>
            <h3>{tab === "pit" ? "Insider filings" : `${tab[0].toUpperCase()}${tab.slice(1)} deals`}{tab !== "pit" && d.as_on ? ` · ${d.as_on}` : ""}</h3>
            <span className="muted small">{ABOUT[tab]}</span>
          </div>
          <SourceNote status={tab === "pit" ? d.pit_status : d.deals_status} source="NSE" />
          <div className="row">
            {tab !== "pit" && tab !== "short" && (
              <Segmented size="small" label="Side" value={side} onChange={setSide} options={[{ id: "all", label: "All" }, { id: "BUY", label: "Buys" }, { id: "SELL", label: "Sells" }]} />
            )}
            <input className="tt-search" type="search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Symbol, company or client" aria-label="Search deals" />
          </div>
        </header>
        {rows.length ? <DataTable rowKey={(r) => JSON.stringify(r)} rows={rows} columns={tab === "pit" ? pitColumns : dealColumns} initialSort={tab === "pit" ? undefined : { key: "value", dir: "desc" }} />
          : <Empty>{tab === "pit" ? "NSE's insider-filing feed returned no records. Each stock's promoter holding trend is on its Fundamental page." : "No deals match."}</Empty>}
      </section>
    </div>
  );
}
