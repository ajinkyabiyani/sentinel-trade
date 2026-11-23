import { useEffect, useState } from "react";

// Symbol logo. The images live in /public/logos: downloaded by tools/fetch_logos.py, and fetched on demand by the backend for any
// stock shown without one (the browser itself never fetches a logo from outside). A contract (option or future) shows its underlying's logo, a NIFTY index the NSE Indices logo,
// a BSE index the BSE logo, an MCX commodity (Gold, Silver...) the MCX logo,
// and anything without a logo keeps the tinted initials tile.
const HUES = [190, 160, 42, 268, 12, 210, 330, 95];
const NSE_INDEX = /^(NIFTY|BANKNIFTY|FINNIFTY|MIDCPNIFTY|INDIA VIX)/;
const MCX = /^(GOLD|SILVER|CRUDEOIL|NATURALGAS|COPPER|ZINC|ALUMINIUM|LEAD|NICKEL)(M|MINI|MIC|GUINEA|PETAL|TEN)?$/;   // MCX commodities: the exchange's logo
const BSE_INDEX =/^(SENSEX|BANKEX|BSE ?(100|200|500|MIDCAP|SMALLCAP|SENSEX))/;

let logos = null;
let loading = null;
const listeners = new Set();
const asked = new Set();                 // symbols already reported this visit
let pending = new Set();
let flush = null;

function loadLogos(force = false) {
  if (force) loading = null;
  loading ??= fetch(`/logos/index.json${force ? `?t=${Date.now()}` : ""}`)
    .then((r) => (r.ok ? r.json() : {}))
    .catch(() => ({}))
    .then((m) => { logos = m; listeners.forEach((f) => f()); });
}

// A stock shown without a logo is reported (a short batch at a time); the backend finds its logo and saves it, and the list is
// read again a little later so the logo appears without a reload.
function report(symbol) {
  if (asked.has(symbol)) return;
  asked.add(symbol);
  pending.add(symbol);
  clearTimeout(flush);
  flush = setTimeout(() => {
    const symbols = [...pending];
    pending = new Set();
    fetch("/api/logos/request", { method: "POST", headers: { "Content-Type": "application/json", "X-Kite-App": "1" }, body: JSON.stringify({ symbols }) })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (d?.queued) [20, 60, 150].forEach((sec) => setTimeout(() => loadLogos(true), sec * 1000)); })
      .catch(() => {});
  }, 1500);
}

function useLogos() {
  const [, redraw] = useState(0);
  useEffect(() => {
    const f = () => redraw((n) => n + 1);
    listeners.add(f);
    if (!logos) loadLogos();
    return () => listeners.delete(f);
  }, []);
  return logos;
}

export function logoFile(map, raw) {
  if (!map) return null;
  const s = String(raw || "").toUpperCase().replace(/^[A-Z]{3}:/, "").replace(/-(EQ|BE|BZ|SM)$/, "").trim();
  if (!s) return null;
  if (map[s]) return map[s];
  const under = s.match(/^([A-Z&-]+?)\d{2}(?:[A-Z]{3}|[1-9OND]\d{2})/)?.[1];  // RELIANCE25NOV1400CE, NIFTY25N1324000CE
  if (under && map[under]) return map[under];
  if (MCX.test(s) || MCX.test(under || "")) return map._MCX || null;
  if (NSE_INDEX.test(s) || NSE_INDEX.test(under || "")) return map._NSEIX || map._NSE || null;
  if (BSE_INDEX.test(s) || BSE_INDEX.test(under || "")) return map._BSE || null;
  return null;
}

export function Logo({ symbol = "", size = 34, text, hue }) {
  const map = useLogos();
  const [broken, setBroken] = useState(false);
  const file = logoFile(map, symbol);
  const plain = /^[A-Z0-9][A-Z0-9&-]{1,19}$/.test(symbol) && !NSE_INDEX.test(symbol) && !BSE_INDEX.test(symbol) && !MCX.test(symbol);
  const target = symbol.match(/^([A-Z&-]+?)\d{2}(?:[A-Z]{3}|[1-9OND]\d{2})/)?.[1] || symbol;   // a contract asks for its underlying
  useEffect(() => { if (map && !file && plain && !text) report(target); }, [map, file, plain, target, text]);
  if (file && !broken) {
    return <img className="logo-img" src={`/logos/${file}`} alt="" width={size} height={size} loading="lazy" style={{ padding: Math.max(2, Math.round(size * 0.1)) }} onError={() => setBroken(true)} />;
  }
  const h = hue ?? HUES[[...symbol].reduce((a, c) => a + c.charCodeAt(0), 0) % HUES.length];
  const txt = text || symbol.replace(/^NIFTY ?/, "N").replace(/[^A-Z0-9&]/g, "").slice(0, symbol.startsWith("NIFTY") ? 3 : 2);
  return (
    <span className="logo-tile" style={{ width: size, height: size, "--h": h, fontSize: size * 0.34 }} aria-hidden="true">
      {txt || "•"}
    </span>
  );
}

// A symbol with its logo in front, for tables and lists.
export function WithLogo({ symbol, size = 22, children }) {
  return <span className="with-logo"><Logo symbol={symbol} size={size} />{children}</span>;
}
