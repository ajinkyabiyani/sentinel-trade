import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api.js";

export const NEWS_SOURCES = [
  { id: "all", label: "All sources" },
  { id: "mint", label: "Mint" },
  { id: "nse", label: "NSE filings" },
];
const POLL_MS = 45_000;

export function ago(iso, now = Date.now()) {
  if (!iso) return "";
  const m = Math.max(0, Math.round((now - new Date(iso).getTime()) / 60_000));
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  return new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short" });
}

// One poll for the whole app: headlines that arrive after the first load are marked fresh until read.
export function useNews() {
  const [state, setState] = useState({ items: [], fetchedAt: null, error: null, loading: true });
  const [fresh, setFresh] = useState(() => new Set());
  const known = useRef(null);

  useEffect(() => {
    let alive = true;
    let timer;
    const poll = async () => {
      try {
        const d = await api.news();
        if (!alive) return;
        if (known.current) {
          const added = d.items.filter((i) => !known.current.has(i.id)).map((i) => i.id);
          if (added.length) setFresh((f) => new Set([...f, ...added]));
        }
        known.current = new Set(d.items.map((i) => i.id));
        setState({ items: d.items, fetchedAt: d.fetched_at, error: null, loading: false });
      } catch (e) {
        if (alive) setState((s) => ({ ...s, error: e.message, loading: false }));
      }
      if (alive) timer = setTimeout(poll, POLL_MS);
    };
    poll();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, []);

  return { ...state, fresh, markRead: () => setFresh(new Set()) };
}

export function NewsList({ news, source = "all", limit, compact }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(t);
  }, []);
  const items = news.items.filter((i) => source === "all" || i.source === source).slice(0, limit);
  if (!items.length) return <p className="muted news-empty">{news.loading ? "Loading headlines…" : news.error || "No headlines from this source right now."}</p>;
  return (
    <ul className={`news-list${compact ? " compact" : ""}`}>
      {items.map((i) => (
        <li key={i.id} className={news.fresh.has(i.id) ? "is-new" : ""}>
          <div className="news-meta">
            <span className={`news-src src-${i.source}`}>{i.publisher || i.source_label}</span>
            <span className="muted">{ago(i.published, now)}</span>
            {news.fresh.has(i.id) && <span className="news-new">NEW</span>}
          </div>
          <a href={i.link} target="_blank" rel="noopener noreferrer" className="news-title">{i.title}</a>
          {!compact && i.summary && <p className="news-sum">{i.summary}</p>}
        </li>
      ))}
    </ul>
  );
}

export function NewsTicker({ news, onOpen }) {
  const items = news.items.slice(0, 12);
  if (!items.length) return null;
  return (
    <div className="news-ticker" role="region" aria-label="Breaking market news">
      <button type="button" className="news-ticker-label" onClick={onOpen}>
        <span className="live-dot" /> Market news
      </button>
      <div className="news-ticker-track">
        <div className="news-ticker-run" style={{ animationDuration: `${items.length * 9}s` }}>
          {[0, 1].map((k) => (
            <span key={k} aria-hidden={k === 1 || undefined}>
              {items.map((i) => (
                <a key={i.id} href={i.link} target="_blank" rel="noopener noreferrer" tabIndex={k ? -1 : undefined}>
                  <b>{i.publisher || i.source_label}</b> {i.title}
                </a>
              ))}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}
