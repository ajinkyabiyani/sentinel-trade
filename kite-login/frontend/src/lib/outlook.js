// Chain outlook: what the option chain says about direction, and whether resistance / support should break.
// A desk-style read, not a forecast: OI build-ups near the money, change in PCR, price momentum vs a normal day,
// futures build-up, max-pain pull near expiry and IV skew are each scored -1..+1 and blended; the odds of reaching
// a level come from the market's own IV (risk-neutral). Recomputed on every tick.
import { ncdf } from "./options.js";

const clamp = (x, a = -1, b = 1) => Math.max(a, Math.min(b, x));

// Who is driving a contract today, from its OI change and its premium change vs the previous session. The premium
// change is the IV change when known: rising IV means buyers are paying up, falling IV means writers are supplying,
// free of time decay and of the spot move. Otherwise the raw price change is used.
export function buildup(dOi, premChg) {
  if (dOi === null || dOi === undefined || premChg === null || premChg === undefined || dOi === 0) return null;
  if (dOi > 0) return premChg >= 0 ? "LB" : "SB"; // long build-up / short build-up (writing)
  return premChg >= 0 ? "SC" : "LU"; // short covering / long unwinding
}
export const premiumChange = (l) => (l?.d_iv !== null && l?.d_iv !== undefined ? l.d_iv : l?.change_pct ?? null);
export const BUILDUP_TEXT = { LB: "Long build-up", SB: "Short build-up (writing)", SC: "Short covering", LU: "Long unwinding" };
// what each build-up implies for the underlying, by option type
const BIAS = { ce: { LB: 1, SB: -1, SC: 1, LU: -0.5 }, pe: { LB: -1, SB: 1, SC: -1, LU: 0.5 } };

function probClose(fwd, k, ivPct, t, above) {
  const s = (ivPct || 0) / 100;
  if (!(s > 0) || !(t > 0)) return null;
  const d2 = (Math.log(fwd / k) - 0.5 * s * s * t) / (s * Math.sqrt(t));
  return above ? ncdf(d2) : ncdf(-d2);
}

export const fmtK = (v) => {
  const a = Math.abs(v);
  return a >= 1e7 ? `${(v / 1e7).toFixed(2)}Cr` : a >= 1e5 ? `${(v / 1e5).toFixed(1)}L` : a >= 1e3 ? `${(v / 1e3).toFixed(1)}K` : `${Math.round(v)}`;
};
const fmtS = (k) => Number(k).toLocaleString("en-IN", { maximumFractionDigits: 1 });
const sgn = (v, d = 2) => `${v >= 0 ? "+" : ""}${Number(v).toFixed(d)}`;

