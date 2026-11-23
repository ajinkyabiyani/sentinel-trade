import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api.js";
import { num, signed, tone } from "../lib/format.js";
import { useTicks } from "../lib/ticks.js";

// ---------------------------------------------------------------------------------------------------------------
// Market clock: NSE cash (pre-open 09:00, trading 09:15-15:30) and MCX (09:00-23:30), weekdays, in IST.
// Exchange holidays are not known here, so on a holiday it still reads "open".
// ---------------------------------------------------------------------------------------------------------------
const MARKETS = [
  { id: "NSE", pre: 540, open: 555, close: 930 },
  { id: "MCX", open: 540, close: 1410 },
];
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

function ist(ms) {
  const d = new Date(ms + (330 + new Date(ms).getTimezoneOffset()) * 60_000);
  return { day: d.getDay(), mins: d.getHours() * 60 + d.getMinutes() };
}
const dur = (m) => (m >= 60 ? `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m` : `${m}m`);
const hhmm = (m) => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;

export function marketStatus(m, now = Date.now()) {
  const { day, mins } = ist(now);
  const weekday = day >= 1 && day <= 5;
  if (weekday && mins >= m.open && mins < m.close) return { state: "open", text: "Open", sub: `closes in ${dur(m.close - mins)}` };
  if (weekday && m.pre && mins >= m.pre && mins < m.open) return { state: "pre", text: "Pre-open", sub: `opens in ${dur(m.open - mins)}` };
  if (weekday && mins < (m.pre ?? m.open)) return { state: "closed", text: "Closed", sub: `opens in ${dur((m.pre ?? m.open) - mins)}` };
  const next = day === 5 || day === 6 ? "Mon" : DAYS[(day + 1) % 7];
  return { state: "closed", text: "Closed", sub: `opens ${next} ${hhmm(m.pre ?? m.open)}` };
}

