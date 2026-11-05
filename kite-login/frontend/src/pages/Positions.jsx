import { useState } from "react";
import { api } from "../lib/api.js";
import { inr, num, signed, tone } from "../lib/format.js";
import { convertTargets, productById } from "../lib/products.js";
import { useTicks } from "../lib/ticks.js";
import { DataTable, ErrorNote, Modal, ProductBadge, Segmented, Spinner, Stat, useLoad } from "../components/ui.jsx";

function ConvertDialog({ pos, kind, onClose, onDone }) {
  const targets = convertTargets(pos.exchange, pos.product);
  const [to, setTo] = useState(targets[0]);
  const [qty, setQty] = useState(Math.abs(pos.quantity));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function save() {
    setBusy(true);
    setError("");
    try {
      await api.convertPosition({
        exchange: pos.exchange,
        tradingsymbol: pos.tradingsymbol,
        transaction_type: pos.quantity > 0 ? "BUY" : "SELL",
        position_type: kind === "day" || !pos.overnight_quantity ? "day" : "overnight",
        quantity: Number(qty),
        old_product: pos.product,
        new_product: to,
      });
      onDone();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={`Convert ${pos.tradingsymbol}`}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="ghost" onClick={onClose}>Cancel</button>
          <button type="button" className="primary" disabled={busy || !(qty > 0)} onClick={save}>
            {busy ? <Spinner small /> : null} Convert to {to}
          </button>
        </>
      }
    >
      <div className="form">
        <p>
          From <ProductBadge product={pos.product} /> {productById[pos.product]?.name} to:
        </p>
        <Segmented label="New product" value={to} onChange={setTo}
          options={targets.map((p) => ({ id: p, label: p, sub: productById[p]?.name, desc: productById[p]?.desc }))} />
        <p className="muted">{productById[to]?.desc}</p>
        <label className="field narrow">
          <span>Quantity (max {Math.abs(pos.quantity)})</span>
          <input type="number" min="1" max={Math.abs(pos.quantity)} value={qty} onChange={(e) => setQty(e.target.value)} />
        </label>
        <ErrorNote error={error} />
      </div>
    </Modal>
  );
}

export default function Positions({ openTicket, openGtt }) {
  const pos = useLoad(api.positions, [], 30_000);
  const live = useTicks([...(pos.data?.net || []), ...(pos.data?.day || [])].map((p) => `${p.exchange}:${p.tradingsymbol}`));
  const [kind, setKind] = useState("net");
  const [productFilter, setProductFilter] = useState("ALL");
  const [convert, setConvert] = useState(null);
  // Live P&L: Kite's formula (sell value - buy value) + net qty x LTP x multiplier, re-evaluated on every tick
  const list = (pos.data?.[kind] || [])
    .filter((p) => productFilter === "ALL" || p.product === productFilter)
    .map((p) => {
      const t = live.ticks[`${p.exchange}:${p.tradingsymbol}`];
      if (!t?.last_price) return p;
      const mult = p.multiplier || 1;
      const pnl = (p.sell_value || 0) - (p.buy_value || 0) + p.quantity * t.last_price * mult;
      return { ...p, last_price: t.last_price, pnl, m2m: p.m2m + (pnl - p.pnl), _live: true };
    });
  const open = list.filter((p) => p.quantity !== 0);
  const pnl = list.reduce((a, p) => a + (p.pnl || 0), 0);
  const m2m = list.reduce((a, p) => a + (p.m2m || 0), 0);

  const exit = (p) =>
    openTicket({ exchange: p.exchange, tradingsymbol: p.tradingsymbol, transaction_type: p.quantity > 0 ? "SELL" : "BUY",
      product: p.product, order_type: "MARKET", quantity: Math.abs(p.quantity) });
  const add = (p) =>
    openTicket({ exchange: p.exchange, tradingsymbol: p.tradingsymbol, transaction_type: p.quantity >= 0 ? "BUY" : "SELL",
      product: p.product, order_type: "LIMIT", price: p.last_price, quantity: Math.abs(p.quantity) || 1 });

  const cols = [
    { key: "product", label: "Product", render: (p) => <ProductBadge product={p.product} /> },
    { key: "tradingsymbol", label: "Instrument", render: (p) => <span className="sym"><b>{p.tradingsymbol}</b><small>{p.exchange}</small></span> },
    { key: "quantity", label: "Qty", align: "num", render: (p) => <span className={tone(p.quantity)}>{p.quantity}</span> },
    { key: "average_price", label: "Avg", align: "num", render: (p) => num(p.average_price) },
    { key: "last_price", label: "LTP", align: "num", render: (p) => num(p.last_price) },
    { key: "pnl", label: "P&L", align: "num", render: (p) => <span className={tone(p.pnl)}>{signed(p.pnl)}</span> },
    { key: "m2m", label: "M2M", align: "num", render: (p) => <span className={tone(p.m2m)}>{signed(p.m2m)}</span> },
    { key: "buy_quantity", label: "Bought", align: "num", render: (p) => `${p.buy_quantity} @ ${num(p.buy_price)}` },
    { key: "sell_quantity", label: "Sold", align: "num", render: (p) => `${p.sell_quantity} @ ${num(p.sell_price)}` },
    {
      key: "act",
      label: "",
      nosort: true,
      render: (p) => (
        <span className="row-actions">
          {p.quantity > 0 && ["CNC", "MTF"].includes(p.product) && (p.exchange === "NSE" || p.exchange === "BSE") && (
            <button type="button" className="ghost small" title="Overnight stop + target (GTT) for a delivery position"
              onClick={() => openGtt({ mode: "protect", exchange: p.exchange, tradingsymbol: p.tradingsymbol, quantity: p.quantity, entry: p.average_price })}>
              Protect
            </button>
          )}
          {p.quantity !== 0 && <button type="button" className="ghost small danger-text" onClick={() => exit(p)}>Exit</button>}
          <button type="button" className="ghost small" onClick={() => add(p)}>Add</button>
          {p.quantity !== 0 && <button type="button" className="ghost small" onClick={() => setConvert(p)}>Convert</button>}
        </span>
      ),
    },
  ];

  return (
    <div className="stack">
      <section className="panel">
        <header className="panel-head">
          <h3>Positions {live.live && list.some((p) => p._live) && <span className="live-tag">LIVE</span>}</h3>
          <Segmented size="small" label="Positions" value={kind} onChange={setKind}
            options={[{ id: "net", label: "Net" }, { id: "day", label: "Day" }]} />
          <Segmented size="small" label="Product" value={productFilter} onChange={setProductFilter}
            options={["ALL", "CNC", "MIS", "NRML", "MTF"].map((p) => ({ id: p, label: p === "ALL" ? "All" : p }))} />
          {pos.loading && <Spinner small />}
        </header>
        <div className="stats">
          <Stat label="Total P&L" value={inr(pnl)} tone={tone(pnl)} />
          <Stat label="M2M" value={inr(m2m)} tone={tone(m2m)} />
          <Stat label="Open positions" value={open.length} />
          <Stat label="Closed today" value={list.length - open.length} />
        </div>
        <ErrorNote error={pos.error} />
        <DataTable columns={cols} rows={list} rowKey={(p) => `${p.exchange}:${p.tradingsymbol}:${p.product}`}
          rowClass={(p) => (p.quantity === 0 ? "closed" : "")} empty="No positions." />
      </section>
      {convert && (
        <ConvertDialog pos={convert} kind={kind} onClose={() => setConvert(null)} onDone={() => { setConvert(null); pos.reload(true); }} />
      )}
    </div>
  );
}
