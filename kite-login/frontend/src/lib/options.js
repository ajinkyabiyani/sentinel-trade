// Live option-chain maths in the browser, mirroring backend/main.py (option_chain): Black-76 IV / delta on the
// put-call-parity forward, plus PCR, max pain, OI walls and the ATM straddle - recomputed on every tick.

// Abramowitz & Stegun 7.1.26 (|error| < 1.5e-7)
function erf(x) {
  const s = Math.sign(x);
  const a = Math.abs(x);
  const t = 1 / (1 + 0.3275911 * a);
  const y = 1 - ((((1.061405429 * t - 1.453152027) * t + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-a * a);
  return s * y;
}
export const ncdf = (x) => 0.5 * (1 + erf(x / Math.SQRT2));

export function b76(f, k, t, vol, call) {
  if (t <= 0 || vol <= 0) return call ? Math.max(0, f - k) : Math.max(0, k - f);
  const sd = vol * Math.sqrt(t);
  const d1 = (Math.log(f / k) + 0.5 * sd * sd) / sd;
  const d2 = d1 - sd;
  return call ? f * ncdf(d1) - k * ncdf(d2) : k * ncdf(-d2) - f * ncdf(-d1);
}

export function ivDelta(price, f, k, t, call) {
  if (!(price > 0) || !(t > 0) || !(f > 0)) return [null, null];
  const intrinsic = call ? Math.max(0, f - k) : Math.max(0, k - f);
  if (price <= intrinsic + 1e-9) return [null, intrinsic > 0 ? (call ? 1 : -1) : null];
  let lo = 1e-4;
  let hi = 5;
  for (let i = 0; i < 50; i++) {
    const mid = (lo + hi) / 2;
    if (b76(f, k, t, mid, call) > price) hi = mid;
    else lo = mid;
  }
  const vol = (lo + hi) / 2;
  if (vol >= 4.99) return [null, null];
  const d1 = (Math.log(f / k) + 0.5 * vol * vol * t) / (vol * Math.sqrt(t));
  return [vol * 100, call ? ncdf(d1) : ncdf(d1) - 1];
}

// Black-76 Greeks per unit: delta, gamma, theta per calendar day, vega per 1 IV point (vol as a fraction).
export function greeks(f, k, t, vol, call) {
  if (!(t > 0) || !(vol > 0) || !(f > 0)) return null;
  const sd = vol * Math.sqrt(t);
  const d1 = (Math.log(f / k) + 0.5 * sd * sd) / sd;
  const pdf = Math.exp(-0.5 * d1 * d1) / Math.sqrt(2 * Math.PI);
  return {
    delta: call ? ncdf(d1) : ncdf(d1) - 1,
    gamma: pdf / (f * sd),
    theta: -(f * pdf * vol) / (2 * Math.sqrt(t)) / 365.25,
    vega: (f * pdf * Math.sqrt(t)) / 100,
  };
}

const mid = (l) => (l?.bid && l?.ask && l.ask >= l.bid ? (l.bid + l.ask) / 2 : l?.ltp);

function mergeLeg(leg, t) {
  if (!leg) return null;
  if (!t?.last_price) return { ...leg, live: false };
  return { ...leg, ltp: t.last_price, change_pct: t.change_pct ?? leg.change_pct, oi: t.oi ?? leg.oi,
    volume: t.volume ?? leg.volume, bid: t.bid ?? leg.bid, ask: t.ask ?? leg.ask, live: true };
}

export function liveChain(chain, ticks, now = Date.now()) {
  if (!chain) return null;
  const spotTick = ticks[chain.spot_key];
  const spot = spotTick?.last_price || chain.spot;
  const rows = chain.rows.map((r) => ({ strike: r.strike, ce: mergeLeg(r.ce, ticks[r.ce?.key]), pe: mergeLeg(r.pe, ticks[r.pe?.key]) }));
  const near = [...rows].sort((a, b) => Math.abs(a.strike - spot) - Math.abs(b.strike - spot)).slice(0, 3);
  const parity = near.map((r) => (mid(r.ce) && mid(r.pe) ? r.strike + mid(r.ce) - mid(r.pe) : null)).filter((x) => x).sort((a, b) => a - b);
  const fwd = parity.length ? parity[Math.floor(parity.length / 2)] : chain.forward * (spot / chain.spot);
  const t = Math.max(new Date(chain.expiry_at).getTime() - now, 60_000) / (365.25 * 86400_000);
  // yesterday's IV from yesterday's closing option price, spot and time left: the IV change separates buyers paying up
  // from writers supplying without the bias of time decay (weekly options lose value daily) or of the spot move
  const spotPrev = chain.spot_prev_close;
  const fwdPrev = spotPrev ? fwd * (spotPrev / spot) : null;
  const today = new Date(now).toISOString().slice(0, 10);
  for (const r of rows) {
    for (const [side, call] of [["ce", true], ["pe", false]]) {
      const l = r[side];
      if (!l) continue;
      const [iv, delta] = ivDelta(mid(l), fwd, r.strike, t, call);
      l.iv = iv;
      l.delta = delta;
      const g = iv ? greeks(fwd, r.strike, t, iv / 100, call) : null;
      l.gamma = g?.gamma ?? null;
      l.theta = g?.theta ?? null;
      l.vega = g?.vega ?? null;
      if (fwdPrev && l.prev_close) {
        const gapDays = l.prev_date ? Math.max(1, Math.round((new Date(today) - new Date(l.prev_date)) / 86400_000)) : 1;
        const [ivPrev] = ivDelta(l.prev_close, fwdPrev, r.strike, t + gapDays / 365.25, call);
        l.prev_iv = ivPrev;
        l.d_iv = iv !== null && ivPrev !== null ? iv - ivPrev : null;
      }
    }
  }
  const ks = rows.map((r) => r.strike);
  const ceOi = rows.reduce((a, r) => a + (r.ce?.oi || 0), 0);
  const peOi = rows.reduce((a, r) => a + (r.pe?.oi || 0), 0);
  const pain = (px) => rows.reduce((a, r) => a + (r.ce?.oi || 0) * Math.max(0, px - r.strike) + (r.pe?.oi || 0) * Math.max(0, r.strike - px), 0);
  const maxPain = ceOi + peOi ? ks.reduce((best, k) => (pain(k) < pain(best) ? k : best), ks[0]) : null;
  const above = ks.filter((k) => k >= spot && k <= spot * 1.1);
  const below = ks.filter((k) => k <= spot && k >= spot * 0.9);
  const byK = Object.fromEntries(rows.map((r) => [r.strike, r]));
  const wall = (list, side) => (list.length ? list.reduce((b, k) => ((byK[k][side]?.oi || 0) > (byK[b][side]?.oi || 0) ? k : b), list[0]) : null);
  const atm = ks.reduce((b, k) => (Math.abs(k - fwd) < Math.abs(b - fwd) ? k : b), ks[0]);
  const a = byK[atm];
  const straddle = (a?.ce?.ltp || 0) + (a?.pe?.ltp || 0);
  const ivs = [a?.ce?.iv, a?.pe?.iv].filter((x) => x);
  return {
    rows, spot, spotChange: spotTick?.change_pct ?? null, forward: fwd, atm, t,
    pcr: ceOi ? peOi / ceOi : null, callOi: ceOi, putOi: peOi, maxPain,
    resistance: wall(above, "ce"), support: wall(below, "pe"), straddle,
    impliedMove: straddle && spot ? (straddle / spot) * 100 : null,
    atmIv: ivs.length ? ivs.reduce((x, y) => x + y, 0) / ivs.length : null,
    daysToExpiry: t * 365.25,
    live: Boolean(spotTick?.last_price) || rows.some((r) => r.ce?.live || r.pe?.live),
  };
}
