import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api.js";
import { WithLogo } from "./Logo.jsx";

// Watchlist kept in this browser (symbols only, no prices). Every component using the hook stays in sync.
const KEY = "kd.watchlist";
const DEFAULT = ["NSE:RELIANCE", "NSE:TCS", "NSE:INFY", "NSE:HDFCBANK", "NSE:ICICIBANK", "NSE:LT"];
const EVT = "kd-watchlist";

function read() {
  try {
    const v = JSON.parse(localStorage.getItem(KEY) || "null");
    return Array.isArray(v) ? v : DEFAULT;
  } catch {
    return DEFAULT;
  }
}

export function useWatchlist() {
  const [list, setList] = useState(read);
  useEffect(() => {
    const on = () => setList(read());
    window.addEventListener(EVT, on);
    return () => window.removeEventListener(EVT, on);
  }, []);
  const save = (next) => {
    try {
      localStorage.setItem(KEY, JSON.stringify(next));
    } catch {
      /* private window: lasts for this visit */
    }
    setList(next);
    window.dispatchEvent(new Event(EVT));
  };
  const key = (symbol, exchange = "NSE") => `${exchange}:${symbol}`;
  return {
    list,
    has: (symbol, exchange) => list.includes(key(symbol, exchange)),
    toggle: (symbol, exchange) => {
      const k = key(symbol, exchange);
      save(list.includes(k) ? list.filter((x) => x !== k) : [...list, k]);
    },
    remove: (k) => save(list.filter((x) => x !== k)),
  };
}

// Symbol search over Kite's instrument list (NSE cash by default); onPick(symbol, exchange).
export function SymbolSearch({ onPick, placeholder = "Search stocks, e.g. TCS, RELIANCE", exchange = "NSE", compact }) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState([]);
  const [open, setOpen] = useState(false);
  const [i, setI] = useState(0);
  const box = useRef(null);
  useEffect(() => {
    if (!q.trim()) {
      setHits([]);
      return undefined;
    }
    const t = setTimeout(() => {
      api.search(q.trim(), exchange)
        .then((r) => setHits(r.filter((x) => x.instrument_type === "EQ" || x.segment === "INDICES").slice(0, 8)))
        .catch(() => setHits([]));
    }, 180);
    return () => clearTimeout(t);
  }, [q, exchange]);
  useEffect(() => {
    const close = (e) => !box.current?.contains(e.target) && setOpen(false);
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);
  const pick = (h) => {
    onPick(h.tradingsymbol, h.exchange);
    setQ("");
    setOpen(false);
  };
  return (
    <div className={`sym-search${compact ? " compact" : ""}`} ref={box}>
      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
        <path d="M11 18a7 7 0 100-14 7 7 0 000 14zM20 20l-4-4" />
      </svg>
      <input
        value={q} placeholder={placeholder} aria-label="Search symbols"
        onChange={(e) => { setQ(e.target.value); setOpen(true); setI(0); }}
        onFocus={() => setOpen(true)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") setI((x) => Math.min(hits.length - 1, x + 1));
          else if (e.key === "ArrowUp") setI((x) => Math.max(0, x - 1));
          else if (e.key === "Enter" && hits[i]) pick(hits[i]);
          else if (e.key === "Escape") setOpen(false);
        }}
      />
      {open && hits.length > 0 && (
        <ul className="sym-hits" role="listbox">
          {hits.map((h, k) => (
            <li key={h.instrument_token} role="option" aria-selected={k === i}>
              <button type="button" className={k === i ? "on" : ""} onMouseEnter={() => setI(k)} onClick={() => pick(h)}>
                <WithLogo symbol={h.tradingsymbol} size={20}><b>{h.tradingsymbol}</b></WithLogo>
                <span>{h.name}</span>
                <small>{h.segment === "INDICES" ? "INDEX" : h.exchange}</small>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