export function MarketClock() {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 15_000);
    return () => clearInterval(t);
  }, []);
  return (
    <div className="mkt-clock" title="Exchange hours in IST (holidays not shown)">
      {MARKETS.map((m) => {
        const s = marketStatus(m, now);
        return (
          <span key={m.id} className={`mkt ${s.state}`}>
            <i aria-hidden="true" />
            <b>{m.id}</b>
            <span>{s.text}</span>
            <small>{s.sub}</small>
          </span>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------------------------------------------
// Alerts: your price and change alerts and paper stop / target hits, announced on every tab with an in-app toast,
// a short chime and (once you allow it) a desktop notification. Nothing is ordered.
// ---------------------------------------------------------------------------------------------------------------
const PREF = "kd.orbAlerts";          // the stored on/off choice keeps its old name so it carries over
const readPref = () => {
  try {
    return localStorage.getItem(PREF) !== "off";
  } catch {
    return true;
  }
};
let audio;
function chime(up) {
  try {
    audio = audio || new (window.AudioContext || window.webkitAudioContext)();
    const t = audio.currentTime;
    (up ? [660, 880] : [520, 390]).forEach((f, i) => {
      const o = audio.createOscillator();
      const g = audio.createGain();
      o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, t + i * 0.14);
      g.gain.exponentialRampToValueAtTime(0.12, t + i * 0.14 + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t + i * 0.14 + 0.22);
      o.connect(g).connect(audio.destination);
      o.start(t + i * 0.14);
      o.stop(t + i * 0.14 + 0.25);
    });
  } catch {
    /* audio blocked until the page has been clicked */
  }
}

export function useAlerts() {
  const [toasts, setToasts] = useState([]);
  const [enabled, setEnabledState] = useState(readPref);
  const on = useRef(enabled);
  on.current = enabled;

  useEffect(() => {
    if (!toasts.length) return undefined;
    const t = setTimeout(() => setToasts((x) => x.slice(0, -1)), 12_000);
    return () => clearTimeout(t);
  }, [toasts]);

  const setEnabled = (v) => {
    setEnabledState(v);
    try {
      localStorage.setItem(PREF, v ? "on" : "off");
    } catch {
      /* private window: the choice lasts for this visit */
    }
    if (v) {
      chime(true);
      if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
    }
  };
  // smart alerts (price / change alerts and paper stop-target hits) from the backend checker
  const [feed, setFeed] = useState({ alerts: [], unread: 0 });
  const seenAlerts = useRef(null);
  const loadFeed = useRef(null);
  useEffect(() => {
    let alive = true;
    let timer;
    const poll = async () => {
      try {
        const f = await api.alerts();
        if (!alive) return;
        setFeed(f);
        const trig = f.alerts.filter((a) => a.status === "triggered");
        if (seenAlerts.current) {
          const fresh = trig.filter((a) => !seenAlerts.current.has(a.id));
          if (fresh.length && on.current) {
            setToasts((t) => [...fresh.map((a) => ({ id: `al-${a.id}`, kind: a.kind === "system" ? "signal" : "buy", title: a.title, body: a.body || "", go: "news" })), ...t].slice(0, 5));
            chime(true);
            if ("Notification" in window && Notification.permission === "granted") {
              fresh.forEach((a) => new Notification(a.title, { body: a.body || "", icon: "/favicon.svg", tag: a.id }));
            }
          }
        }
        seenAlerts.current = new Set(trig.map((a) => a.id));
      } catch {
        /* backend restarting */
      }
      if (alive) timer = setTimeout(poll, 15_000);
    };
    loadFeed.current = () => { clearTimeout(timer); poll(); };
    poll();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, []);

  const dismiss = (id) => setToasts((t) => t.filter((x) => x.id !== id));
  // priority news (a global event, or a stock alert on a watched stock): the same pop-up, chime and desktop notice as a fired alert
  const push = (list) => {
    if (!on.current || !list.length) return;
    setToasts((t) => [...list.map((n) => ({ id: `nw-${n.id}`, kind: n.kind || "signal", title: n.title, body: n.body || "", go: "news" })), ...t].slice(0, 5));
    chime(true);
    if ("Notification" in window && Notification.permission === "granted") list.forEach((n) => new Notification(n.title, { body: n.body || "", icon: "/favicon.svg", tag: `nw-${n.id}` }));
  };
  return { toasts, dismiss, enabled, setEnabled, feed, reloadFeed: () => loadFeed.current?.(), push };
}

export function Toasts({ toasts, dismiss, onOpen }) {
  if (!toasts.length) return null;
  return (
    <div className="toasts" role="status" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={`toast ${t.kind}`}>
          <button type="button" className="toast-body" onClick={() => { onOpen(t.go); dismiss(t.id); }}>
            <b>{t.title}</b>
            <span>{t.body}</span>
          </button>
          <button type="button" className="toast-x" onClick={() => dismiss(t.id)} aria-label="Dismiss">×</button>
        </div>
      ))}
    </div>
  );
}

// Ticker strip under the top bar: indices, India VIX, gold, silver and USD/INR on live ticks.
const STRIP = [
  { key: "NSE:NIFTY 50", label: "NIFTY 50" }, { key: "NSE:NIFTY BANK", label: "BANK NIFTY" }, { key: "BSE:SENSEX", label: "SENSEX" },
  { key: "NSE:INDIA VIX", label: "INDIA VIX" }, { key: "NSE:NIFTY MIDCAP 100", label: "MIDCAP 100" },
];
export function TickerStrip({ onOpen }) {
  const [extra, setExtra] = useState([]);
  useEffect(() => {
    // front-month gold, silver and USD/INR futures, as chosen by the backend
    api.marketIndices()
      .then((m) => setExtra(m.filter((x) => x.kind === "commodity").map((x) => ({ key: x.key, label: x.label.toUpperCase().replace(" / ", "/"), fx: x.exchange === "CDS", symbol: x.symbol, exchange: x.exchange, last: x.last, change_pct: x.change_pct }))))
      .catch(() => {});
  }, []);
  const items = [...STRIP, ...extra];
  const { ticks } = useTicks(items.map((i) => i.key));
  return (
    <div className="ticker-strip" role="list" aria-label="Market prices">
      {items.map((i) => {
        const t = ticks[i.key];
        const last = t?.last_price ?? i.last;
        const pc = t?.change_pct ?? i.change_pct;
        return (
          <button key={i.key} type="button" role="listitem" className="ts-item"
            onClick={() => onOpen(i.symbol || i.key.split(":")[1], i.exchange || i.key.split(":")[0])}>
            <b>{i.label}</b>
            <span>{last == null ? "–" : num(last, i.fx ? 4 : last > 1000 ? 0 : 2)}</span>
            {pc != null && <em className={tone(pc)}>{signed(pc)}%</em>}
          </button>
        );
      })}
    </div>
  );
}
