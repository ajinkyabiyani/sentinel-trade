// Which way a trade wins. An option is always bought here, so its contract type says the view:
// a call wins on a rise (bullish), a put wins on a fall (bearish). Shares say LONG or SHORT.
const CONTRACT = /(CE|PE)$/;

export function sideOf(symbol = "", side = "LONG") {
  const m = String(symbol).match(CONTRACT);
  if (m) return m[1] === "PE" ? { label: "PUT · bearish", cls: "short", title: "Bought put: profits when the underlying falls" } : { label: "CALL · bullish", cls: "long", title: "Bought call: profits when the underlying rises" };
  return side === "SHORT" ? { label: "SHORT", cls: "short", title: "Sold shares: profits when the price falls" } : { label: "LONG", cls: "long", title: "Bought shares: profits when the price rises" };
}

export default function SideTag({ symbol, side }) {
  const s = sideOf(symbol, side);
  return <span className={`dir ${s.cls}`} title={s.title}>{s.label}</span>;
}
