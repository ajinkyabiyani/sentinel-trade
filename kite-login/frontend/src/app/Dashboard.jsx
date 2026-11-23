import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api.js";
import { dateTime, until } from "../lib/format.js";
import BrandLogo from "../components/BrandLogo.jsx";
import GttDialog from "../components/GttDialog.jsx";
import { MarketClock, Toasts, useAlerts } from "../components/live.jsx";
import { NewsTicker, useNews } from "../components/news.jsx";
import { Avatar, UserCard } from "./Login.jsx";
import { ThemeToggle } from "../components/theme.jsx";
import { SymbolSearch, useWatchlist } from "../components/watchlist.jsx";
import MultiAgentTeam from "../pages/MultiAgentTeam.jsx";
import GapMomentum from "../pages/GapMomentum.jsx";
import ClaudeTrader from "../pages/ClaudeTrader.jsx";
import StrategyRules, { RULES_TAB_KEY } from "../pages/StrategyRules.jsx";
import ExpiryRange from "../pages/ExpiryRange.jsx";
import EventScreener from "../pages/EventScreener.jsx";
import Holdings from "../pages/Holdings.jsx";
import Home from "../pages/Home.jsx";
import Options from "../pages/Options.jsx";
import Orders from "../pages/Orders.jsx";
import Paper from "../pages/Paper.jsx";
import Positions from "../pages/Positions.jsx";
import Profile from "../pages/Profile.jsx";
import Trade from "../pages/Trade.jsx";
import Calendar from "../pages/terminal/Calendar.jsx";
import Fundamental from "../pages/terminal/Fundamental.jsx";
import SectorRotation from "../pages/terminal/SectorRotation.jsx";
import InsiderIpo from "../pages/terminal/InsiderIpo.jsx";
import News from "../pages/terminal/News.jsx";
import Scanner from "../pages/terminal/Scanner.jsx";

