const nf = (d) => new Intl.NumberFormat("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
const cache = {};

export function num(v, d = 2) {
  if (v === null || v === undefined || v === "" || Number.isNaN(Number(v))) return "–";
  return (cache[d] ||= nf(d)).format(Number(v));
}

export function inr(v, d = 2) {
  return v === null || v === undefined || Number.isNaN(Number(v)) ? "–" : `₹${num(v, d)}`;
}

export function signed(v, d = 2, suffix = "") {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "–";
  return `${Number(v) > 0 ? "+" : ""}${num(v, d)}${suffix}`;
}

export const tone = (v) => (Number(v) > 0 ? "up" : Number(v) < 0 ? "down" : "");
export const REGIME_TONE = { Uptrend: "up", Downtrend: "down", "Range-bound": "", Squeeze: "accent", Choppy: "amber" };

export function dateTime(iso, opts = { dateStyle: "medium", timeStyle: "short" }) {
  if (!iso) return "–";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleString("en-IN", { timeZone: "Asia/Kolkata", ...opts });
}

export const time = (iso) => dateTime(iso, { timeStyle: "medium" });

export function until(iso, now = Date.now()) {
  const ms = new Date(iso).getTime() - now;
  if (!(ms > 0)) return "expired";
  const h = Math.floor(ms / 3.6e6);
  const m = Math.floor((ms % 3.6e6) / 6e4);
  return h ? `${h}h ${m}m` : `${m}m`;
}

export function initials(name = "") {
  return name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("") || "?";
}

const SECTOR_SHORT = {
  "Automobile and Auto Components": "Auto",
  "Capital Goods": "Capital Goods",
  "Construction Materials": "Cement & Materials",
  "Consumer Durables": "Cons. Durables",
  "Consumer Services": "Cons. Services",
  "Fast Moving Consumer Goods": "FMCG",
  "Financial Services": "Financials",
  "Information Technology": "IT",
  "Metals & Mining": "Metals",
  "Oil Gas & Consumable Fuels": "Oil & Gas",
  Telecommunication: "Telecom",
};
export const shortSector = (s) => SECTOR_SHORT[s] || s;

export const roundTick = (v, tick = 0.05) => Number((Math.round(Number(v) / tick) * tick).toFixed(2));

// Signed rupee amount with the sign before the symbol: +₹1,250 / −₹380 (0 decimals by default).
export function rupee(v, d = 0) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "–";
  const n = Number(v);
  return `${n > 0 ? "+" : n < 0 ? "−" : ""}₹${num(Math.abs(n), d)}`;
}
