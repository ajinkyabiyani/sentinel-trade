import { useState } from "react";
import { api } from "../lib/api.js";
import { num, rupee, tone } from "../lib/format.js";
import { Badge, Empty, Modal, Spinner, useLoad } from "./ui.jsx";
import { WithLogo } from "./Logo.jsx";
import SideTag from "./SideTag.jsx";

// Which confidence levels actually won: every strategy's closed paper trades grouped by the confidence they were taken at.
// The summary lines come from the backend; the full log (every trade, open ones too) opens in a dialog and is also kept as a CSV.

function Cell({ b }) {
  if (!b || !b.closed) return <td className="num muted">–</td>;
  return (
    <td className="num" title={`${b.won} won, ${b.lost} lost · net ${b.net}`}>
      <b>{b.won}/{b.closed}</b> <span className={`cw-pct ${b.win_rate >= 50 ? "up" : "down"}`}>{num(b.win_rate, 0)}%</span>
    </td>
  );
}

function FullLog({ rows, onClose, openStock }) {
  const [who, setWho] = useState("All");
  const names = ["All", ...new Set(rows.map((r) => r.strategy))];
  const shown = who === "All" ? rows : rows.filter((r) => r.strategy === who);
  return (
    <Modal title="Every trade by confidence" wide onClose={onClose}>
      <div className="cw-filter">
        {names.map((n) => <button key={n} type="button" className={`scan-chip${who === n ? " on" : ""}`} onClick={() => setWho(n)}>{n}</button>)}
      </div>
      <div className="table-wrap cw-log">
        <table className="table">
          <thead><tr><th>Date</th><th>Strategy</th><th>Instrument</th><th className="num">Confidence</th><th>Result</th><th className="num">Net</th><th className="num">R</th></tr></thead>
          <tbody>
            {shown.map((r, i) => (
              <tr key={i}>
                <td className="small">{r.date}</td>
                <td className="small">{r.strategy}</td>
                <td><button type="button" className="linkish" onClick={() => { onClose(); openStock?.(r.instrument); }}><WithLogo symbol={r.instrument} size={18}><b>{r.instrument}</b></WithLogo></button> <SideTag symbol={r.instrument} /></td>
                <td className="num" title={r.source}>{r.confidence == null ? "–" : `${num(r.confidence, 0)}%`}</td>
                <td>{r.status === "open" ? <Badge tone="accent">Open</Badge> : <Badge tone={r.won ? "buy" : "sell"}>{r.won ? "Won" : "Lost"}{r.exit_reason ? ` · ${r.exit_reason}` : ""}</Badge>}</td>
                <td className={`num ${tone(r.net)}`}>{r.net == null ? "–" : rupee(r.net)}</td>
                <td className="num muted">{r.r == null ? "–" : num(r.r, 2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Modal>
  );
}

export default function ConfidenceWins({ openStock }) {
  const q = useLoad(api.confidence, [], 60_000);
  const [open, setOpen] = useState(false);
  const d = q.data;
  const bands = (d?.bands || []).filter((b) => b.band !== "Not stated" || b.closed);
  return (
    <section className="card cw-card">
      <header className="card-head">
        <div>
          <h3>Wins by confidence</h3>
          <span className="muted small">{d ? `${d.total.closed} closed trades across all strategies · ${d.total.won} won (${num(d.total.win_rate ?? 0, 0)}%) · ${rupee(d.total.net)}` : "every strategy's closed paper trades"}</span>
        </div>
        <button type="button" className="link-accent" onClick={() => setOpen(true)} disabled={!d?.rows?.length}>Full log →</button>
      </header>
      {q.loading && !d ? <Spinner /> : !d?.rows?.length ? <Empty>No trade has closed yet.</Empty> : (
        <>
          <div className="table-wrap">
            <table className="table cw-table">
              <thead>
                <tr>
                  <th>Confidence at entry</th>
                  {d.by_strategy.map((x) => <th key={x.strategy} className="num" title={`Confidence here is ${x.source.toLowerCase()}`}>{x.strategy}</th>)}
                  <th className="num">All · won/closed</th>
                  <th style={{ width: 140 }}>Win rate</th>
                  <th className="num">Net P&L</th>
                  <th className="num">Avg a trade</th>
                </tr>
              </thead>
              <tbody>
                {bands.map((b) => (
                  <tr key={b.band}>
                    <td><b>{b.band}</b></td>
                    {d.by_strategy.map((x) => <Cell key={x.strategy} b={x.bands.find((y) => y.band === b.band)} />)}
                    <td className="num"><b>{b.closed ? `${b.won}/${b.closed}` : "–"}</b></td>
                    <td>{b.closed ? <span className="cw-rate-cell"><span className="cw-track"><span className="cw-won" style={{ width: `${(b.won / b.closed) * 100}%` }} /><span className="cw-lost" style={{ width: `${(b.lost / b.closed) * 100}%` }} /></span><b>{num(b.win_rate, 0)}%</b></span> : <span className="muted">–</span>}</td>
                    <td className={`num ${tone(b.net)}`}>{b.closed ? rupee(b.net) : "–"}</td>
                    <td className={`num ${tone(b.net)}`}>{b.closed ? rupee(b.net / b.closed) : "–"}</td>
                  </tr>
                ))}
                <tr className="cw-total">
                  <td><b>All trades</b></td>
                  {d.by_strategy.map((x) => <Cell key={x.strategy} b={x.total} />)}
                  <td className="num"><b>{d.total.won}/{d.total.closed}</b></td>
                  <td><b>{d.total.win_rate == null ? "–" : `${num(d.total.win_rate, 0)}%`}</b></td>
                  <td className={`num ${tone(d.total.net)}`}><b>{rupee(d.total.net)}</b></td>
                  <td className={`num ${tone(d.total.net)}`}>{d.total.closed ? rupee(d.total.net / d.total.closed) : "–"}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <ul className="cw-lines">{d.lines.map((l) => <li key={l}>{l}</li>)}</ul>
        </>
      )}
      {open && d && <FullLog rows={d.rows} onClose={() => setOpen(false)} openStock={openStock} />}
    </section>
  );
}
