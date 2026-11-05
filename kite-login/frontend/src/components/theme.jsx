import { useEffect, useState } from "react";

// Dark / daylight theme on <html data-theme>, remembered in this browser. index.html applies the saved
// choice before the first paint; this hook keeps every toggle in sync.
const KEY = "st.theme";
const EVT = "st-theme";

export function useTheme() {
  const [theme, setTheme] = useState(() => document.documentElement.dataset.theme || "dark");
  useEffect(() => {
    const on = () => setTheme(document.documentElement.dataset.theme || "dark");
    window.addEventListener(EVT, on);
    return () => window.removeEventListener(EVT, on);
  }, []);
  const set = (t) => {
    document.documentElement.dataset.theme = t;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", t === "light" ? "#f4f7fb" : "#0a0f19");
    try {
      localStorage.setItem(KEY, t);
    } catch {
      /* private window: lasts for this visit */
    }
    window.dispatchEvent(new Event(EVT));
  };
  return [theme, set];
}

export function ThemeToggle({ className = "square-btn" }) {
  const [theme, set] = useTheme();
  const light = theme === "light";
  return (
    <button type="button" className={`${className} theme-tog`} onClick={() => set(light ? "dark" : "light")} aria-label={light ? "Switch to dark theme" : "Switch to daylight theme"}
      title={light ? "Dark theme" : "Daylight theme"}>
      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        {light
          ? <path d="M21 12.8A9 9 0 1111.2 3a7 7 0 009.8 9.8z" />
          : <path d="M12 17a5 5 0 100-10 5 5 0 000 10zM12 1v2M12 21v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M1 12h2M21 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4" />}
      </svg>
    </button>
  );
}
