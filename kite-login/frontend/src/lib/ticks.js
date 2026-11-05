import { useEffect, useState } from "react";

// Live Kite ticks for EXCHANGE:SYMBOL keys, pushed by the backend's KiteTicker connection over
// server-sent events. Returns { ticks: {KEY: {last_price, change, change_pct, ohlc, bid, ask, time}}, live }.
// The access token never reaches the browser: the backend holds the WebSocket to Kite.
export function useTicks(keys) {
  const [ticks, setTicks] = useState({});
  const [live, setLive] = useState(false);
  const id = [...new Set(keys.filter(Boolean))].sort().join("|");

  useEffect(() => {
    if (!id) return undefined;
    const es = new EventSource(`/api/stream?${id.split("|").map((k) => `i=${encodeURIComponent(k)}`).join("&")}`);
    es.onmessage = (e) => {
      try {
        const d = JSON.parse(e.data);
        setLive(Boolean(d.connected));
        if (d.ticks && Object.keys(d.ticks).length) setTicks((t) => ({ ...t, ...d.ticks }));
      } catch {
        /* ignore a malformed event */
      }
    };
    es.onerror = () => setLive(false);
    return () => es.close();
  }, [id]);

  return { ticks, live };
}
