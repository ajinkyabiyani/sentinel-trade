import { useState } from "react";
import { api } from "../lib/api.js";
import { inr, num, signed, tone } from "../lib/format.js";
import { useTicks } from "../lib/ticks.js";
import { Badge, DataTable, ErrorNote, Spinner, Stat, useLoad } from "../components/ui.jsx";
import { WithLogo } from "../components/Logo.jsx";

export default function Holdings({ openTicket, openGtt }) {
  const h = useLoad(api.holdings, [], 60_000);
  const live = useTicks((h.data || []).map((r) => `${r.exchange}:${r.tradingsymbol}`));
  const [q, setQ] = useState("");
  const rows = (h.data || [])
    .map((r0) => {
      const t = live.ticks[`${r0.exchange}:${r0.tradingsymbol}`];
      const r = t?.last_price ? { ...r0, last_price: t.last_price,
        day_change: t.last_price - (r0.close_price || t.last_price),
        day_change_percentage: r0.close_price ? (t.last_price / r0.close_price - 1) * 100 : r0.day_change_percentage } : r0;
      const qty = (r.quantity || 0) + (r.t1_quantity || 0);
      const invested = qty * (r.average_price || 0);
      const current = qty * (r.last_price || 0);
      if (t?.last_price) r.pnl = current - invested;
      return { ...r, qty, invested, current, pnl_pct: invested ? (100 * (current - invested)) / invested : 0, mtf_qty: r.mtf?.quantity || 0 };
    })
    .filter((r) => !q || r.tradingsymbol.includes(q.toUpperCase()));
  const invested = rows.reduce((a, r) => a + r.invested, 0);
  const current = rows.reduce((a, r) => a + r.current, 0);
  const dayPnl = rows.reduce((a, r) => a + (r.day_change || 0) * r.qty, 0);

  const trade = (r, side) =>
    openTicket({ exchange: r.exchange, tradingsymbol: r.tradingsymbol, transaction_type: side, product: r.mtf_qty ? "MTF" : "CNC",
      order_type: "LIMIT", price: r.last_price, quantity: side === "SELL" ? r.qty : 1 });

  const cols = [
    {
      key: "tradingsymbol",
      label: "Instrument",
      render: (r) => (
        <WithLogo symbol={r.tradingsymbol}><span className="sym">
          <b>
            {r.tradingsymbol} {r.mtf_qty > 0 && <Badge tone="teal">MTF {r.mtf_qty}</Badge>}
            {r.t1_quantity > 0 && <Badge tone="amber">T1 {r.t1_quantity}</Badge>}
          </b>
          <small>{r.exchange}</small>
        </span></WithLogo>
      ),
    },
    { key: "qty", label: "Qty", align: "num" },
    { key: "average_price", label: "Avg cost", align: "num", render: (r) => num(r.average_price) },
    { key: "last_price", label: "LTP", align: "num", render: (r) => num(r.last_price) },
    { key: "invested", label: "Invested", align: "num", render: (r) => inr(r.invested, 0) },
    { key: "current", label: "Current", align: "num", render: (r) => inr(r.current, 0) },
    { key: "pnl", label: "P&L", align: "num", render: (r) => <span className={tone(r.pnl)}>{signed(r.pnl)}</span> },
    { key: "pnl_pct", label: "Net %", align: "num", render: (r) => <span className={tone(r.pnl_pct)}>{signed(r.pnl_pct)}%</span> },
    { key: "day_change_percentage", label: "Day %", align: "num", render: (r) => <span className={tone(r.day_change_percentage)}>{signed(r.day_change_percentage)}%</span> },
    {
      key: "act",
      label: "",
      nosort: true,
      render: (r) => (
        <span className="row-actions">
          <button type="button" className="ghost small" title="Stop-loss + target that stay on Kite overnight"
            onClick={() => openGtt({ mode: "protect", exchange: r.exchange, tradingsymbol: r.tradingsymbol, quantity: r.qty, entry: r.average_price })}>
            Protect (GTT)
          </button>
          <button type="button" className="ghost small" onClick={() => trade(r, "BUY")}>Buy</button>
          <button type="button" className="ghost small danger-text" onClick={() => trade(r, "SELL")}>Sell</button>
        </span>
      ),
    },
  ];

  return (
    <section className="panel">
      <header className="panel-head">
        <h3>Holdings {live.live && Object.keys(live.ticks).length > 0 && <span className="live-tag">LIVE</span>}</h3>
        <input className="search" placeholder="Search…" value={q} onChange={(e) => setQ(e.target.value)} />
        {h.loading && <Spinner small />}
      </header>
      <div className="stats">
        <Stat label="Invested" value={inr(invested, 0)} />
        <Stat label="Current value" value={inr(current, 0)} />
        <Stat label="Total P&L" value={inr(current - invested, 0)} tone={tone(current - invested)}
          sub={invested ? `${signed((100 * (current - invested)) / invested)}%` : ""} />
        <Stat label="Day's P&L" value={inr(dayPnl, 0)} tone={tone(dayPnl)} />
      </div>
      <ErrorNote error={h.error} />
      <DataTable columns={cols} rows={rows} rowKey={(r) => `${r.exchange}:${r.tradingsymbol}`} initialSort={{ key: "current", dir: "desc" }}
        empty="No holdings." />
    </section>
  );
}
