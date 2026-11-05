import { useEffect, useState } from "react";
import { api, LOGGED_OUT_EVENT } from "../lib/api.js";
import Dashboard from "./Dashboard.jsx";
import Login, { PENDING_TOKEN_STORAGE, rememberUser } from "./Login.jsx";
import { Spinner } from "../components/ui.jsx";

// Kite redirects here with ?request_token=… when the app's redirect URL is this page.
// In the login popup: hand the token to the opener and close. In a normal tab: keep it for the login form.
function captureRedirect() {
  const params = new URLSearchParams(window.location.search);
  if (!params.has("request_token") && !params.has("status")) return false;
  const token = params.get("status") === "success" ? params.get("request_token") : "";
  if (window.opener && window.opener !== window) {
    try {
      window.opener.postMessage({ type: "kite-request-token", token }, window.location.origin);
      window.close();
      return true;
    } catch {
      /* opener gone: fall through */
    }
  }
  try {
    if (token) sessionStorage.setItem(PENDING_TOKEN_STORAGE, token);
  } catch {
    /* storage blocked */
  }
  window.history.replaceState(null, "", window.location.pathname + window.location.hash);
  return false;
}

const isPopup = captureRedirect();

export default function App() {
  const [session, setSession] = useState(null); // null = checking
  const [notice, setNotice] = useState("");

  useEffect(() => {
    if (isPopup) return;
    api
      .session()
      .then((s) => {
        if (s.logged_in) rememberUser(s);
        setNotice(s.reason || "");
        setSession(s);
      })
      .catch((e) => {
        setNotice(e.message);
        setSession({ logged_in: false });
      });
    const onOut = (e) => {
      setNotice(e.detail || "Kite session expired; please log in again");
      setSession({ logged_in: false });
    };
    window.addEventListener(LOGGED_OUT_EVENT, onOut);
    return () => window.removeEventListener(LOGGED_OUT_EVENT, onOut);
  }, []);

  if (isPopup) return <div className="splash">Login complete. You can close this window.</div>;
  if (session === null) {
    return (
      <div className="splash">
        <Spinner /> Checking saved Kite session…
      </div>
    );
  }
  if (!session.logged_in) {
    return (
      <Login
        notice={notice}
        onLoggedIn={(s) => {
          setNotice("");
          setSession(s);
        }}
      />
    );
  }
  return (
    <Dashboard
      session={session}
      onLogout={async () => {
        await api.logout().catch(() => {});
        setNotice("");
        setSession({ logged_in: false });
      }}
    />
  );
}
