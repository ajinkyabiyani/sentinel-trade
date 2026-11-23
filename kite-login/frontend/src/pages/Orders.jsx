import { useState } from "react";
import { api } from "../lib/api.js";
import { num, time } from "../lib/format.js";
import { ORDER_TYPES } from "../lib/products.js";
import { Badge, DataTable, ErrorNote, Modal, ProductBadge, Segmented, SideBadge, Spinner, useLoad } from "../components/ui.jsx";
import { WithLogo } from "../components/Logo.jsx";

const OPEN = ["OPEN", "TRIGGER PENDING", "AMO REQ RECEIVED", "OPEN PENDING", "VALIDATION PENDING", "PUT ORDER REQ RECEIVED", "MODIFY PENDING", "MODIFIED"];
const statusTone = (s) => (s === "COMPLETE" ? "buy" : s === "REJECTED" ? "sell" : s === "CANCELLED" ? "" : "amber");
const VIEWS = [
  { id: "open", label: "Open" },
  { id: "executed", label: "Executed" },
  { id: "all", label: "All orders" },
  { id: "trades", label: "Trades" },
  { id: "gtt", label: "GTT" },
];

function ModifyDialog({ order, onClose, onDone }) {
  const [f, setF] = useState({
    quantity: order.quantity,
    price: order.price || "",
    trigger_price: order.trigger_price || "",
    order_type: order.order_type,
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const needPrice = f.order_type === "LIMIT" || f.order_type === "SL";
  const needTrigger = f.order_type === "SL" || f.order_type === "SL-M";

  async function save() {
    setBusy(true);
    setError("");
    try {
      await api.modifyOrder(order.variety, order.order_id, {
        quantity: Number(f.quantity),
        order_type: f.order_type,
        price: needPrice ? Number(f.price) : null,
        trigger_price: needTrigger ? Number(f.trigger_price) : null,
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
      title={`Modify ${order.tradingsymbol}`}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="ghost" onClick={onClose}>Cancel</button>
          <button type="button" className="primary" disabled={busy} onClick={save}>
            {busy ? <Spinner small /> : null} Modify order
          </button>
        </>
      }
    >
      <div className="form">
        <Segmented size="small" label="Order type" value={f.order_type} onChange={(v) => setF({ ...f, order_type: v })} options={ORDER_TYPES} />
        <div className="ticket-grid three">
          <label className="field">
            <span>Quantity</span>
            <input type="number" min="1" value={f.quantity} onChange={(e) => setF({ ...f, quantity: e.target.value })} />
          </label>
          <label className={`field${needPrice ? "" : " dim"}`}>
            <span>Price</span>
            <input type="number" step="0.05" disabled={!needPrice} value={needPrice ? f.price : ""} onChange={(e) => setF({ ...f, price: e.target.value })} />
          </label>
          <label className={`field${needTrigger ? "" : " dim"}`}>
            <span>Trigger</span>
            <input type="number" step="0.05" disabled={!needTrigger} value={needTrigger ? f.trigger_price : ""} onChange={(e) => setF({ ...f, trigger_price: e.target.value })} />
          </label>
        </div>
        <ErrorNote error={error} />
      </div>
    </Modal>
  );
}

export default function Orders({ openTicket }) {
  const gtts = useLoad(api.gtts, [], 30_000);
  const orders = useLoad(api.orders, [], 10_000);
  const trades = useLoad(api.trades, [], 30_000);
  const [view, setView] = useState("open");
  const [modify, setModify] = useState(null);
  const [cancelling, setCancelling] = useState(null);
  const [error, setError] = useState("");

  const all = [...(orders.data || [])].reverse();
  const rows = view === "open" ? all.filter((o) => OPEN.includes(o.status)) : view === "executed" ? all.filter((o) => o.status === "COMPLETE") : all;

  async function cancel(o) {
    if (!window.confirm(`Cancel ${o.transaction_type} ${o.quantity} ${o.tradingsymbol}?`)) return;
    setCancelling(o.order_id);
    setError("");
    try {
      await api.cancelOrder(o.variety, o.order_id, o.parent_order_id);
      orders.reload(true);
    } catch (e) {
      setError(e.message);
    } finally {
      setCancelling(null);
    }
  }

  const instrument = (o) => (
    <WithLogo symbol={o.tradingsymbol}><span className="sym">
      <b>{o.tradingsymbol}</b>
      <small>{o.exchange}</small>
    </span></WithLogo>
  );

  const orderCols = [
    { key: "order_timestamp", label: "Time", render: (o) => time(o.order_timestamp) },
    { key: "transaction_type", label: "Type", render: (o) => <SideBadge side={o.transaction_type} /> },
    { key: "tradingsymbol", label: "Instrument", render: instrument },
    { key: "product", label: "Product", render: (o) => <ProductBadge product={o.product} /> },
    { key: "variety", label: "Variety", render: (o) => o.variety?.toUpperCase() },
    { key: "order_type", label: "Order" },
    { key: "quantity", label: "Qty", align: "num", render: (o) => `${o.filled_quantity}/${o.quantity}` },
    { key: "price", label: "Price", align: "num", render: (o) => (o.price ? num(o.price) : "MKT") },
    { key: "trigger_price", label: "Trigger", align: "num", render: (o) => (o.trigger_price ? num(o.trigger_price) : "–") },
    { key: "average_price", label: "Avg", align: "num", render: (o) => (o.average_price ? num(o.average_price) : "–") },
    {
      key: "status",
      label: "Status",
      render: (o) => (
        <Badge tone={statusTone(o.status)} title={o.status_message || ""}>
          {o.status}
        </Badge>
      ),
    },
    {
      key: "act",
      label: "",
      nosort: true,
      render: (o) =>
        OPEN.includes(o.status) ? (
          <span className="row-actions">
            <button type="button" className="ghost small" onClick={() => setModify(o)}>Modify</button>
            <button type="button" className="ghost small danger-text" disabled={cancelling === o.order_id} onClick={() => cancel(o)}>
              {cancelling === o.order_id ? <Spinner small /> : "Cancel"}
            </button>
          </span>
        ) : (
          <button
            type="button"
            className="ghost small"
            onClick={() =>
              openTicket({ exchange: o.exchange, tradingsymbol: o.tradingsymbol, transaction_type: o.transaction_type, product: o.product,
                order_type: o.order_type, quantity: o.quantity, price: o.price || undefined, trigger_price: o.trigger_price || undefined })
            }
          >
            Repeat
          </button>
        ),
    },
  ];

  const tradeCols = [
    { key: "fill_timestamp", label: "Time", render: (t) => time(t.fill_timestamp || t.exchange_timestamp) },
    { key: "transaction_type", label: "Type", render: (t) => <SideBadge side={t.transaction_type} /> },
    { key: "tradingsymbol", label: "Instrument", render: instrument },
    { key: "product", label: "Product", render: (t) => <ProductBadge product={t.product} /> },
    { key: "quantity", label: "Qty", align: "num" },
    { key: "average_price", label: "Price", align: "num", render: (t) => num(t.average_price) },
    { key: "order_id", label: "Order ID", render: (t) => <code>{t.order_id}</code> },
  ];

  return (
    <section className="panel">
      <header className="panel-head">
        <h3>Orders</h3>
        <Segmented
          size="small"
          label="View"
          value={view}
          onChange={setView}
          options={VIEWS.map((v) => ({
            ...v,
            sub: v.id === "open" ? all.filter((o) => OPEN.includes(o.status)).length : v.id === "trades" ? (trades.data || []).length
              : v.id === "gtt" ? (gtts.data || []).filter((g) => g.status === "active").length : undefined,
          }))}
        />
        {(orders.loading || trades.loading) && <Spinner small />}
        <button type="button" className="ghost" onClick={() => { orders.reload(); trades.reload(); }}>Refresh</button>
      </header>
      <ErrorNote error={error || orders.error || trades.error || (view === "gtt" ? gtts.error : "")} />
      {view === "gtt" ? (
        <DataTable
          rows={gtts.data || []}
          rowKey={(g) => g.id}
          empty="No GTTs. Use Protect (GTT) on a holding or position to add an overnight stop and target."
          columns={[
            { key: "tradingsymbol", label: "Instrument", render: (g) => <WithLogo symbol={g.tradingsymbol}><span className="sym"><b>{g.tradingsymbol}</b><small>{g.exchange} · #{g.id}</small></span></WithLogo> },
            { key: "type", label: "Type", render: (g) => (g.type === "two-leg" ? "Stop + target (OCO)" : "Single trigger") },
            { key: "trigger_values", label: "Triggers", align: "num", render: (g) => (g.trigger_values || []).map((v) => num(v)).join(" / ") },
            { key: "orders", label: "Orders", render: (g) => (g.orders || []).map((o) => `${o.transaction_type} ${o.quantity} @ ${num(o.price)}`).join(" · ") },
            { key: "last_price", label: "Price when set", align: "num", render: (g) => num(g.last_price) },
            { key: "status", label: "Status", render: (g) => <Badge tone={g.status === "active" ? "blue" : g.status === "triggered" ? "buy" : ""}>{g.status}</Badge> },
            { key: "expires_at", label: "Expires", render: (g) => (g.expires_at || "").slice(0, 10) },
            {
              key: "act", label: "", nosort: true,
              render: (g) => g.status === "active" ? (
                <button type="button" className="ghost small danger-text" onClick={async () => {
                  if (!window.confirm(`Delete GTT #${g.id} for ${g.tradingsymbol}?`)) return;
                  try { await api.gttDelete(g.id); gtts.reload(true); } catch (e) { setError(e.message); }
                }}>Delete</button>
              ) : null,
            },
          ]}
        />
      ) : view === "trades" ? (
        <DataTable columns={tradeCols} rows={[...(trades.data || [])].reverse()} rowKey={(t) => t.trade_id} empty="No trades today." />
      ) : (
        <DataTable columns={orderCols} rows={rows} rowKey={(o) => o.order_id}
          empty={view === "open" ? "No open orders." : "No orders today."} />
      )}
      {modify && <ModifyDialog order={modify} onClose={() => setModify(null)} onDone={() => { setModify(null); orders.reload(true); }} />}
    </section>
  );
}
