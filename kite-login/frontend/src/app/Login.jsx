import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api.js";
import BrandLogo from "../components/BrandLogo.jsx";
import { dateTime, initials } from "../lib/format.js";
import { Spinner } from "../components/ui.jsx";
import { ThemeToggle } from "../components/theme.jsx";

const API_KEY_STORAGE = "kite.apiKey"; // the API key is not secret; remembering it saves typing
const LAST_USER_STORAGE = "kite.lastUser"; // name / id / email of the last login, for the welcome-back card
export const PENDING_TOKEN_STORAGE = "kite.pendingRequestToken";

const read = (k, fallback = "") => {
  try {
    return localStorage.getItem(k) ?? fallback;
  } catch {
    return fallback;
  }
};
const write = (k, v) => {
  try {
    localStorage.setItem(k, v);
  } catch {
    /* storage blocked: fine */
  }
};

export function rememberUser(s) {
  write(LAST_USER_STORAGE, JSON.stringify({ user_name: s.user_name, user_id: s.user_id, email: s.email, avatar_url: s.avatar_url }));
}

function takePendingToken() {
  try {
    const t = sessionStorage.getItem(PENDING_TOKEN_STORAGE) || "";
    sessionStorage.removeItem(PENDING_TOKEN_STORAGE);
    return t;
  } catch {
    return "";
  }
}

export function Avatar({ user, size = 40 }) {
  const [broken, setBroken] = useState(false);
  const style = { width: size, height: size, fontSize: size * 0.38 };
  return user?.avatar_url && !broken ? (
    <img className="avatar" src={user.avatar_url} alt="" style={style} onError={() => setBroken(true)} />
  ) : (
    <span className="avatar" style={style} aria-hidden="true">
      {initials(user?.user_name || user?.user_id)}
    </span>
  );
}

export function UserCard({ user, compact }) {
  return (
    <div className={`user-card${compact ? " compact" : ""}`}>
      <Avatar user={user} size={compact ? 40 : 56} />
      <div>
        <strong>{user.user_name || user.user_id}</strong>
        <span>
          {user.user_id}
          {user.broker ? ` · ${user.broker}` : ""}
        </span>
        {user.email && <span>{user.email}</span>}
      </div>
    </div>
  );
}

const STEPS = ["App credentials", "Authorize on Kite", "Connected"];

