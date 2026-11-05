# Sentinel Trade

A private trading terminal for Indian markets, built on Zerodha Kite Connect. It runs on your own PC.

## What it does

**Market terminal**

| Section | What it shows | Data |
|---|---|---|
| Home | Headline indices, breadth, market mood, sector performance, pre-open volume, FII/DII, market cap leaders, index performance, heatmap, top strategy calls | Kite, NSE |
| Calendar | RBI policy, F&O expiries, holidays, FII/DII, results dates, dividends and corporate actions, IPOs | NSE, Kite |
| Fundamental | Chart and technical read, key ratios, company profile, quarterly results, shareholding | Kite, Screener.in |
| Heatmap | Nifty 500 by sector over 1D to 1Y, sector rotation, market regime | Kite, Screener.in (market cap) |
| Insiders | Bulk, block and short deals, insider filings | NSE |
| Scanner | One-click setups and free filters over the Nifty 500 | Kite |
| News | Breaking news with the stocks mentioned, smart alerts | Public RSS feeds |
| Option Chain | Live chain, Greeks, OI build-up, strategy builder | Kite |

**Strategies and trading**: ORB and Darvas Box paper trading on ₹1 lakh each, backtests, the nightly improvement loop, Portfolio & Risk, and the Trade, Orders, Positions and Holdings pages.

Real orders are sent only from the Trade page, after you review and confirm them. Strategies and paper features never place orders.

## Project layout

```
darvas_algo.py          Darvas scanner and backtester (run as a child process)
index_lists/            Sensex members, which BSE does not publish as a file
kite-login/
  backend/
    main.py             Entry point
    core.py             App object, Kite session, shared Kite helpers
    routes/             HTTP routes, one module per feature
    strategies/         ORB, momentum sweep, market regime, paper desks, improvement loop
    terminal/           Market terminal: metrics, calendar, public data sources
  frontend/src/
    app/                App shell, login, menu
    pages/              One file per page; pages/terminal/ holds the market terminal
    components/         Shared UI, charts
    lib/                API client, formatting, live ticks, calculations
    styles/             Stylesheets
  tests/                Regression suite
  tools/                Health check and UI scan
```

## Run

Requires Python 3.12+ and Node 20+.

```bash
cd kite-login/backend
pip install -r requirements.txt
python -m uvicorn main:app --reload --reload-dir . --host 127.0.0.1 --port 8000 --timeout-graceful-shutdown 3
```

```bash
cd kite-login/frontend
npm install
npm run dev
```

Open http://127.0.0.1:5173 and log in with your Kite API key. To avoid typing the secret, put it next to `darvas_algo.py` in a file named `Credentials.txt`:

```
api_key = your_key
api_secret_key = your_secret
```

Run the tests from `kite-login/`:

```bash
python -m pytest tests -q
```

## Security

- `Credentials.txt`, `access_token.json` and `.kite_session.json` hold secrets and are excluded by `.gitignore`, which denies everything by default.
- The Kite access token stays in the backend and is never sent to the browser.
- State-changing API calls need the `X-Kite-App: 1` header, so other websites can't place orders through the local API.
- By default the dev server is reachable from your home network, and anyone who can open the page can use your logged-in session. Set `HOST=127.0.0.1` before `npm run dev` to keep it on this PC.

## Public data sources

Kite doesn't publish fundamentals, calendars or deals, so these come from public websites (`backend/terminal/sources.py`):

- The app identifies itself honestly and does not imitate a browser.
- When a source refuses, the app leaves it alone for an hour and shows the last saved copy, marked stale.
- Everything is cached on disk. Market caps load one company every 4 seconds, about 30 minutes for the Nifty 500, and refresh weekly.

These are unofficial and can change or stop without notice. **Not available from any free source I could use:** analyst ratings, and India's macro calendar with estimates (CPI, GDP, IIP, PMI).

"Market mood" is this app's own measure, calculated from India VIX, Nifty momentum, breadth, trend and 52-week highs vs lows. It is not a published index.
