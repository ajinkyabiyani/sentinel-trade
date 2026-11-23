import { useEffect, useState } from "react";
import { ErrorNote, Segmented, Spinner } from "../../components/ui.jsx";
import Regime from "../Regime.jsx";
import { HeatLegend, LoadNote, PERIODS, Treemap, periodOptions, useStocks } from "./shared.jsx";

// Market map: the Nifty 500 heatmap, with the market regime read beside it.
const VIEWS = [{ id: "heatmap", label: "Heatmap" }, { id: "regime", label: "Market regime" }];

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
      {loading ? <div className="chart-empty" style={{ height: 620 }}><Spinner /></div> : <Treemap rows={rows} period={period} onPick={openStock} height={Math.max(1150, Math.round(window.innerHeight - 260))} />}
      <footer className="tt-foot">
        <span className="muted small">{period === "1D" ? "Change from the previous close" : `Return over ${period}`}. Bigger boxes are {byCap ? "bigger companies by market cap" : "more value traded today"}, with sizes compressed so every name stays readable. Click a box to open the stock.</span>
        <HeatLegend />
      </footer>
      <div className="tt-load"><LoadNote meta={meta} /></div>
      <ErrorNote error={error} />
    </section>
  );
}

export default function Heatmap({ openStock }) {
  // links to the old Market Regime page land on the matching view
  const fromHash = () => ({ "#regime": "regime" })[window.location.hash];
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
      {view === "regime" && <Regime openStock={openStock} />}
    </div>
  );
}
