// Zerodha Kite order vocabulary: what each product / variety / order type / validity means and where it is allowed.
export const EQUITY = ["NSE", "BSE"];
export const DERIVATIVES = ["NFO", "BFO", "CDS", "BCD", "MCX"];

export const EXCHANGES = [
  { id: "NSE", label: "NSE", desc: "NSE equity" },
  { id: "BSE", label: "BSE", desc: "BSE equity" },
  { id: "NFO", label: "NFO", desc: "NSE futures & options" },
  { id: "BFO", label: "BFO", desc: "BSE futures & options (Sensex, Bankex)" },
  { id: "CDS", label: "CDS", desc: "NSE currency derivatives" },
  { id: "BCD", label: "BCD", desc: "BSE currency derivatives" },
  { id: "MCX", label: "MCX", desc: "Commodity futures & options" },
];

export const PRODUCTS = [
  {
    id: "CNC",
    label: "CNC",
    name: "Delivery",
    desc: "Cash and Carry: buy equity with full cash and hold it in your demat for as long as you like. No leverage.",
    exchanges: EQUITY,
  },
  {
    id: "MIS",
    label: "MIS",
    name: "Intraday",
    desc: "Margin Intraday Squareoff: leveraged intraday trades on any segment. Open positions are auto-squared off before the close (about 3:20 PM for equity).",
    exchanges: [...EQUITY, ...DERIVATIVES],
  },
  {
    id: "NRML",
    label: "NRML",
    name: "Overnight F&O",
    desc: "Normal: carry futures, options, currency and commodity positions overnight until expiry, with full SPAN + exposure margin.",
    exchanges: DERIVATIVES,
  },
  {
    id: "MTF",
    label: "MTF",
    name: "Margin Trading",
    desc: "Margin Trading Facility: buy approved stocks with funded leverage and hold them overnight. Interest is charged daily on the funded amount.",
    exchanges: EQUITY,
  },
];

export const VARIETIES = [
  { id: "regular", label: "Regular", desc: "Normal order during market hours." },
  { id: "amo", label: "AMO", desc: "After-market order: queued now, sent to the exchange when the market opens." },
  { id: "co", label: "Cover", desc: "Cover order: MIS entry with a compulsory stop-loss leg (trigger price)." },
  { id: "iceberg", label: "Iceberg", desc: "Splits a large LIMIT/SL order into 2-50 smaller legs to reduce impact." },
  { id: "auction", label: "Auction", desc: "Bid in an exchange short-delivery auction (CNC, LIMIT, auction number)." },
];

export const ORDER_TYPES = [
  { id: "MARKET", label: "Market", desc: "Execute at the best available price." },
  { id: "LIMIT", label: "Limit", desc: "Execute at your price or better." },
  { id: "SL", label: "SL", desc: "Stop-loss limit: becomes a LIMIT order at your price once the trigger is hit." },
  { id: "SL-M", label: "SL-M", desc: "Stop-loss market: becomes a MARKET order once the trigger is hit." },
];

export const VALIDITIES = [
  { id: "DAY", label: "Day", desc: "Valid until the end of the trading day." },
  { id: "IOC", label: "IOC", desc: "Immediate or cancel: whatever doesn't fill at once is cancelled." },
  { id: "TTL", label: "Minutes", desc: "Valid for a number of minutes, then cancelled." },
];

export const productById = Object.fromEntries(PRODUCTS.map((p) => [p.id, p]));

// Products allowed on this exchange. Kite's profile "products" list is advisory only: it leaves out MTF even
// when MTF is activated, so an unlisted product stays selectable and Kite itself has the final say.
export function productsFor(exchange, listed) {
  return PRODUCTS.map((p) => {
    const reason = p.exchanges.includes(exchange) ? "" : `Not available on ${exchange}`;
    const note = listed?.length && !listed.includes(p.id) ? "not listed on profile" : "";
    return { ...p, disabled: Boolean(reason), reason, note };
  });
}

export function orderTypesFor(variety) {
  if (variety === "co") return ["MARKET", "LIMIT"];
  if (variety === "iceberg") return ["LIMIT", "SL"];
  if (variety === "auction") return ["LIMIT"];
  return ORDER_TYPES.map((o) => o.id);
}

// Which products a position can be converted to (Kite's convert position rules).
export function convertTargets(exchange, product) {
  const pool = EQUITY.includes(exchange) ? ["CNC", "MIS", "MTF"] : ["NRML", "MIS"];
  return pool.filter((p) => p !== product);
}