// icon = SVG path (24px grid, stroked); title / sub = the page header
const NAV = [
  { group: "Main", items: [
    { id: "dashboard", label: "Home", icon: "M3 11l9-7 9 7v9a1 1 0 01-1 1h-5v-6H9v6H4a1 1 0 01-1-1z",
      title: "Home", sub: "The Indian market at a glance, on live prices." },
    { id: "calendar", label: "Calendar", icon: "M4 6h16v14H4zM4 10h16M8 3v4M16 3v4",
      title: "Calendar", sub: "RBI policy, F&O expiries, results, dividends and IPOs." },
    { id: "stock", label: "Fundamental", icon: "M12 3a9 9 0 109 9h-9z" },
    { id: "rotation", label: "Sector Rotation", icon: "M12 3a9 9 0 109 9M12 3v9h9M12 3a9 9 0 019 9",
      title: "Sector Rotation", sub: "NSE sectors and themes by RSI: which are leading, weakening, lagging or improving, live." },
    { id: "insiders", label: "Insider and IPO", icon: "M16 20v-2a4 4 0 00-4-4H6a4 4 0 00-4 4v2M9 10a4 4 0 100-8 4 4 0 000 8zM17 11l2 2 4-4",
      title: "Insider and IPO", sub: "An agent that rates upcoming IPOs on fundamentals, GMP and timing, and the bulk, block and insider trades reported to NSE." },
  ] },
  { group: "Trading Tools", items: [
    { id: "scanner", label: "Scanner", icon: "M11 19a8 8 0 100-16 8 8 0 000 16zM21 21l-4.3-4.3",
      title: "Scanner", sub: "Filter the Nifty 500 by price, volume, volatility and trend, on live prices." },
    { id: "news", label: "News", icon: "M4 5h13v14a2 2 0 002 2H6a2 2 0 01-2-2zM17 9h3v10a2 2 0 01-4 0M8 9h5M8 13h5",
      title: "News", sub: "Stock alerts and global events first, then every market headline, with your price alerts." },
    { id: "options", label: "Option Chain", icon: "M4 6h7M4 12h7M4 18h7M13 6h7M13 12h7M13 18h7",
      title: "Option Chain", sub: "Live chain, Greeks, OI build-up and a strategy builder with payoff." },
    { id: "events", label: "Event Screener", icon: "M4 6h16M7 12h10M10 18h4",
      title: "Event Screener", sub: "From world events to the less obvious Indian companies they may touch, with the evidence and what is missing. Research only." },
  ] },
  { group: "Agents", items: [
    { id: "expiry_range", label: "Expiry Range", icon: "M4 12h16M7 8v8M17 8v8M4 6v12M20 6v12",
      title: "Expiry Range Agent", sub: "Predicts where Nifty and Bank Nifty close on each weekly expiry, three ways, and checks its own win rate." },
  ] },
  { group: "Strategies", items: [
    { id: "multi_agent_team", label: "Multi Agent Team", icon: "M12 3v3M7 7h10a2 2 0 012 2v8a2 2 0 01-2 2H7a2 2 0 01-2-2V9a2 2 0 012-2zM9 12h.01M15 12h.01M9 16h6",
      title: "Multi Agent Team", sub: "Technical, Risk and Trader agents on Claude Opus 5.5, reading live Nifty, Bank Nifty and F&O stock data. Scans by itself after every 5-minute candle." },
    { id: "gap_momentum", label: "Gap Momentum", icon: "M3 17l5-5 4 3 8-9M15 6h5v5",
      title: "Gap Momentum", sub: "Buys one ATM stock option a day in the direction of the biggest opening gap, only when Nifty is trending. Paper, with Telegram calls." },
    { id: "claude_trader", label: "Claude Trader", icon: "M12 2a4 4 0 014 4v1h1a3 3 0 013 3v3a3 3 0 01-3 3h-1v1a4 4 0 01-8 0v-1H7a3 3 0 01-3-3v-3a3 3 0 013-3h1V6a4 4 0 014-4zM9.5 11h.01M14.5 11h.01",
      title: "Claude Trader", sub: "No strategy given: every 15 minutes Claude reads live Kite data for Nifty, Bank Nifty and the top F&O movers and decides on its own whether to buy a call or a put. Paper, with Telegram calls." },
    { id: "rules", label: "Strategy Rules", icon: "M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0M14 4v4M8 10v4M16 16v4",
      title: "Strategy Rules", sub: "Capital, risk, limits, timing and filters for every strategy. Changes apply on each strategy's next pass." },
  ] },
  { group: "Portfolio & Broker", items: [
    { id: "paper", label: "Portfolio & Risk", icon: "M4 8h16v12H4zM9 8V5h6v3",
      title: "Portfolio & Risk", sub: "One paper portfolio: ₹1 lakh for each strategy, marked live." },
    { id: "trade", label: "Trade", icon: "M7 4v16M7 4l-3 3M7 4l3 3M17 20V4M17 20l-3-3M17 20l3-3",
      title: "Place an order", sub: "Every Zerodha product, variety and order type, with margin and charges before you send." },
    { id: "orders", label: "Orders", icon: "M9 6h11M9 12h11M9 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01",
      title: "Orders", sub: "Today's order book and trades: modify, cancel or repeat." },
    { id: "positions", label: "Positions", icon: "M3 17l6-6 4 4 8-8M15 7h6v6",
      title: "Positions", sub: "Live P&L by product: exit, add, or convert between CNC, MIS, MTF and NRML." },
    { id: "holdings", label: "Holdings", icon: "M4 7h16v12H4zM8 7V5h8v2",
      title: "Holdings", sub: "Your demat holdings with invested value, P&L and day change." },
  ] },
];
const TABS = NAV.flatMap((g) => g.items);
const GEAR = "M12 15a3 3 0 100-6 3 3 0 000 6zM19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.7 1.7 0 00-1.1-1.5 1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1a1.7 1.7 0 001.5-1.1 1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8V9a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z";

