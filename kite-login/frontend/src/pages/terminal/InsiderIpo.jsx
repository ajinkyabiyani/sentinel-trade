import { useState } from "react";
import Insiders from "./Insiders.jsx";
import IpoAgent from "../IpoAgent.jsx";

// Insider and IPO: two kinds of "who is putting money in" on one page. The IPO Agent rates upcoming issues; Insider trades lists
// bulk, block and short deals and insider filings. The chosen tab is remembered on this device.
const KEY = "kd.insiderIpo.tab";
const TABS = [
  { id: "ipo", label: "IPO Agent", hint: "Upcoming and open IPOs, each with a signal, the odds from past issues like it, and the checks behind it." },
  { id: "insider", label: "Insider trades", hint: "Bulk, block and short deals, and trades by promoters, directors and key staff, as reported to NSE." },
];
const read = () => { try { return localStorage.getItem(KEY) || "ipo"; } catch { return "ipo"; } };

export default function InsiderIpo({ openStock }) {
  const [tab, setTabState] = useState(read);
  const setTab = (t) => { setTabState(t); try { localStorage.setItem(KEY, t); } catch { /* private window */ } };
  const cur = TABS.find((t) => t.id === tab) || TABS[0];
  return (
    <div className="tt-page">
      <nav className="es-tabs" aria-label="Insider and IPO sections">
        {TABS.map((t) => <button key={t.id} type="button" className={tab === t.id ? "on" : ""} aria-current={tab === t.id ? "page" : undefined} onClick={() => setTab(t.id)}>{t.label}</button>)}
      </nav>
      <p className="es-note" style={{ margin: "-4px 2px 0" }}>{cur.hint}</p>
      {cur.id === "ipo" ? <IpoAgent /> : <Insiders openStock={openStock} />}
    </div>
  );
}
