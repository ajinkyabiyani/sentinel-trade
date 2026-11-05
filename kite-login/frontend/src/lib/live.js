// Intraday maths on live ticks: sector moves, the relative rotation graph with the current week's live point,
// and the live Darvas breakout watch. Mirrors darvas_algo.py (sector_rotation, build_plan) so live numbers line
// up with the end-of-day scan.

// A tick only counts as "today" if its exchange time is after the scan's last daily bar; otherwise its change is
// already inside the scan (e.g. weekends, or after the evening scan).
export const tickIsNew = (t, asof) => Boolean(t?.last_price && t.time && asof && t.time.slice(0, 10) > asof);

const mean = (xs) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null);

// Equal-weight sector move today and breadth, from live ticks of the members.
export function sectorMoves(groups, ticks, asof) {
  return groups
    .map((g) => {
      const members = g.series?.members || [];
      const ch = members.map((s) => ticks[`NSE:${s}`] || ticks[`BSE:${s}`]).filter((t) => tickIsNew(t, asof)).map((t) => t.change_pct);
      return { sector: g.sector, quadrant: g.quadrant, n: members.length, live: ch.length,
        chg: mean(ch), up: ch.filter((c) => c > 0).length, down: ch.filter((c) => c < 0).length };
    })
    .filter((g) => g.chg !== null)
    .sort((a, b) => b.chg - a.chg);
}

// pandas ewm(span, adjust=False) skipping leading NaNs
function ewm(xs, span) {
  const a = 2 / (span + 1);
  let prev = null;
  return xs.map((v) => {
    if (v === null || !Number.isFinite(v)) return prev === null ? null : prev;
    prev = prev === null ? v : a * v + (1 - a) * prev;
    return prev;
  });
}

// 100 + rolling z-score (window w, sample std) - same as darvas_algo.sector_rotation's z()
function zscore(xs, w) {
  return xs.map((_, i) => {
    if (i < w - 1) return null;
    const win = xs.slice(i - w + 1, i + 1);
    if (win.some((v) => v === null)) return null;
    const m = win.reduce((a, b) => a + b, 0) / w;
    const sd = Math.sqrt(win.reduce((a, b) => a + (b - m) ** 2, 0) / (w - 1));
    return sd > 0 ? 100 + (xs[i] - m) / sd : null;
  });
}

const fridayOf = (iso) => {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + ((5 - d.getUTCDay() + 7) % 7));
  return d.toISOString().slice(0, 10);
};

// Rotation (RS-Ratio / RS-Momentum, 8-week tails) from the weekly series, with this week's point moved to live
// prices when the market is trading. Returns groups shaped like the scan's (sector, quadrant, rs_*, tail), plus `live`.
export function liveRotation(groups, ticks, asof, benchKey = "NSE:NIFTY 50", weeks = 10, tail = 8) {
  const bench = ticks[benchKey];
  const benchLive = tickIsNew(bench, asof);
  const today = benchLive ? bench.time.slice(0, 10) : null;
  return groups.map((g) => {
    const ser = g.series;
    if (!ser?.s?.length) return { ...g, live: false };
    let dates = [...ser.dates];
    let s = [...ser.s];
    let b = [...ser.b];
    let live = false;
    if (benchLive) {
      const ch = (ser.members || []).map((x) => ticks[`NSE:${x}`] || ticks[`BSE:${x}`]).filter((t) => tickIsNew(t, asof)).map((t) => t.change_pct);
      if (ch.length) {
        const sl = s[s.length - 1] * (1 + mean(ch) / 100);
        const bl = b[b.length - 1] * (1 + bench.change_pct / 100);
        const wk = fridayOf(today);
        if (wk === dates[dates.length - 1]) {
          s[s.length - 1] = sl;
          b[b.length - 1] = bl;
        } else {
          dates = [...dates, wk];
          s = [...s, sl];
          b = [...b, bl];
        }
        live = true;
      }
    }
    const rs = ewm(s.map((v, i) => (100 * v) / b[i]), 3);
    const ratio = zscore(rs, weeks);
    const d = ratio.map((v, i) => (i && v !== null && ratio[i - 1] !== null ? v - ratio[i - 1] : null));
    const mom = zscore(ewm(d, 3), weeks);
    const pts = dates.map((dt, i) => ({ date: dt, rs_ratio: ratio[i], rs_momentum: mom[i] })).filter((p) => p.rs_ratio !== null && p.rs_momentum !== null).slice(-tail);
    if (!pts.length) return { ...g, live: false };
    const h = pts[pts.length - 1];
    const quadrant = h.rs_ratio >= 100 ? (h.rs_momentum >= 100 ? "Leading" : "Weakening") : h.rs_momentum >= 100 ? "Improving" : "Lagging";
    return { ...g, tail: pts.map((p) => ({ ...p, rs_ratio: +p.rs_ratio.toFixed(2), rs_momentum: +p.rs_momentum.toFixed(2) })),
      rs_ratio: +h.rs_ratio.toFixed(2), rs_momentum: +h.rs_momentum.toFixed(2), quadrant, live };
  });
}

