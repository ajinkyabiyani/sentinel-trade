import { useEffect, useState } from "react";
import { api } from "../lib/api.js";
import { inr, num, signed } from "../lib/format.js";
import { ErrorNote, Modal, Segmented, Spinner } from "./ui.jsx";

// prefill: { mode: "protect" | "buy-stop", exchange, tradingsymbol, quantity, stop, targets: [t1, t2], trigger, entry }
export default function GttDialog({ prefill, onClose, go }) {
  const [mode, setMode] = useState(prefill.mode || "protect");
  const [qty, setQty] = useState(prefill.quantity || "");
  const [stop, setStop] = useState(prefill.stop || "");
  const [t1, setT1] = useState(prefill.targets?.[0] || "");
  const [t2, setT2] = useState(prefill.targets?.[1] || "");
  const [trigger, setTrigger] = useState(prefill.trigger || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState(null);
  const [ltp, setLtp] = useState(null);
  const key = `${prefill.exchange}:${prefill.tradingsymbol}`;

  useEffect(() => {
    api.quote([key]).then((q) => setLtp(q[key]?.last_price ?? null)).catch(() => {});
  }, [key]);

  const ref = Number(prefill.entry) || ltp || 0;
  const pct = (v) => (ref && Number(v) ? `${signed((Number(v) / ref - 1) * 100, 1)}%` : "");
  const targets = [t1, t2].map(Number).filter((x) => x > 0);
  const ready = Number(qty) > 0 && (mode === "protect" ? Number(stop) > 0 && targets.length > 0 : Number(trigger) > 0);
  const half = targets.length > 1 ? Math.round(Number(qty) / 2) : Number(qty);

  async function submit() {
    setBusy(true);
    setError("");
    try {
      const base = { exchange: prefill.exchange, tradingsymbol: prefill.tradingsymbol, quantity: Number(qty) };
      setDone(mode === "protect"
        ? await api.gttProtect({ ...base, stop: Number(stop), targets })
        : await api.gttBuyStop({ ...base, trigger: Number(trigger) }));
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={`GTT · ${prefill.tradingsymbol}`}
      onClose={onClose}
      footer={done ? (
        <>
          <button type="button" className="ghost" onClick={() => { onClose(); go("orders"); }}>View GTTs</button>
          <button type="button" className="primary" onClick={onClose}>Done</button>
        </>
      ) : (
        <>
          <button type="button" className="ghost" onClick={onClose}>Cancel</button>
          <button type="button" className="primary" disabled={!ready || busy} onClick={submit}>
            {busy ? <Spinner small /> : null} {mode === "protect" ? "Place stop + target GTT" : "Place buy-stop GTT"}
          </button>
        </>
      )}
    >
      {done ? (
        <div className="placed fade-in">
          ✓ GTT placed on Kite.{" "}
          {mode === "protect"
            ? done.placed.map((p) => `#${p.trigger_id}: ${p.quantity} sh, stop ${num(p.stop)} / target ${num(p.target)}`).join(" · ")
            : `#${done.trigger_id}: buys ${qty} when ${prefill.tradingsymbol} trades at ${num(done.trigger)} (limit ${num(done.limit)}).`}
        </div>
      ) : (
        <div className="form">
          <Segmented size="small" label="GTT type" value={mode} onChange={setMode}
            options={[{ id: "protect", label: "Protect a position (stop + target)" }, { id: "buy-stop", label: "Buy-stop entry" }]} />
          <p className="muted gtt-why">
            {mode === "protect"
              ? "A normal stop-loss order is cancelled at 15:30. A GTT stays on Kite for up to a year: whichever of stop or target trades first sells, and the other leg is cancelled. With two targets you get two GTTs, each for half the shares."
              : "Waits at the Darvas trigger and buys on whichever day price trades through it, with a limit 0.5% above so it doesn't chase a spike. Add a stop-loss GTT once it fills."}
          </p>
          {ltp && <p className="muted">Last price {num(ltp)}{prefill.entry ? ` · plan entry ${num(prefill.entry)}` : ""}</p>}
          <div className="ticket-grid three">
            <label className="field">
              <span>Quantity</span>
              <input type="number" min="1" value={qty} onChange={(e) => setQty(e.target.value)} />
            </label>
            {mode === "protect" ? (
              <label className="field">
                <span>Stop-loss trigger</span>
                <input type="number" step="0.05" value={stop} onChange={(e) => setStop(e.target.value)} />
                <small className="hint down">{pct(stop)} · sells at a limit 1% below</small>
              </label>
            ) : (
              <label className="field">
                <span>Buy trigger</span>
                <input type="number" step="0.05" value={trigger} onChange={(e) => setTrigger(e.target.value)} />
                <small className="hint">{pct(trigger)} from {prefill.entry ? "entry" : "last price"}</small>
              </label>
            )}
            {mode === "protect" && (
              <label className="field">
                <span>Target 1</span>
                <input type="number" step="0.05" value={t1} onChange={(e) => setT1(e.target.value)} />
                <small className="hint up">{pct(t1)}</small>
              </label>
            )}
          </div>
          {mode === "protect" && (
            <div className="ticket-grid three">
              <label className="field">
                <span>Target 2 (optional)</span>
                <input type="number" step="0.05" value={t2} onChange={(e) => setT2(e.target.value)} />
                <small className="hint up">{pct(t2)}</small>
              </label>
              {ready && (
                <div className="gtt-summary">
                  <small className="muted">If stopped out</small>
                  <b className="down">{inr((Number(stop) - ref) * Number(qty), 0)}</b>
                  <small className="muted">{targets.length > 1 ? `${half} sh at T1, ${Number(qty) - half} at T2` : `${qty} sh at target`}</small>
                </div>
              )}
            </div>
          )}
          <ErrorNote error={error} />
          <p className="warn">This places a real GTT on your Zerodha account.</p>
        </div>
      )}
    </Modal>
  );
}
