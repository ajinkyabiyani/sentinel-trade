import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api.js";
import { dateTime, until } from "../lib/format.js";
import BrandLogo from "../components/BrandLogo.jsx";
import GttDialog from "../components/GttDialog.jsx";
import { MarketClock, Toasts, useOrbAlerts } from "../components/live.jsx";
import { NewsTicker, useNews } from "../components/news.jsx";
import { Avatar, UserCard } from "./Login.jsx";
import { Spinner, useLoad } from "../components/ui.jsx";
import { ThemeToggle } from "../components/theme.jsx";
import { SymbolSearch } from "../components/watchlist.jsx";
import Backtest from "../pages/Backtest.jsx";
import Improve from "../pages/Improve.jsx";
import Holdings from "../pages/Holdings.jsx";
import Home from "../pages/Home.jsx";
import Options from "../pages/Options.jsx";
import Orb from "../pages/Orb.jsx";
import Orders from "../pages/Orders.jsx";
import Paper from "../pages/Paper.jsx";
import Positions from "../pages/Positions.jsx";
import Profile from "../pages/Profile.jsx";
import Screener from "../pages/Screener.jsx";
import Trade from "../pages/Trade.jsx";
import Calendar from "../pages/terminal/Calendar.jsx";
import Fundamental from "../pages/terminal/Fundamental.jsx";
import Heatmap from "../pages/terminal/Heatmap.jsx";
import Insiders from "../pages/terminal/Insiders.jsx";
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
    { id: "heatmap", label: "Heatmap", icon: "M4 4h16v16H4zM4 12h16M12 4v16",
      title: "Heatmap", sub: "The Nifty 500 by sector, with sector rotation and the market regime." },
    { id: "insiders", label: "Insiders", icon: "M16 20v-2a4 4 0 00-4-4H6a4 4 0 00-4 4v2M9 10a4 4 0 100-8 4 4 0 000 8zM17 11l2 2 4-4",
      title: "Insiders", sub: "Bulk, block and short deals, and insider filings reported to NSE." },
  ] },
  { group: "Trading Tools", items: [
    { id: "scanner", label: "Scanner", icon: "M11 19a8 8 0 100-16 8 8 0 000 16zM21 21l-4.3-4.3",
      title: "Scanner", sub: "Filter the Nifty 500 by price, volume, volatility and trend, on live prices." },
    { id: "news", label: "News", icon: "M4 5h13v14a2 2 0 002 2H6a2 2 0 01-2-2zM17 9h3v10a2 2 0 01-4 0M8 9h5M8 13h5",
      title: "News", sub: "Breaking market news with the stocks it mentions, and your smart alerts." },
    { id: "options", label: "Option Chain", icon: "M4 6h7M4 12h7M4 18h7M13 6h7M13 12h7M13 18h7",
      title: "Option Chain", sub: "Live chain, Greeks, OI build-up and a strategy builder with payoff." },
  ] },
  { group: "Strategies", items: [
    { id: "orb", label: "ORB Strategy", icon: "M4 17h4V9H4zM10 17h4V5h-4zM16 17h4v-6h-4zM3 7h18",
      title: "ORB Strategy", sub: "Opening-range breakouts on liquid F&O and momentum stocks, confirmed by Nifty." },
    { id: "darvas", label: "Darvas Box", icon: "M4 19h16M6 15V9h5v6M13 15V5h5v10",
      title: "Darvas Box (quant)", sub: "10-day boxes near 52-week highs, 1.5× volume breakouts, stop trailed box by box; buys only while Nifty is above its 50-DMA." },
    { id: "backtest", label: "Backtest", icon: "M4 19V5M4 19h16M8 15l3-4 3 2 5-6",
      title: "Backtest", sub: "Test a strategy on years of history before trusting it with capital." },
    { id: "improve", label: "Improvement Loop", icon: "M4 12a8 8 0 0114-5.3M20 12a8 8 0 01-14 5.3M18 3v4h-4M6 21v-4h4",
      title: "Improvement Loop", sub: "Four agents test each strategy every night; you approve what changes." },
  ] },
  { group: "Portfolio & Broker", items: [
    { id: "paper", label: "Portfolio & Risk", icon: "M4 8h16v12H4zM9 8V5h6v3",
      title: "Portfolio & Risk", sub: "One ₹2 lakh paper portfolio: ₹1 lakh each for ORB and Darvas, marked live." },
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
const MOVED = { rotation: "heatmap", regime: "heatmap" };   // these pages now live inside the Heatmap section
const tabFromHash = () => {
  const h = window.location.hash.slice(1);
  if (MOVED[h]) return MOVED[h];
  if (h === "screener") return "darvas";   // the old combined screener is now one tab per strategy
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
  const [scan, setScan] = useState(null);
  const [scanVersion, setScanVersion] = useState(0);
  const [stock, setStock] = useState(lastStock);
  const news = useNews();
  const alerts = useOrbAlerts();
  const darvas = useLoad(api.darvasResults, [scanVersion]);
  const orb = useLoad(api.orbState, [], 15_000);
  const menuRef = useRef(null);
  const wasRunning = useRef(false);

  useEffect(() => {
    const onHash = () => setTab(tabFromHash());
    window.addEventListener("hashchange", onHash);
    const t = setInterval(() => setNow(Date.now()), 30_000);
    return () => {
      window.removeEventListener("hashchange", onHash);
      clearInterval(t);
    };
  }, []);

  // Follow the backend's (automatic) Darvas scan; reload results everywhere when one finishes.
  useEffect(() => {
    let alive = true;
    let timer;
    const poll = async () => {
      try {
        const s = await api.darvasStatus();
        if (!alive) return;
        setScan(s);
        if (wasRunning.current && !s.running) setScanVersion((v) => v + 1);
        wasRunning.current = s.running;
        timer = setTimeout(poll, s.running ? 3000 : 30_000);
      } catch {
        timer = setTimeout(poll, 30_000);
      }
    };
    poll();
    return () => {
      alive = false;
      clearTimeout(timer);
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
  const orbSnap = orb.data?.snapshot || null;
  const unread = alerts.feed.unread + news.fresh.size;
  const shared = { go, openStock, openTicket, darvas: darvas.data, orb: orbSnap, orbError: orb.error, orbProgress: orb.data?.progress };

  return (
    <div className="app shell side-shell">
      <nav className="sidebar" aria-label="Sections">
        <button type="button" className="side-brand" onClick={() => go("dashboard")} aria-label="Sentinel Trade home">
          <BrandLogo size={36} />
          <span className="side-brand-text">
            <span>SENTINEL <b>TRADE</b></span>
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
            {scan?.running && (
              <button type="button" className="status-pill scanning" onClick={() => go("rotation")} title={`Daily scan: ${scan.stage}`}>
                <Spinner small /> Scanning{scan.progress ? ` ${scan.progress[0]}/${scan.progress[1]}` : "…"}
              </button>
            )}
            <button type="button" className="paper-pill" onClick={() => go("paper")} title="Paper trading: ORB bot, option strategies and screener trades. No order is sent to Kite from here.">
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
              <span className="page-date">{new Date(now).toLocaleDateString("en-IN", { weekday: "long", day: "numeric", month: "long", year: "numeric" })}</span>
            </div>
          )}
          {tab === "dashboard" && <Home {...shared} news={news} />}
          {tab === "stock" && <Fundamental key={`${stock.exchange}:${stock.symbol}`} symbol={stock.symbol} exchange={stock.exchange} {...shared} news={news} reloadAlerts={alerts.reloadFeed} />}
          {tab === "calendar" && <Calendar openStock={openStock} />}
          {tab === "heatmap" && <Heatmap openStock={openStock} openTicket={openTicket} scanVersion={scanVersion} />}
          {tab === "insiders" && <Insiders openStock={openStock} />}
          {tab === "scanner" && <Scanner openStock={openStock} openTicket={openTicket} />}
          {tab === "darvas" && <Screener {...shared} key="darvas" only={["ready", "buys", "sells"]} />}
          {tab === "options" && <Options openTicket={openTicket} />}
          {tab === "orb" && <Orb openTicket={openTicket} />}
          {tab === "backtest" && <Backtest />}
          {tab === "improve" && <Improve />}
          {tab === "paper" && <Paper go={go} />}
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