function NavIcon({ d, size = 16 }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={d} />
    </svg>
  );
}
const ROUTES = [...TABS.map((t) => t.id), "profile"];
const MOVED = { regime: "rotation", heatmap: "rotation" };   // old links land on Sector Rotation
const tabFromHash = () => {
  const h = window.location.hash.slice(1);
  if (MOVED[h]) return MOVED[h];
  if (h === "desks") return "paper";       // the paper desks now live in Portfolio & Risk
  return ROUTES.includes(h) ? h : "dashboard";
};
const STOCK_KEY = "kd.lastStock";
const lastStock = () => {
  try {
    return JSON.parse(localStorage.getItem(STOCK_KEY)) || { symbol: "RELIANCE", exchange: "NSE" };
  } catch {
    return { symbol: "RELIANCE", exchange: "NSE" };
  }
};

export default function Dashboard({ session, onLogout }) {
  const [tab, setTab] = useState(tabFromHash);
  const [ticket, setTicket] = useState(null); // prefill for the Trade tab, set from other tabs
  const [gtt, setGtt] = useState(null); // GTT dialog prefill
  const [menu, setMenu] = useState(false);
  const [now, setNow] = useState(Date.now());
  const [stock, setStock] = useState(lastStock);
  const news = useNews();
  const alerts = useAlerts();
  // Seen is read: a few seconds on the News page clears the new-headline and fired-alert counts (long enough to see what was new).
  const unreadNow = alerts.feed.unread + news.fresh.size;
  useEffect(() => {
    if (tab !== "news" || !unreadNow) return undefined;
    const t = setTimeout(() => {
      if (news.fresh.size) news.markRead();
      if (alerts.feed.unread) api.alertsRead().then(alerts.reloadFeed).catch(() => {});
    }, 4000);
    return () => clearTimeout(t);
  }, [tab, unreadNow]); // eslint-disable-line react-hooks/exhaustive-deps
  const wl = useWatchlist();
  // priority news pops up as it arrives: any high global event, and high stock alerts on watchlist stocks. The first load only seeds.
  const seenNews = useRef(null);
  useEffect(() => {
    const items = news.items || [];
    if (!items.length) return;
    const watched = new Set(wl.list.map((k) => k.split(":")[1]));
    const urgent = items.filter((n) => n.priority?.level === "high" && (n.priority.lane === "global" || (n.priority.lane === "stock" && (n.tickers || []).some((t) => watched.has(t)))));
    if (seenNews.current) {
      const fresh = urgent.filter((n) => !seenNews.current.has(n.id)).slice(0, 3);
      alerts.push(fresh.map((n) => ({ id: n.id, kind: n.priority.lane === "global" ? "signal" : "buy",
        title: `${n.priority.lane === "global" ? "Global" : (n.tickers || []).join(", ")} · ${n.priority.tags.join(", ")}`, body: n.title })));
    }
    seenNews.current = new Set([...(seenNews.current || []), ...urgent.map((n) => n.id)]);
  }, [news.items]); // eslint-disable-line react-hooks/exhaustive-deps
  const menuRef = useRef(null);

  useEffect(() => {
    const onHash = () => setTab(tabFromHash());
    window.addEventListener("hashchange", onHash);
    const t = setInterval(() => setNow(Date.now()), 30_000);
    return () => {
      window.removeEventListener("hashchange", onHash);
      clearInterval(t);
    };
  }, []);

  useEffect(() => {
    if (!menu) return undefined;
    const close = (e) => !menuRef.current?.contains(e.target) && setMenu(false);
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [menu]);

  // the browser tab shows only the system name; "/" jumps to the symbol search from anywhere
  useEffect(() => {
    document.title = "Sentinel Trade";
    const onKey = (e) => {
      if (e.key === "/" && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName)) {
        e.preventDefault();
        document.querySelector(".topbar .sym-search input")?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const go = (id) => {
    window.location.hash = id;
    setTab(MOVED[id] || id);
    setMenu(false);
    window.scrollTo(0, 0);
  };
  const openStock = (symbol, exchange = "NSE") => {
    const s = { symbol, exchange: ["BSE", "MCX", "CDS"].includes(exchange) ? exchange : "NSE" };
    setStock(s);
    try {
      localStorage.setItem(STOCK_KEY, JSON.stringify(s));
    } catch {
      /* private window */
    }
    go("stock");
  };
  const cur = TABS.find((t) => t.id === tab);
  const openTicket = (prefill) => {
    setTicket({ ...prefill, _at: Date.now() });
    go("trade");
  };
  const openGtt = (prefill) => setGtt({ ...prefill, _at: Date.now() });
  const unread = alerts.feed.unread + news.fresh.size;
  const shared = { go, openStock, openTicket };

  return (
    <div className="app shell side-shell">
      <nav className="sidebar" aria-label="Sections">
        <button type="button" className="side-brand" onClick={() => go("dashboard")} aria-label="Sentinel Trade home">
          <BrandLogo size={36} />
          <span className="side-brand-text">
            <span>Sentinel Trade</span>
            <small>by Ajinkya</small>
          </span>
        </button>
        <div className="side-scroll">
          {NAV.map((g) => (
            <div key={g.group} className="side-group">
              <span className="side-label">{g.group}</span>
              {g.items.map((t) => (
                <button key={t.id} type="button" className={`side-item${tab === t.id ? " on" : ""}`} onClick={() => go(t.id)} aria-current={tab === t.id ? "page" : undefined}>
                  <NavIcon d={t.icon} size={18} />
                  <span>{t.label}</span>
                  {t.id === "news" && alerts.feed.unread > 0 && <em className="side-count">{alerts.feed.unread}</em>}
                </button>
              ))}
            </div>
          ))}
        </div>
        <div className="side-foot">
          <button type="button" className={`side-item${tab === "profile" ? " on" : ""}`} onClick={() => go("profile")}>
            <NavIcon d={GEAR} size={18} />
            <span>Settings</span>
          </button>
          <button type="button" className="side-user" onClick={() => go("paper")}>
            <Avatar user={session} size={36} />
            <span>
              <b>{session.user_shortname || session.user_name || session.user_id}</b>
              <small>Paper workspace · {session.user_id}</small>
            </span>
          </button>
        </div>
      </nav>

      <div className="stage">
        <header className="topbar">
          <SymbolSearch onPick={openStock} placeholder="Search stocks or indices…  ( / )" />
          <div className="top-actions">
            <MarketClock />
            <button type="button" className="paper-pill" onClick={() => go("paper")} title="Paper trading: the desks' books, option strategies and screener trades. No order is sent to Kite from here.">
              Paper
            </button>
            <ThemeToggle />
            <button type="button" className={`square-btn${alerts.enabled ? " on" : ""}`} onClick={() => alerts.setEnabled(!alerts.enabled)}
              aria-pressed={alerts.enabled} title={alerts.enabled ? "Alerts on: toast, chime and desktop notification" : "Alerts muted"}>
              <NavIcon d={alerts.enabled ? "M4.9 19.1a10 10 0 010-14.2M19.1 4.9a10 10 0 010 14.2M7.8 16.2a6 6 0 010-8.4M16.2 7.8a6 6 0 010 8.4M12 12h.01" : "M4.9 19.1a10 10 0 010-14.2M19.1 4.9a10 10 0 010 14.2M7.8 16.2a6 6 0 010-8.4M16.2 7.8a6 6 0 010 8.4M12 12h.01M3 3l18 18"} />
            </button>
            <button type="button" className="square-btn" onClick={() => go("news")} aria-label={`News and alerts${unread ? `, ${unread} new` : ""}`} title="News & alerts">
              <NavIcon d="M6 8a6 6 0 1112 0c0 7 3 9 3 9H3s3-2 3-9M10.3 21a1.9 1.9 0 003.4 0" />
              {unread > 0 && <span className="bell-count">{unread > 99 ? "99+" : unread}</span>}
            </button>
            <button type="button" className="cta" onClick={() => go("trade")}>
              <NavIcon d="M13 2L4 14h7l-1 8 9-12h-7z" /> Trade
            </button>
            <div className="user-menu" ref={menuRef}>
              <button type="button" className="user-chip" onClick={() => setMenu((m) => !m)} aria-expanded={menu} aria-label="Account">
                <Avatar user={session} size={32} />
              </button>
              {menu && (
                <div className="menu-pop fade-in">
                  <UserCard user={session} />
                  <dl className="facts">
                    <dt>Expires</dt>
                    <dd>{dateTime(session.expires_at)} ({until(session.expires_at, now)})</dd>
                  </dl>
                  <button type="button" className="ghost block" onClick={() => go("profile")}>Profile, permissions & funds</button>
                  <button type="button" className="ghost block danger-text" onClick={onLogout}>Log out</button>
                </div>
              )}
            </div>
          </div>
        </header>

        <main className="content" key={tab}>
          {cur?.title && (
            <div className="page-head fade-in">
              <div>
                <h1>{cur.title}</h1>
                <p className="muted">{cur.sub}</p>
              </div>
              <span className="page-head-side">
                {tab === "multi_agent_team" && (
                  <button type="button" className="ghost small" onClick={() => { try { localStorage.setItem(RULES_TAB_KEY, tab); } catch { /* private window */ } go("rules"); }}>Edit rules</button>
                )}
                <span className="page-date">{new Date(now).toLocaleDateString("en-IN", { weekday: "long", day: "numeric", month: "long", year: "numeric" })}</span>
              </span>
            </div>
          )}
          {tab === "dashboard" && <Home {...shared} news={news} />}
          {tab === "stock" && <Fundamental key={`${stock.exchange}:${stock.symbol}`} symbol={stock.symbol} exchange={stock.exchange} {...shared} news={news} reloadAlerts={alerts.reloadFeed} />}
          {tab === "calendar" && <Calendar openStock={openStock} />}
          {tab === "rotation" && <SectorRotation openStock={openStock} />}
          {tab === "insiders" && <InsiderIpo openStock={openStock} />}
          {tab === "scanner" && <Scanner openStock={openStock} openTicket={openTicket} />}
          {tab === "options" && <Options openTicket={openTicket} />}
          {tab === "events" && <EventScreener openStock={openStock} />}
          {tab === "multi_agent_team" && <MultiAgentTeam />}
          {tab === "gap_momentum" && <GapMomentum />}
          {tab === "claude_trader" && <ClaudeTrader />}
          {tab === "rules" && <StrategyRules />}
          {tab === "expiry_range" && <ExpiryRange />}
          {tab === "paper" && <Paper />}
          {tab === "news" && <News news={news} feed={alerts.feed} reloadFeed={alerts.reloadFeed} openStock={openStock} />}
          {tab === "trade" && <Trade session={session} prefill={ticket} go={go} />}
          {tab === "orders" && <Orders openTicket={openTicket} openGtt={openGtt} />}
          {tab === "positions" && <Positions openTicket={openTicket} openGtt={openGtt} />}
          {tab === "holdings" && <Holdings openTicket={openTicket} openGtt={openGtt} />}
          {tab === "profile" && <Profile session={session} now={now} onLogout={onLogout} />}
          <p className="page-foot">Sentinel Trade by Ajinkya · live Kite data · paper workspace · IST</p>
        </main>
      </div>
      <Toasts toasts={alerts.toasts} dismiss={alerts.dismiss} onOpen={(to) => go(to || "paper")} />
      <NewsTicker news={news} onOpen={() => go("news")} />
      {gtt && <GttDialog key={gtt._at} prefill={gtt} onClose={() => setGtt(null)} go={go} />}
    </div>
  );
}