// darvas_algo.tick_size: NSE slabs from 15 Apr 2025; BSE 0.01 under ₹100 else 0.05
const tickOf = (x, exch = "NSE") =>
  exch === "BSE" ? (x < 100 ? 0.01 : 0.05) : x < 250 ? 0.01 : x <= 1000 ? 0.05 : x <= 5000 ? 0.1 : x <= 10000 ? 0.5 : x <= 20000 ? 1 : 5;
const roundDown = (v, t) => +(Math.floor(v / t + 1e-9) * t).toFixed(2);
const roundNear = (v, t) => +(Math.round(v / t) * t).toFixed(2);

// Live Darvas watch for every active box: provisional breakouts / near trigger / breakdowns, each with the plan
// build_plan would make at the live price (stop = tighter of box bottom - buffer and k ATR, >= min ATR away).
export function liveWatch(boxes, ticks, asof, params, regime, niftyTick, nearPct = 1.5) {
  const p = params || {};
  const marketOk = niftyTick?.last_price && regime?.sma50 ? niftyTick.last_price > regime.sma50 : null;
  return boxes.map((bx) => {
    const t = ticks[`${bx.exchange}:${bx.symbol}`];
    const isLive = tickIsNew(t, asof);
    const px = isLive ? t.last_price : bx.last_close;
    const state = px >= bx.trigger ? "breakout" : px < bx.box_bottom ? "breakdown" : (bx.trigger / px - 1) * 100 <= nearPct ? "near" : "inside";
    const surge = p.volume_surge?.[bx.segment] ?? 1.5;
    const volRatio = isLive && t.volume && bx.vol_avg ? t.volume / bx.vol_avg : null;
    let plan = null;
    if (state === "breakout" && bx.atr > 0) {
      const tk = tickOf(px, bx.exchange);
      const boxStop = bx.box_bottom * (1 - (p.stop_buffer_pct ?? 0.5) / 100);
      const atrStop = px - (p.atr_stop_mult ?? 2.5) * bx.atr;
      let stop = Math.max(boxStop, atrStop);
      if (px - stop < (p.min_stop_atr ?? 1) * bx.atr) stop = px - (p.min_stop_atr ?? 1) * bx.atr;
      stop = roundDown(stop, tk);
      const rps = px - stop;
      plan = { entry: px, stop, rps, targets: (p.targets_r || [2, 4]).map((r) => roundNear(px + r * rps, tk)), risk_pct: (rps / px) * 100 };
    }
    const checks = {
      rs: bx.rs === null || bx.rs === undefined || p.min_rs_pct === null || p.min_rs_pct === undefined ? null : bx.rs >= p.min_rs_pct,
      volume: volRatio === null ? null : volRatio >= surge,
      market: marketOk,
      near_high: bx.near_52wh,
    };
    return { ...bx, px, chg: isLive ? t.change_pct : null, isLive, state, volRatio, surge, plan, checks,
      above_pct: (px / bx.box_top - 1) * 100, to_trigger_pct: (bx.trigger / px - 1) * 100 };
  });
}
