import { useEffect, useState } from "react";

// Live Kite ticks for EXCHANGE:SYMBOL keys, pushed by the backend's KiteTicker connection over server-sent events.
// useTicks(keys) returns { ticks: {KEY: {last_price, change, change_pct, ohlc, bid, ask, time}}, live }.
// Every hook shares ONE EventSource: a browser allows only 6 connections to a site, so a stream per hook would starve
// the page's other requests. The access token never reaches the browser: the backend holds the WebSocket to Kite.
const MAX_KEYS = 600;        // the backend's limit for one stream
const RESUBSCRIBE_MS = 200;  // keys settle before the stream is reopened
const FLUSH_MS = 300;        // subscribers are told at most this often

const subscribers = new Set();
const latest = {};           // every tick seen, so a new subscriber starts with what is known
let pending = {};
let connected = false;
let source = null;
let streamId = "";
let resubscribeTimer = null;
let flushTimer = null;

function flush() {
  flushTimer = null;
  const batch = pending;
  pending = {};
  for (const s of subscribers) {
    let part = null;
    for (const k of s.keys) {
      if (batch[k]) (part ||= {})[k] = batch[k];
    }
    s.notify(part, connected);
  }
}

const scheduleFlush = () => { flushTimer ??= setTimeout(flush, FLUSH_MS); };

// A union over the limit gives way on the largest lists, so a page's few essential prices always stream.
function wantedKeys() {
  const wanted = new Set();
  for (const s of [...subscribers].sort((a, b) => a.keys.size - b.keys.size)) {
    for (const k of s.keys) {
      if (wanted.size < MAX_KEYS) wanted.add(k);
    }
  }
  return [...wanted].sort();
}

const param = (k) => `i=${encodeURIComponent(k).replace(/%3A/g, ":")}`;

function resubscribe() {
  resubscribeTimer = null;
  const keys = wantedKeys();
  const id = keys.join("|");
  if (id === streamId) return;
  streamId = id;
  source?.close();
  source = null;
  if (!keys.length) return;
  const es = new EventSource(`/api/stream?${keys.map(param).join("&")}`);
  es.onmessage = (e) => {
    try {
      const d = JSON.parse(e.data);
      connected = Boolean(d.connected);
      if (d.ticks) {
        Object.assign(latest, d.ticks);
        Object.assign(pending, d.ticks);
      }
      scheduleFlush();
    } catch {
      /* ignore a malformed event */
    }
  };
  es.onerror = () => {
    connected = false;
    scheduleFlush();
  };
  source = es;
}

const scheduleResubscribe = () => { resubscribeTimer ??= setTimeout(resubscribe, RESUBSCRIBE_MS); };

export function useTicks(keys) {
  const id = [...new Set(keys.filter(Boolean))].sort().join("|");
  const [ticks, setTicks] = useState({});
  const [live, setLive] = useState(connected);

  useEffect(() => {
    if (!id) return undefined;
    const wanted = new Set(id.split("|"));
    const known = Object.fromEntries([...wanted].filter((k) => latest[k]).map((k) => [k, latest[k]]));
    if (Object.keys(known).length) setTicks((t) => ({ ...t, ...known }));
    setLive(connected);
    const sub = {
      keys: wanted,
      notify(part, isLive) {
        setLive(isLive);
        if (part) setTicks((t) => ({ ...t, ...part }));
      },
    };
    subscribers.add(sub);
    scheduleResubscribe();
    return () => {
      subscribers.delete(sub);
      scheduleResubscribe();
    };
  }, [id]);

  return { ticks, live };
}

if (import.meta.hot) {
  import.meta.hot.dispose(() => source?.close());
}
