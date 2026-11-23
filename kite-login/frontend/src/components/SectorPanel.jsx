import { useMemo, useState } from "react";
import { api } from "../lib/api.js";
import { signed, tone } from "../lib/format.js";
import { Segmented, Spinner, useLoad } from "./ui.jsx";
import { Logo } from "./Logo.jsx";

// Sector performance, made to be read and explored: each sector's return for the chosen period on a bar centred at zero, how many
// of its own stocks are rising right now, its RSI momentum and rotation quadrant. Click a sector for its return across every
// period and its biggest movers today. Index returns move on live ticks; stock breadth on the tracked index stocks.
const PERIODS = ["1D", "1W", "1M", "3M", "YTD", "1Y"];
const SORTS = [{ id: "ret", label: "Return" }, { id: "rsi", label: "Momentum" }, { id: "breadth", label: "Breadth" }];
const QUAD = { Leading: "lead", Weakening: "weak", Improving: "impr", Lagging: "lag" };

function liveRet(s, period, ticks) {
  const px = ticks[s.key]?.last_price;
  let v = s.ret?.[period];
  if (px && v != null && s.last) v = (1 + v / 100) * (px / s.last) * 100 - 100;
  return v;
}

export default function SectorPanel({ sectors, ticks = {}, movers, stockTicks = {}, openStock, go }) {
  const [period, setPeriod] = useState("1D");
  const [sort, setSort] = useState("ret");
  const [open, setOpen] = useState(null);
  const rot = useLoad(() => api.terminalRotation("sectors"), [], 300_000);
  const rotBy = useMemo(() => Object.fromEntries((rot.data?.rows || []).map((r) => [r.key, r])), [rot.data]);
  const uni = movers?.universe || {};
  const groups = movers?.groups || {};
  const chg = (sym) => {
    const u = uni[sym];
    if (!u) return null;
    const px = stockTicks[`NSE:${sym}`]?.last_price ?? u[1];
    return (px / u[0] - 1) * 100;
  };

  const rows = useMemo(() => (sectors || []).map((s) => {
    const v = liveRet(s, period, ticks);
    const stocks = (groups[s.label] || []).map((sym) => ({ sym, c: chg(sym) })).filter((x) => x.c != null);
    const up = stocks.filter((x) => x.c > 0).length;
    const down = stocks.filter((x) => x.c < 0).length;
    return { ...s, v, stocks, up, down, rot: rotBy[s.key] };
  }).filter((s) => s.v != null), [sectors, ticks, period, groups, uni, stockTicks, rotBy]); // eslint-disable-line react-hooks/exhaustive-deps

  const sorted = [...rows].sort((a, b) => (sort === "rsi" ? (b.rot?.rsi ?? -1) - (a.rot?.rsi ?? -1)
    : sort === "breadth" ? (b.up / Math.max(1, b.up + b.down)) - (a.up / Math.max(1, a.up + a.down)) : b.v - a.v));
  const max = Math.max(0.01, ...rows.map((s) => Math.abs(s.v)));
  const upCount = rows.filter((s) => s.v > 0).length;
  const best = [...rows].sort((a, b) => b.v - a.v)[0];
  const worst = [...rows].sort((a, b) => a.v - b.v)[0];
  const periodMax = Object.fromEntries(PERIODS.map((p) => [p, Math.max(0.01, ...rows.map((s) => Math.abs(liveRet(s, p, ticks) ?? 0)))]));

  return (
    <section className="card sp">
      <header className="sp-head">
        <div>
          <h3>Sector performance</h3>
          {rows.length > 0 && (
            <span className="sp-sum">
              <b className={upCount >= rows.length / 2 ? "up" : "down"}>{upCount} of {rows.length}</b> up · best <b>{best?.label}</b> <span className={tone(best?.v)}>{signed(best?.v)}%</span> · worst <b>{worst?.label}</b> <span className={tone(worst?.v)}>{signed(worst?.v)}%</span>
            </span>
          )}
        </div>
        <div className="sp-ctrl">
          <Segmented size="small" label="Period" value={period} onChange={setPeriod} options={PERIODS.map((p) => ({ id: p, label: p }))} />
          <Segmented size="small" label="Sort by" value={sort} onChange={setSort} options={SORTS} />
        </div>
      </header>
      {!sectors ? <div className="sp-empty"><Spinner /></div> : !rows.length ? <p className="sp-empty muted small">Period returns appear once the daily candles have loaded.</p> : (
        <>
          <div className="sp-cols" aria-hidden="true"><span>Sector</span><span className="c">{period} return</span><span>Stocks up / down now</span><span>RSI · rotation</span></div>
          <ul className="sp-list">
            {sorted.map((s) => {
              const isOpen = open === s.key;
              const share = s.up + s.down ? (s.up / (s.up + s.down)) * 100 : null;
              const best3 = [...s.stocks].sort((a, b) => b.c - a.c).slice(0, 3);
              const worst3 = [...s.stocks].sort((a, b) => a.c - b.c).slice(0, 3);
              const q = s.rot?.quadrant;
              return (
                <li key={s.key} className={isOpen ? "open" : ""}>
                  <button type="button" className="sp-row" aria-expanded={isOpen} onClick={() => setOpen(isOpen ? null : s.key)}>
                    <span className="sp-name"><i className="sp-chev" aria-hidden="true" />{s.label}</span>
                    <span className="sp-bar">
                      <span className="sp-track"><span className={`sp-fill ${s.v >= 0 ? "pos" : "neg"}`} style={{ width: `${(Math.abs(s.v) / max) * 50}%` }} /></span>
                      <b className={tone(s.v)}>{signed(s.v)}%</b>
                    </span>
                    <span className="sp-breadth" title={`${s.up} rising, ${s.down} falling of ${s.stocks.length} tracked stocks`}>
                      {share == null ? <span className="muted small">no tracked stocks</span> : <>
                        <span className="sp-ad"><span className="up" style={{ width: `${share}%` }} /><span className="down" style={{ width: `${100 - share}%` }} /></span>
                        <small><span className="up">{s.up}</span> / <span className="down">{s.down}</span></small>
                      </>}
                    </span>
                    <span className="sp-rsi">
                      {s.rot ? <>
                        <span className="sp-rsi-gauge" title={`RSI ${s.rot.rsi} (${s.rot.zone || "neutral"})`}><span style={{ left: `${Math.min(100, Math.max(0, s.rot.rsi))}%` }} /></span>
                        <b>{Math.round(s.rot.rsi)}</b>
                        {q && <em className={`sp-q ${QUAD[q]}`}>{q}</em>}
                      </> : <span className="muted small">–</span>}
                    </span>
                  </button>
                  {isOpen && (
                    <div className="sp-detail">
                      <div className="sp-periods">
                        {PERIODS.map((p) => {
                          const v = liveRet(s, p, ticks);
                          const a = v == null ? 0 : Math.min(1, Math.abs(v) / periodMax[p]);
                          return (
                            <button type="button" key={p} className={`sp-cell ${v == null ? "" : v >= 0 ? "pos" : "neg"}${p === period ? " on" : ""}`} style={{ "--a": `${Math.round(12 + a * 38)}%` }} onClick={() => setPeriod(p)}>
                              <small>{p}</small><b>{v == null ? "–" : `${signed(v, 1)}%`}</b>
                            </button>
                          );
                        })}
                      </div>
                      {s.stocks.length > 0 && (
                        <div className="sp-movers">
                          <div><span className="sp-k">Leading today</span>{best3.map((x) => <Mover key={x.sym} x={x} openStock={openStock} />)}</div>
                          <div><span className="sp-k">Lagging today</span>{worst3.map((x) => <Mover key={x.sym} x={x} openStock={openStock} />)}</div>
                        </div>
                      )}
                      {s.rot && <p className="sp-note">RSI {s.rot.rsi} ({s.rot.rsi_change >= 0 ? "+" : ""}{s.rot.rsi_change} in a day) · {s.rot.quadrant}{s.rot.zone ? ` · ${s.rot.zone}` : ""} · 1 week {signed(s.rot.ret_1w, 1)}%, 1 month {signed(s.rot.ret_1m, 1)}%{go && <> · <button type="button" className="link-accent" onClick={() => go("rotation")}>Sector Rotation →</button></>}</p>}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        </>
      )}
    </section>
  );
}

function Mover({ x, openStock }) {
  return (
    <button type="button" className="sp-mover" onClick={() => openStock?.(x.sym)}>
      <Logo symbol={x.sym} size={18} /><b>{x.sym}</b><span className={tone(x.c)}>{signed(x.c)}%</span>
    </button>
  );
}