export function optionOutlook(lc, chain, futTick) {
  if (!lc || !chain) return null;
  const { spot, t, rows } = lc;
  const iv = lc.atmIv || 15;
  const dailyPct = iv / Math.sqrt(252);
  const band = Math.max(spot * 0.03, 1.5 * (lc.straddle || 0));
  const legs = [];
  for (const r of rows) {
    if (Math.abs(r.strike - spot) > band) continue;
    for (const side of ["ce", "pe"]) {
      const l = r[side];
      if (!l || l.prev_oi === null || l.prev_oi === undefined) continue;
      const d = (l.oi || 0) - l.prev_oi;
      const b = buildup(d, premiumChange(l));
      if (!b) continue;
      const w = Math.abs(d) * (1 - (Math.abs(r.strike - spot) / band) * 0.5);
      legs.push({ strike: r.strike, side, d, b, w, bias: BIAS[side][b], dIv: l.d_iv ?? null });
    }
  }
  const haveFlow = legs.length >= 4;
  const factors = [];

  // 1. OI flow near the money
  if (haveFlow) {
    const bull = legs.filter((x) => x.bias > 0).reduce((a, x) => a + x.w * x.bias, 0);
    const bear = legs.filter((x) => x.bias < 0).reduce((a, x) => a - x.w * x.bias, 0);
    const score = bull + bear ? (bull - bear) / (bull + bear) : 0;
    const top = (sign) => legs.filter((x) => Math.sign(x.bias) === sign).sort((a, b) => b.w - a.w).slice(0, 2)
      .map((x) => `${x.side === "ce" ? "call" : "put"} ${BUILDUP_TEXT[x.b].toLowerCase()} at ${fmtS(x.strike)} (OI ${x.d > 0 ? "+" : ""}${fmtK(x.d)}${x.dIv !== null ? `, IV ${sgn(x.dIv, 1)}` : ""})`);
    factors.push({ key: "flow", label: "OI flow near the money", weight: 0.35, score,
      text: score >= 0 ? `Bullish flow leads: ${top(1).join(", ") || "–"}` : `Bearish flow leads: ${top(-1).join(", ") || "–"}` });
    // 2. where fresh OI went today: puts vs calls
    const putAdd = legs.filter((x) => x.side === "pe").reduce((a, x) => a + x.d, 0);
    const callAdd = legs.filter((x) => x.side === "ce").reduce((a, x) => a + x.d, 0);
    const den = Math.abs(putAdd) + Math.abs(callAdd);
    factors.push({ key: "pcr", label: "Change in PCR", weight: 0.2, score: den ? clamp((putAdd - callAdd) / den) : 0,
      text: `Today near the money: puts ${putAdd >= 0 ? "+" : ""}${fmtK(putAdd)} vs calls ${callAdd >= 0 ? "+" : ""}${fmtK(callAdd)} OI · PCR ${lc.pcr ? lc.pcr.toFixed(2) : "–"}` });
  }
  // 3. price momentum relative to a normal day
  if (lc.spotChange !== null && lc.spotChange !== undefined) {
    factors.push({ key: "mom", label: "Price momentum", weight: 0.2, score: clamp(lc.spotChange / dailyPct),
      text: `Spot ${sgn(lc.spotChange)}% today, ${(Math.abs(lc.spotChange) / dailyPct).toFixed(1)}× a normal day's ±${dailyPct.toFixed(2)}%` });
  }
  // 4. futures build-up (near-month)
  const f = chain.future_detail;
  if (f?.prev_oi && (futTick?.oi || f.oi)) {
    const oi = futTick?.oi || f.oi;
    const px = futTick?.change_pct ?? f.change_pct ?? 0;
    const oiPct = ((oi - f.prev_oi) / f.prev_oi) * 100;
    const b = buildup(oi - f.prev_oi, px);
    if (b) {
      const base = { LB: 1, SB: -1, SC: 0.6, LU: -0.6 }[b];
      factors.push({ key: "fut", label: "Futures build-up", weight: 0.15, score: base * clamp(Math.abs(oiPct) / 3 + 0.3, 0, 1),
        text: `${f.tradingsymbol}: ${BUILDUP_TEXT[b].toLowerCase()} (price ${sgn(px)}%, OI ${sgn(oiPct, 1)}%)` });
    }
  }
  // 5. max-pain pull in the last days before expiry
  if (lc.maxPain && lc.daysToExpiry <= 4 && lc.straddle) {
    factors.push({ key: "mp", label: "Max-pain pull", weight: 0.05, score: clamp((lc.maxPain - spot) / lc.straddle),
      text: `${lc.daysToExpiry.toFixed(1)} days to expiry: max pain ${fmtS(lc.maxPain)} ${lc.maxPain >= spot ? "above" : "below"} spot tends to pull price` });
  }
  // 6. IV skew: OTM put IV minus OTM call IV about 3% away
  const pick = (target, side) => rows.reduce((b, r) => (r[side]?.iv && Math.abs(r.strike - target) < Math.abs((b?.strike ?? 1e12) - target) ? r : b), null);
  const pr = pick(spot * 0.97, "pe");
  const cr = pick(spot * 1.03, "ce");
  if (pr?.pe?.iv && cr?.ce?.iv) {
    const skew = pr.pe.iv - cr.ce.iv;
    factors.push({ key: "skew", label: "IV skew", weight: 0.05, score: clamp(-(skew - 2) / 4),
      text: `OTM puts ${skew >= 0 ? `${skew.toFixed(1)} vol pts dearer` : `${(-skew).toFixed(1)} vol pts cheaper`} than OTM calls (${skew > 5 ? "hedging demand / fear" : skew < 0 ? "upside chase" : "normal"})` });
  }

  const wsum = factors.reduce((a, x) => a + x.weight, 0) || 1;
  const score = factors.reduce((a, x) => a + x.weight * x.score, 0) / wsum;
  const label = score > 0.45 ? "Strong bullish" : score > 0.15 ? "Bullish" : score < -0.45 ? "Strong bearish" : score < -0.15 ? "Bearish" : "Range-bound";
  const tone = score > 0.15 ? "up" : score < -0.15 ? "down" : "amber";

  // walls: are writers adding (strengthening) or leaving (weakening)?
  const byK = Object.fromEntries(rows.map((r) => [r.strike, r]));
  // a wall strengthens when writers add (short build-up), weakens when they cover, and is under attack when option
  // buyers pile in (long build-up); buyers leaving (long unwinding) eases the pressure on it
  const WALL_STATE = { SB: "strengthening", SC: "weakening", LB: "attacked", LU: "easing" };
  const wallInfo = (k, side) => {
    const l = byK[k]?.[side];
    const d = l && l.prev_oi !== null && l.prev_oi !== undefined ? (l.oi || 0) - l.prev_oi : null;
    const b = d === null ? null : buildup(d, premiumChange(l));
    return { strike: k, oi: l?.oi ?? null, dOi: d, dIv: l?.d_iv ?? null, iv: l?.iv ?? null, buildup: b,
      state: d === null ? "unknown" : b ? WALL_STATE[b] : "unchanged" };
  };
  const reach = Math.max(2 * (lc.straddle || 0), spot * 0.04);
  const nextWall = (from, side, dir) => rows
    .filter((r) => (dir > 0 ? r.strike > from && r.strike <= from + reach : r.strike < from && r.strike >= from - reach))
    .reduce((b, r) => ((r[side]?.oi || 0) > (b?.[side]?.oi || 0) ? r : b), null)?.strike ?? null;
  const freshWall = (side, dir) => legs.filter((x) => x.side === side && x.b === "SB" && (dir > 0 ? x.strike >= spot : x.strike <= spot))
    .sort((a, b) => b.d - a.d)[0] || null;

  const verdictFor = (wall, isRes) => {
    if (!wall.strike) return null;
    const pClose = probClose(lc.forward, wall.strike, wall.iv || iv, t, isRes);
    const pTouch = pClose === null ? null : Math.min(0.99, 2 * pClose);
    const adj = { weakening: 0.3, attacked: 0.2, strengthening: -0.3, easing: -0.1 }[wall.state] ?? 0;
    const s = (isRes ? score : -score) + adj + ((pTouch ?? 0.5) - 0.5) * 0.6;
    return { ...wall, pClose, pTouch, verdict: s > 0.35 ? "Likely to break" : s < -0.1 ? "Likely to hold" : "Contested",
      tone: s > 0.35 ? (isRes ? "up" : "down") : s < -0.1 ? "amber" : "", next: nextWall(wall.strike, isRes ? "ce" : "pe", isRes ? 1 : -1) };
  };
  const resistance = verdictFor(wallInfo(lc.resistance, "ce"), true);
  const support = verdictFor(wallInfo(lc.support, "pe"), false);
  const fc = freshWall("ce", 1);
  const fp = freshWall("pe", -1);

  const sd = (iv / 100) * Math.sqrt(t);
  const range = { low: lc.forward - lc.straddle, high: lc.forward + lc.straddle, sdLow: lc.forward * Math.exp(-sd), sdHigh: lc.forward * Math.exp(sd),
    todayMove: spot * (iv / 100) * Math.sqrt(1 / 252) };

  const rb = resistance?.verdict === "Likely to break";
  const sb = support?.verdict === "Likely to break";
  let headline;
  const pc = (w) => `${Math.round((w?.pTouch ?? 0) * 100)}%`;
  if (rb && !sb) {
    headline = score > 0.15
      ? `Bias up: ${fmtS(lc.resistance)} resistance likely to give way${resistance.next ? `, next call wall ${fmtS(resistance.next)}` : ""}.`
      : `Neutral overall, but ${fmtS(lc.resistance)} resistance is under pressure (${pc(resistance)} odds of a touch)${resistance.next ? `; above it the next call wall is ${fmtS(resistance.next)}` : ""}.`;
  } else if (sb && !rb) {
    headline = score < -0.15
      ? `Bias down: ${fmtS(lc.support)} support likely to give way${support.next ? `, next put wall ${fmtS(support.next)}` : ""}.`
      : `Neutral overall, but ${fmtS(lc.support)} support is under pressure (${pc(support)} odds of a touch)${support.next ? `; below it the next put wall is ${fmtS(support.next)}` : ""}.`;
  }
  else if (resistance?.verdict === "Likely to hold" && support?.verdict === "Likely to hold") headline = `Range-bound: writers defend ${fmtS(lc.support)}–${fmtS(lc.resistance)} into expiry.`;
  else if (score > 0.15) headline = `${label} pressure: buyers lean on ${fmtS(lc.resistance)} (touch odds ${Math.round((resistance?.pTouch ?? 0) * 100)}%), but call writers are not giving way yet.`;
  else if (score < -0.15) headline = `${label} pressure: sellers lean on ${fmtS(lc.support)} (touch odds ${Math.round((support?.pTouch ?? 0) * 100)}%), but put writers are not giving way yet.`;
  else headline = `No decisive edge: ${fmtS(lc.support)} support and ${fmtS(lc.resistance)} resistance decide the next move.`;

  // the three biggest OI walls each side within 10% of spot, nearest first: call writers above (R1-R3), put writers
  // below (S1-S3), each with today's OI change and whether its writers are adding or covering
  const walls = (side, dir) => rows
    .filter((r) => r[side]?.oi && (dir > 0 ? r.strike >= spot && r.strike <= spot * 1.1 : r.strike <= spot && r.strike >= spot * 0.9))
    .sort((a, b) => b[side].oi - a[side].oi).slice(0, 3)
    .sort((a, b) => dir * (a.strike - b.strike))
    .map((r) => wallInfo(r.strike, side));
  const levels = { resistance: walls("ce", 1), support: walls("pe", -1) };

  const watch = [
    resistance?.strike && `Break-out trigger: sustained trade above ${fmtS(resistance.strike)} with call OI there falling${resistance.next ? `, opens ${fmtS(resistance.next)}` : ""}.`,
    support?.strike && `Break-down trigger: trade below ${fmtS(support.strike)} with put OI there falling${support.next ? `, opens ${fmtS(support.next)}` : ""}.`,
    fc && fc.strike !== lc.resistance && `Fresh call writing at ${fmtS(fc.strike)} (+${fmtK(fc.d)}): a new ceiling may be forming.`,
    fp && fp.strike !== lc.support && `Fresh put writing at ${fmtS(fp.strike)} (+${fmtK(fp.d)}): a new floor may be forming.`,
  ].filter(Boolean);

  return { score, label, tone, headline, factors, resistance, support, levels, range, watch, haveFlow, flowLegs: legs.length };
}
