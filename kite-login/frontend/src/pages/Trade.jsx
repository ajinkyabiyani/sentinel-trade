import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../lib/api.js";
import { inr, num, signed, tone } from "../lib/format.js";
import { EQUITY, EXCHANGES, ORDER_TYPES, PRODUCTS, VALIDITIES, VARIETIES, orderTypesFor, productsFor } from "../lib/products.js";
import { useTicks } from "../lib/ticks.js";
import { ErrorNote, Modal, ProductBadge, Segmented, SideBadge, Spinner } from "../components/ui.jsx";
import { WithLogo } from "../components/Logo.jsx";

const BLANK = {
  exchange: "NSE",
  transaction_type: "BUY",
  product: "CNC",
  variety: "regular",
  order_type: "LIMIT",
  quantity: 1,
  price: "",
  trigger_price: "",
  validity: "DAY",
  validity_ttl: 5,
  disclosed_quantity: "",
  iceberg_legs: 2,
  auction_number: "",
  market_protection: "",
  tag: "",
};

function SymbolSearch({ exchange, value, onPick }) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState([]);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const seq = useRef(0);

  useEffect(() => {
    if (!q.trim()) {
      setHits([]);
      return undefined;
    }
    const n = ++seq.current;
    const t = setTimeout(async () => {
      setLoading(true);
      setError("");
      try {
        const r = await api.search(q.trim(), exchange);
        if (n === seq.current) {
          setHits(r);
          setActive(0);
        }
      } catch (e) {
        if (n === seq.current) setError(e.message);
      } finally {
        if (n === seq.current) setLoading(false);
      }
    }, 250);
    return () => clearTimeout(t);
  }, [q, exchange]);

  const pick = (h) => {
    onPick(h);
    setQ("");
    setOpen(false);
  };

  return (
    <div className="search-box">
      <input
        className="search big"
        placeholder={value ? `${value.tradingsymbol} · search another ${exchange} instrument` : `Search ${exchange} instruments, e.g. INFY or NIFTY 24000 CE`}
        value={q}
        onChange={(e) => {
          setQ(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") setActive((a) => Math.min(a + 1, hits.length - 1));
          else if (e.key === "ArrowUp") setActive((a) => Math.max(a - 1, 0));
          else if (e.key === "Enter" && hits[active]) {
            e.preventDefault();
            pick(hits[active]);
          }
        }}
        aria-label="Instrument"
      />
      {loading && <Spinner small />}
      {open && (hits.length > 0 || error) && (
        <ul className="search-pop" role="listbox">
          {error && <li className="error">{error}</li>}
          {hits.map((h, i) => (
            <li
              key={`${h.exchange}:${h.tradingsymbol}`}
              role="option"
              aria-selected={i === active}
              className={i === active ? "active" : ""}
              onMouseDown={() => pick(h)}
              onMouseEnter={() => setActive(i)}
            >
              <WithLogo symbol={h.tradingsymbol} size={20}><b>{h.tradingsymbol}</b></WithLogo>
              <span className="muted">{h.name}</span>
              <span className="chip">{h.instrument_type}</span>
              {h.expiry && <span className="muted">{h.expiry}</span>}
              {h.lot_size > 1 && <span className="muted">lot {h.lot_size}</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function productList(exchange, variety, allowed) {
  const base = productsFor(exchange, allowed);
  if (variety === "co") return base.map((p) => (p.id === "MIS" ? p : { ...p, disabled: true, reason: "Cover orders are MIS only" }));
  if (variety === "auction") return base.map((p) => (p.id === "CNC" ? p : { ...p, disabled: true, reason: "Auction orders are CNC only" }));
  return base;
}

export default function Trade({ session, prefill, go }) {
  const [o, setO] = useState(BLANK);
  const [inst, setInst] = useState(null);
  const [quote, setQuote] = useState(null);
  const [margin, setMargin] = useState(null);
  const [review, setReview] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [placed, setPlaced] = useState(null);
  const [more, setMore] = useState(false);
  const set = (k, v) => setO((s) => ({ ...s, [k]: v }));

  // Prefill from positions / holdings.
  useEffect(() => {
    if (!prefill) return;
    const { _at, tradingsymbol, ...rest } = prefill;
    setO({ ...BLANK, ...Object.fromEntries(Object.entries(rest).filter(([, v]) => v !== undefined && v !== null)) });
    setInst({ tradingsymbol, exchange: prefill.exchange, lot_size: 1, tick_size: 0.05 });
    setMargin(null);
    setPlaced(null);
    setError("");
    api
      .search(tradingsymbol, prefill.exchange)
      .then((r) => {
        const exact = r.find((h) => h.tradingsymbol === tradingsymbol);
        if (exact) setInst(exact);
      })
      .catch(() => {});
  }, [prefill?._at]); // eslint-disable-line react-hooks/exhaustive-deps

  const key = inst ? `${inst.exchange}:${inst.tradingsymbol}` : "";
  useEffect(() => {
    if (!key) return undefined;
    let alive = true;
    const load = () =>
      api
        .quote([key])
        .then((r) => alive && setQuote(r[key] || null))
        .catch(() => {});
    setQuote(null);
    load();
    const t = setInterval(() => document.visibilityState === "visible" && load(), 30_000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [key]);

  const stream = useTicks(key ? [key] : []);
  const tick = stream.ticks[key];
  const q = quote || tick ? { ...(quote || {}), ...(tick ? { last_price: tick.last_price, net_change: tick.change, bid: tick.bid ?? quote?.bid, ask: tick.ask ?? quote?.ask } : {}) } : null;

  const products = useMemo(() => productList(o.exchange, o.variety, session.products), [o.exchange, o.variety, session.products]);
  const types = orderTypesFor(o.variety);
  const varieties = VARIETIES.map((v) =>
    v.id === "auction" && !EQUITY.includes(o.exchange) ? { ...v, disabled: true, reason: "NSE/BSE equity only" } : v,
  );

  // Keep the ticket valid when exchange / variety change. Checked against the state being updated, not this
  // render's lists: on a prefill the two can differ (an NFO ticket checked against NSE products lost NRML).
  useEffect(() => {
    setO((s) => {
      let n = s;
      const ps = productList(s.exchange, s.variety, session.products);
      if (ps.find((p) => p.id === s.product)?.disabled) {
        const first = ps.find((p) => !p.disabled);
        if (first) n = { ...n, product: first.id };
      }
      const ts = orderTypesFor(n.variety);
      if (!ts.includes(n.order_type)) n = { ...n, order_type: ts[0] };
      if (n.variety === "auction" && !EQUITY.includes(n.exchange)) n = { ...n, variety: "regular" };
      return n;
    });
    setMargin(null);
  }, [o.exchange, o.variety]); // eslint-disable-line react-hooks/exhaustive-deps

  const needPrice = o.order_type === "LIMIT" || o.order_type === "SL";
  const needTrigger = o.order_type === "SL" || o.order_type === "SL-M" || o.variety === "co";
  const lot = inst?.lot_size || 1;
  const refPrice = Number(o.price) || q?.last_price || 0;
  const value = refPrice * Number(o.quantity || 0);

  function order() {
    const n = (v) => (v === "" || v === null || v === undefined ? null : Number(v));
    return {
      variety: o.variety,
      exchange: o.exchange,
      tradingsymbol: inst?.tradingsymbol,
      transaction_type: o.transaction_type,
      quantity: Number(o.quantity),
      product: o.product,
      order_type: o.order_type,
      price: needPrice ? n(o.price) : null,
      trigger_price: needTrigger ? n(o.trigger_price) : null,
      validity: o.validity,
      validity_ttl: o.validity === "TTL" ? n(o.validity_ttl) : null,
      disclosed_quantity: n(o.disclosed_quantity),
      iceberg_legs: o.variety === "iceberg" ? n(o.iceberg_legs) : null,
      auction_number: o.variety === "auction" ? o.auction_number || null : null,
      market_protection: n(o.market_protection),
      tag: o.tag || null,
    };
  }

  async function checkMargin() {
    setError("");
    setBusy("margin");
    try {
      setMargin(await api.orderMargin(order()));
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function place() {
    setError("");
    setBusy("place");
    try {
      const r = await api.placeOrder(order());
      setPlaced({ ...r, ...order() });
      setReview(false);
    } catch (e) {
      setError(e.message);
      setReview(false);
    } finally {
      setBusy("");
    }
  }

  const buy = o.transaction_type === "BUY";
  const wholeLots = lot <= 1 || Number(o.quantity) % lot === 0;   // the exchange accepts derivatives only in whole lots
  const ready = inst && Number(o.quantity) > 0 && wholeLots && (!needPrice || Number(o.price) > 0) && (!needTrigger || Number(o.trigger_price) > 0);

  return (
    <div className="trade-layout">
      <section className={`panel ticket ${buy ? "is-buy" : "is-sell"}`}>
        <header className="ticket-head">
          <Segmented
            label="Side"
            value={o.transaction_type}
            onChange={(v) => set("transaction_type", v)}
            options={[{ id: "BUY", label: "Buy" }, { id: "SELL", label: "Sell" }]}
          />
          <Segmented
            size="small"
            label="Exchange"
            value={o.exchange}
            onChange={(v) => {
              set("exchange", v);
              setInst(null);
            }}
            options={EXCHANGES.filter((x) => !session.exchanges?.length || session.exchanges.includes(x.id))}
          />
        </header>

        <SymbolSearch exchange={o.exchange} value={inst} onPick={(h) => { setInst(h); setMargin(null); if (h.lot_size > 1) set("quantity", h.lot_size); }} />

        {inst && (
          <div className="quote-strip">
            <div>
              <WithLogo symbol={inst.tradingsymbol} size={28}><b className="quote-sym">{inst.tradingsymbol}</b></WithLogo>
              <span className="muted">
                {inst.exchange}
                {inst.name ? ` · ${inst.name}` : ""}
                {inst.expiry ? ` · exp ${inst.expiry}` : ""}
              </span>
            </div>
            {q ? (
              <div className="quote-nums">
                {tick && stream.live && <span className="live-tag">LIVE</span>}
                <span key={q.last_price} className={`ltp ${tick ? (q.net_change >= 0 ? "flash-up" : "flash-down") : ""}`}>{num(q.last_price)}</span>
                <span className={tone(q.net_change)}>{signed(q.net_change)}</span>
                <span className="muted">bid {num(q.bid)} · ask {num(q.ask)}</span>
                <span className="muted">
                  circuit {num(q.lower_circuit)} – {num(q.upper_circuit)}
                </span>
              </div>
            ) : (
              <Spinner small />
            )}
          </div>
        )}

        <div className="ticket-section">
          <span className="eyebrow">Product</span>
          <div className="product-pick">
            {products.map((p) => (
              <button
                key={p.id}
                type="button"
                className={`product-opt${o.product === p.id ? " on" : ""}`}
                disabled={p.disabled}
                title={p.reason || p.desc}
                onClick={() => set("product", p.id)}
              >
                <b>{p.label}</b>
                <span>{p.name}</span>
                {(p.disabled ? p.reason : p.note) && <small>{p.disabled ? p.reason : p.note}</small>}
              </button>
            ))}
          </div>
        </div>

        <div className="ticket-grid">
          <div className="ticket-section">
            <span className="eyebrow">Variety</span>
            <Segmented size="small" label="Variety" value={o.variety} onChange={(v) => set("variety", v)} options={varieties} />
          </div>
          <div className="ticket-section">
            <span className="eyebrow">Order type</span>
            <Segmented
              size="small"
              label="Order type"
              value={o.order_type}
              onChange={(v) => set("order_type", v)}
              options={ORDER_TYPES.map((t) => (types.includes(t.id) ? t : { ...t, disabled: true, reason: "Not allowed for this variety" }))}
            />
          </div>
        </div>

        <div className="ticket-grid three">
          <label className="field">
            <span>Quantity{lot > 1 ? ` (lot ${lot})` : ""}</span>
            <input type="number" min={lot} step={lot} value={o.quantity} onChange={(e) => set("quantity", e.target.value)} />
            {lot > 1 && <small className={wholeLots ? "hint" : "hint down"}>{wholeLots ? `${num(Number(o.quantity) / lot, 0)} lots of ${lot}` : `Must be a multiple of the lot size (${lot})`}</small>}
          </label>
          <label className={`field${needPrice ? "" : " dim"}`}>
            <span>Price</span>
            <input type="number" min="0" step={inst?.tick_size || 0.05} value={needPrice ? o.price : ""} disabled={!needPrice}
              placeholder={needPrice ? "" : "Market"} onChange={(e) => set("price", e.target.value)} />
          </label>
          <label className={`field${needTrigger ? "" : " dim"}`}>
            <span>{o.variety === "co" ? "Stop-loss trigger" : "Trigger price"}</span>
            <input type="number" min="0" step={inst?.tick_size || 0.05} value={needTrigger ? o.trigger_price : ""} disabled={!needTrigger}
              onChange={(e) => set("trigger_price", e.target.value)} />
          </label>
        </div>

        {o.transaction_type === "SELL" && (o.order_type === "SL" || o.order_type === "SL-M") && ["CNC", "MTF"].includes(o.product) && (
          <p className="warn-note">
            A stop-loss order is a day order: Kite cancels it at 15:30 if it hasn't triggered. To keep a stop on a delivery position
            overnight, use <b>Protect (GTT)</b> from Holdings or Positions.
          </p>
        )}

        <div className="ticket-grid">
          <div className="ticket-section">
            <span className="eyebrow">Validity</span>
            <Segmented size="small" label="Validity" value={o.validity} onChange={(v) => set("validity", v)} options={VALIDITIES} />
          </div>
          {o.validity === "TTL" && (
            <label className="field narrow">
              <span>Minutes</span>
              <input type="number" min="1" max="1440" value={o.validity_ttl} onChange={(e) => set("validity_ttl", e.target.value)} />
            </label>
          )}
          {o.variety === "iceberg" && (
            <label className="field narrow">
              <span>Iceberg legs (2–50)</span>
              <input type="number" min="2" max="50" value={o.iceberg_legs} onChange={(e) => set("iceberg_legs", e.target.value)} />
              <small className="hint">≈ {num(Math.ceil(Number(o.quantity) / Number(o.iceberg_legs || 1)), 0)} per leg</small>
            </label>
          )}
          {o.variety === "auction" && (
            <label className="field narrow">
              <span>Auction number</span>
              <input value={o.auction_number} onChange={(e) => set("auction_number", e.target.value)} />
            </label>
          )}
        </div>

        <button type="button" className="link" onClick={() => setMore((m) => !m)}>
          {more ? "− Fewer options" : "+ More options"} (disclosed qty, market protection, tag)
        </button>
        {more && (
          <div className="ticket-grid three fade-in">
            <label className="field">
              <span>Disclosed qty</span>
              <input type="number" min="0" value={o.disclosed_quantity} onChange={(e) => set("disclosed_quantity", e.target.value)} />
            </label>
            <label className="field">
              <span>Market protection %</span>
              <input type="number" step="0.5" placeholder="auto" value={o.market_protection} onChange={(e) => set("market_protection", e.target.value)} />
              <small className="hint">MARKET / SL-M only; blank = Kite auto</small>
            </label>
            <label className="field">
              <span>Tag</span>
              <input maxLength={20} value={o.tag} onChange={(e) => set("tag", e.target.value)} placeholder="e.g. swing" />
            </label>
          </div>
        )}

        <div className="ticket-foot">
          <div>
            <span className="muted">Approx. value</span>
            <b>{inr(value)}</b>
            {margin && (
              <span className="muted">
                · margin <b>{inr(margin.total)}</b> · charges {inr(margin.charges.total)}
              </span>
            )}
          </div>
          <div className="row">
            <button type="button" className="ghost" disabled={!ready || busy} onClick={checkMargin}>
              {busy === "margin" ? <Spinner small /> : null} Margin & charges
            </button>
            <button type="button" className={buy ? "buy" : "sell"} disabled={!ready || busy} onClick={() => setReview(true)}>
              Review {buy ? "buy" : "sell"}
            </button>
          </div>
        </div>
        <ErrorNote error={error} />
        {placed && (
          <div className="placed fade-in">
            ✓ Order <code>{placed.order_id}</code> sent: {placed.transaction_type} {placed.quantity} {placed.tradingsymbol} {placed.product}{" "}
            {placed.order_type}.{" "}
            <button type="button" className="link" onClick={() => go("orders")}>
              View in Orders →
            </button>
          </div>
        )}
      </section>

      <aside className="stack">
        {margin && (
          <section className="panel">
            <header className="panel-head">
              <h3>Margin & charges</h3>
            </header>
            <dl className="facts">
              <dt>Required margin</dt>
              <dd>
                <b>{inr(margin.total)}</b>
              </dd>
              {margin.leverage ? (<><dt>Leverage</dt><dd>{num(margin.leverage, 2)}×</dd></>) : null}
              {margin.span ? (<><dt>SPAN</dt><dd>{inr(margin.span)}</dd></>) : null}
              {margin.exposure ? (<><dt>Exposure</dt><dd>{inr(margin.exposure)}</dd></>) : null}
              <dt>Brokerage</dt>
              <dd>{inr(margin.charges.brokerage)}</dd>
              <dt>STT / CTT</dt>
              <dd>{inr(margin.charges.stt)}</dd>
              <dt>Exchange</dt>
              <dd>{inr(margin.charges.exchange)}</dd>
              <dt>SEBI</dt>
              <dd>{inr(margin.charges.sebi)}</dd>
              <dt>Stamp duty</dt>
              <dd>{inr(margin.charges.stamp_duty)}</dd>
              <dt>GST</dt>
              <dd>{inr(margin.charges.gst)}</dd>
              <dt>Total charges</dt>
              <dd>
                <b>{inr(margin.charges.total)}</b>
              </dd>
            </dl>
          </section>
        )}
        <section className="panel guide">
          <header className="panel-head">
            <h3>Zerodha products</h3>
          </header>
          {PRODUCTS.map((p) => (
            <div key={p.id} className={`guide-row${o.product === p.id ? " on" : ""}`}>
              <ProductBadge product={p.id} />
              <div>
                <b>{p.name}</b>
                <p>{p.desc}</p>
              </div>
            </div>
          ))}
          <span className="eyebrow">Varieties</span>
          {VARIETIES.map((v) => (
            <p key={v.id} className={`guide-line${o.variety === v.id ? " on" : ""}`}>
              <b>{v.label}</b> {v.desc}
            </p>
          ))}
          <span className="eyebrow">Order types</span>
          {ORDER_TYPES.map((t) => (
            <p key={t.id} className={`guide-line${o.order_type === t.id ? " on" : ""}`}>
              <b>{t.label}</b> {t.desc}
            </p>
          ))}
        </section>
      </aside>

      {review && (
        <Modal
          title="Confirm order"
          onClose={() => setReview(false)}
          footer={
            <>
              <button type="button" className="ghost" onClick={() => setReview(false)}>
                Cancel
              </button>
              <button type="button" className={buy ? "buy" : "sell"} disabled={busy === "place"} onClick={place}>
                {busy === "place" ? <Spinner small /> : null} Place {buy ? "buy" : "sell"} order
              </button>
            </>
          }
        >
          <div className="review">
            <div className="review-title">
              <SideBadge side={o.transaction_type} /> <b>{inst?.tradingsymbol}</b> <span className="muted">{o.exchange}</span>
            </div>
            <dl className="facts">
              <dt>Product</dt>
              <dd>
                <ProductBadge product={o.product} /> {PRODUCTS.find((p) => p.id === o.product)?.name}
              </dd>
              <dt>Variety</dt>
              <dd>{VARIETIES.find((v) => v.id === o.variety)?.label}</dd>
              <dt>Order type</dt>
              <dd>{o.order_type}</dd>
              <dt>Quantity</dt>
              <dd>{o.quantity}{lot > 1 ? ` (${num(Number(o.quantity) / lot, 2)} lots)` : ""}</dd>
              {needPrice && (<><dt>Price</dt><dd>{num(o.price)}</dd></>)}
              {needTrigger && (<><dt>Trigger</dt><dd>{num(o.trigger_price)}</dd></>)}
              <dt>Validity</dt>
              <dd>{o.validity}{o.validity === "TTL" ? ` (${o.validity_ttl} min)` : ""}</dd>
              <dt>Approx. value</dt>
              <dd>{inr(value)}</dd>
              {margin && (<><dt>Margin</dt><dd>{inr(margin.total)}</dd></>)}
            </dl>
            <p className="warn">This sends a real order to Zerodha.</p>
          </div>
        </Modal>
      )}
    </div>
  );
}
