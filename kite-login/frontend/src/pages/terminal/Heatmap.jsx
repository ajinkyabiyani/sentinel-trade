import { useEffect, useState } from "react";
import { ErrorNote, Segmented, Spinner } from "../../components/ui.jsx";
import Regime from "../Regime.jsx";
import Rotation from "../Rotation.jsx";
import { HeatLegend, LoadNote, PERIODS, Treemap, periodOptions, useStocks } from "./shared.jsx";

// Market map: the Nifty 500 heatmap, with sector rotation and the market regime read beside it.
const VIEWS = [{ id: "heatmap", label: "Heatmap" }, { id: "rotation", label: "Sector rotation" }, { id: "regime", label: "Market regime" }];

function StockMap({ openStock }) {
  const { rows, live, meta, loading, error } = useStocks();
  const [period, setPeriod] = useState("1D");
  const byCap = rows.filter((r) => r.mcap_cr).length >= rows.length * 0.9;
  return (
    <section className="card tt-card">
      <header className="card-head">
        <div className="row">
          <h3>Nifty 500 heatmap</h3>
          {live && <span className="live-chip"><span className="live-dot" /> Live</span>}
        </div>
        <Segmented size="small" label="Period" value={period} onChange={setPeriod} options={periodOptions(PERIODS)} />
      </header>
      {loading ? <div className="chart-empty" style={{ height: 620 }}><Spinner /></div> : <Treemap rows={rows} period={period} onPick={openStock} height={Math.max(560, Math.round(window.innerHeight - 260))} />}
      <footer className="tt-foot">
        <span className="muted small">{period === "1D" ? "Change from the previous close" : `Return over ${period}`}. Box size is {byCap ? "market cap" : "value traded today"}. Click a box to open the stock.</span>
        <HeatLegend />
      </footer>
      <div className="tt-load"><LoadNote meta={meta} /></div>
      <ErrorNote error={error} />
    </section>
  );
}

export default function Heatmap({ openStock, openTicket, scanVersion }) {
  // links to the old Sector Rotation and Market Regime pages land on the matching view
  const fromHash = () => ({ "#rotation": "rotation", "#regime": "regime" })[window.location.hash];
  const [view, setView] = useState(() => fromHash() || "heatmap");
  useEffect(() => {
    const onHash = () => { const v = fromHash(); if (v) setView(v); };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  return (
    <div className="tt-page">
      <Segmented value={view} onChange={setView} options={VIEWS} label="Market view" />
      {view === "heatmap" && <StockMap openStock={openStock} />}
      {view === "rotation" && <Rotation openTicket={openTicket} scanVersion={scanVersion} openStock={openStock} />}
      {view === "regime" && <Regime openStock={openStock} />}
    </div>
  );
}