// NSE cash session in IST from the clock alone: exchange holidays aren't known here.
function marketStatus(now = new Date()) {
  const ist = new Date(now.getTime() + (now.getTimezoneOffset() + 330) * 60000);
  const mins = ist.getHours() * 60 + ist.getMinutes();
  const day = ist.getDay();
  const weekday = day >= 1 && day <= 5;
  const open = 9 * 60 + 15;
  const close = 15 * 60 + 30;
  const hm = (m) => `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
  const time = ist.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false });
  if (weekday && mins >= open && mins < close) return { live: true, time, text: `Market open · closes in ${hm(close - mins)}` };
  if (weekday && mins >= 9 * 60 && mins < open) return { live: false, time, text: `Pre-open · market opens in ${hm(open - mins)}` };
  let days = 0;
  let d = day;
  do {
    d = (d + 1) % 7;
    days += 1;
  } while (d === 0 || d === 6);
  const next = weekday && mins < open ? "today" : days === 1 ? "tomorrow" : ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"][d];
  return { live: false, time, text: `Market closed · opens ${next} 09:15` };
}

function MarketClock() {
  const [m, setM] = useState(marketStatus);
  useEffect(() => {
    const id = setInterval(() => setM(marketStatus()), 30000);
    return () => clearInterval(id);
  }, []);
  return (
    <div className={`market-clock${m.live ? " live" : ""}`}>
      <span className="pulse-dot" aria-hidden="true" />
      <span>{m.text}</span>
      <span className="muted">{m.time} IST</span>
    </div>
  );
}

export default function Login({ onLoggedIn, notice }) {
  const [config, setConfig] = useState(null);
  const [apiKey, setApiKey] = useState(() => read(API_KEY_STORAGE));
  const [secret, setSecret] = useState("");
  const [showSecret, setShowSecret] = useState(false);
  const [useSaved, setUseSaved] = useState(false);
  const [token, setToken] = useState(takePendingToken);
  const [step, setStep] = useState(0);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState(notice || "");
  const [session, setSession] = useState(null);
  const popup = useRef(null);
  const lastUser = (() => {
    try {
      return JSON.parse(read(LAST_USER_STORAGE, "null"));
    } catch {
      return null;
    }
  })();

  useEffect(() => {
    api
      .loginConfig()
      .then((c) => {
        setConfig(c);
        if (c.api_key && !apiKey) setApiKey(c.api_key);
        if (c.has_saved_secret && (!apiKey || apiKey === c.api_key)) setUseSaved(true);
      })
      .catch(() => setConfig({}));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const savedSecretUsable = config?.has_saved_secret && apiKey.trim() === config.api_key;
  const secretOk = (useSaved && savedSecretUsable) || secret.length > 0;
  const credsOk = apiKey.trim() && secretOk;

  // A request token arrived via the redirect (same tab) and the secret is on the server: finish at once.
  useEffect(() => {
    if (token && config && step === 0) {
      setStep(1);
      if (credsOk) submit(token);
    }
  }, [config]); // eslint-disable-line react-hooks/exhaustive-deps

  // The Kite popup lands on this app (when the redirect URL points here) and posts the token back.
  useEffect(() => {
    const onMsg = (e) => {
      if (e.origin !== window.location.origin || e.data?.type !== "kite-request-token") return;
      popup.current?.close();
      if (e.data.token) {
        setToken(e.data.token);
        submit(e.data.token);
      } else setError("Kite login was cancelled.");
    };
    window.addEventListener("message", onMsg);
    return () => window.removeEventListener("message", onMsg);
  });

  async function openKite() {
    setError("");
    setBusy("url");
    try {
      const { url } = await api.loginUrl(apiKey.trim());
      write(API_KEY_STORAGE, apiKey.trim());
      const w = 520;
      const h = 720;
      const left = window.screenX + (window.outerWidth - w) / 2;
      const top = window.screenY + (window.outerHeight - h) / 2;
      popup.current = window.open(url, "kite-login", `width=${w},height=${h},left=${left},top=${top}`);
      if (!popup.current) window.open(url, "_blank", "noopener");
      setStep(1);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function submit(tok = token) {
    setError("");
    setBusy("login");
    try {
      const s = await api.login({
        api_key: apiKey.trim(),
        api_secret: useSaved && savedSecretUsable ? null : secret,
        use_saved_secret: Boolean(useSaved && savedSecretUsable),
        request_token: tok.trim(),
      });
      write(API_KEY_STORAGE, apiKey.trim());
      rememberUser(s);
      setSecret("");
      setToken("");
      setSession(s);
      setStep(2);
    } catch (e) {
      setError(e.message);
      setToken(""); // a request token works only once
      setStep(1);
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="login-page">
      <aside className="login-hero">
        <div className="brand">
          <BrandLogo size={38} />
          <span className="login-brand-text">
            <span>Sentinel <b>Trade</b></span>
            <small>by Ajinkya</small>
          </span>
        </div>
        <ThemeToggle className="square-btn login-theme" />
        <div className="hero-copy">
          <MarketClock />
          <h1>Strategies, sector rotation, the option chain and your Kite account, on one screen.</h1>
          <p>Log in once a day with your Kite Connect app. The access token is created and kept on this computer's backend; the browser never sees it.</p>
          <ul className="hero-points">
            <li>
              <b>Option chain</b> live OI, PCR and max pain, whether the chain leans bullish or bearish, and where support and resistance sit
            </li>
            <li>
              <b>Rotation</b> which sectors, market caps and indices are leading, from a daily scan
            </li>
            <li>
              <b>Orders, GTTs, positions</b> every real order waits for your confirmation
            </li>
          </ul>
        </div>
        {lastUser?.user_id && step < 2 && (
          <div className="welcome-back">
            <span className="eyebrow">Welcome back</span>
            <UserCard user={lastUser} compact />
          </div>
        )}
      </aside>

      <main className="login-main">
        <div className="login-card">
          <ol className="stepper">
            {STEPS.map((s, i) => (
              <li key={s} className={i < step ? "done" : i === step ? "current" : ""}>
                <span className="dot">{i < step ? "✓" : i + 1}</span>
                <span className="step-label">{s}</span>
              </li>
            ))}
          </ol>

          {step === 2 && session ? (
            <div className="connected fade-in">
              <div className="check-burst" aria-hidden="true">
                ✓
              </div>
              <h2>You're connected</h2>
              <UserCard user={session} />
              <dl className="facts">
                <dt>User type</dt>
                <dd>{session.user_type || "–"}</dd>
                <dt>Exchanges</dt>
                <dd className="chips">{(session.exchanges || []).map((x) => <span key={x} className="chip">{x}</span>)}</dd>
                <dt>Products</dt>
                <dd className="chips">{(session.products || []).map((x) => <span key={x} className="chip">{x}</span>)}</dd>
                <dt>Session expires</dt>
                <dd>{dateTime(session.expires_at)} IST</dd>
                <dt>Darvas scan</dt>
                <dd>Refreshes after each market close, for the Rotation tab and the index trend</dd>
              </dl>
              <button type="button" className="primary block" onClick={() => onLoggedIn(session)}>
                Open dashboard →
              </button>
            </div>
          ) : (
            <form
              className="form fade-in"
              autoComplete="off"
              onSubmit={(e) => {
                e.preventDefault();
                if (step === 0) openKite();
                else submit();
              }}
            >
              <div>
                <h2>{step === 0 ? "Connect your Kite app" : "Authorize on Kite"}</h2>
                <p className="muted">
                  {step === 0
                    ? "Your Kite Connect API key and secret from developers.kite.trade."
                    : "Log in on the Kite window. If your app's redirect URL is this page, the token comes back on its own."}
                </p>
              </div>

              <label className="field">
                <span>API key</span>
                <input
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  spellCheck={false}
                  placeholder="e.g. abcd1234efgh5678"
                  required
                />
              </label>

              {config?.has_saved_secret && (
                <label className={`toggle${savedSecretUsable ? "" : " disabled"}`}>
                  <input
                    type="checkbox"
                    checked={useSaved && savedSecretUsable}
                    disabled={!savedSecretUsable}
                    onChange={(e) => setUseSaved(e.target.checked)}
                  />
                  <span className="switch" aria-hidden="true" />
                  <span>
                    Use the API secret saved in <code>Credentials.txt</code>
                    {!savedSecretUsable && <small> (it belongs to a different API key)</small>}
                  </span>
                </label>
              )}

              {!(useSaved && savedSecretUsable) && (
                <label className="field">
                  <span>API secret key</span>
                  <div className="input-group">
                    <input
                      type={showSecret ? "text" : "password"}
                      value={secret}
                      onChange={(e) => setSecret(e.target.value)}
                      autoComplete="new-password"
                      spellCheck={false}
                      required
                    />
                    <button type="button" className="ghost" onClick={() => setShowSecret((v) => !v)}>
                      {showSecret ? "Hide" : "Show"}
                    </button>
                  </div>
                  <small className="hint">Sent once to the local backend to create the session. Never stored.</small>
                </label>
              )}

              {step === 1 && (
                <label className="field fade-in">
                  <span>Request token</span>
                  <input
                    value={token}
                    onChange={(e) => setToken(e.target.value)}
                    placeholder="token, or paste the whole redirect URL"
                    spellCheck={false}
                    autoFocus
                  />
                  <small className="hint">
                    Waiting for Kite… or copy <code>request_token</code> from the address Kite sends you to. It works
                    once and expires in about 2 minutes. Set your app's redirect URL to{" "}
                    <code>{window.location.origin}/</code> to skip this step.
                  </small>
                </label>
              )}

              {error && (
                <p className="error" role="alert">
                  {error}
                </p>
              )}

              {step === 0 ? (
                <button type="submit" className="primary block" disabled={!credsOk || busy}>
                  {busy ? <Spinner small /> : null} Continue with Kite
                </button>
              ) : (
                <div className="row">
                  <button type="button" className="ghost" onClick={openKite} disabled={busy}>
                    Reopen Kite login
                  </button>
                  <button type="submit" className="primary grow" disabled={!credsOk || !token.trim() || busy}>
                    {busy === "login" ? (
                      <>
                        <Spinner small /> Connecting…
                      </>
                    ) : (
                      "Login"
                    )}
                  </button>
                </div>
              )}
            </form>
          )}
        </div>
        <p className="footnote">Single-user tool on your home network · anyone who can open this page can use your Kite session</p>
      </main>
    </div>
  );
}
