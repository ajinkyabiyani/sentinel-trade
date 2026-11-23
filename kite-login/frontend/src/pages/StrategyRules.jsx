import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api.js";
import { num } from "../lib/format.js";
import { ErrorNote, Spinner, useLoad } from "../components/ui.jsx";

// Strategy Rules: every strategy's capital, risk, limits, timing and filters in one place. Edit, then save: the backend checks
// ranges and combinations, applies the change on the strategy's next pass and keeps a history. Paper only.
export const RULES_TAB_KEY = "kd.rules.tab";
const readTab = () => { try { return localStorage.getItem(RULES_TAB_KEY) || "multi_agent_team"; } catch { return "multi_agent_team"; } };

const show = (r, v) => {
  if (v == null || v === "") return "–";
  if (r.kind === "inr") return `₹${num(Number(v), 0)}`;
  if (r.kind === "limit") return Number(v) === 0 ? "No limit" : num(Number(v), 0);
  if (r.kind === "time") return v;
  return r.kind === "int" ? num(Number(v), 0) : num(Number(v), 2).replace(/\.00$/, "");
};
const same = (a, b) => String(a) === String(b) || (a !== "" && b !== "" && !Number.isNaN(Number(a)) && Number(a) === Number(b));

function Field({ r, value, onChange }) {
  const changed = !same(value, r.default);
  const step = r.step || 1;
  const bump = (d) => {
    const v = Math.min(r.max, Math.max(r.min, Math.round((Number(value || 0) + d * step) * 100) / 100));
    onChange(String(v));
  };
  return (
    <div className={`sr-field${changed ? " changed" : ""}`}>
      <div className="sr-label">
        <b>{r.label}{changed && <i className="sr-dot" title="Changed from the default" />}</b>
        <small>{r.help}</small>
      </div>
      <div className="sr-ctrl">
        {r.kind === "time" ? (
          <input type="time" value={value} min={r.min} max={r.max} onChange={(e) => onChange(e.target.value)} aria-label={r.label} />
        ) : (
          <div className={`sr-num${r.kind === "inr" ? " inr" : ""}`}>
            <button type="button" onClick={() => bump(-1)} aria-label={`Lower ${r.label}`} disabled={Number(value) <= r.min}>−</button>
            {r.kind === "inr" && <span className="sr-pre">₹</span>}
            <input type="number" value={value} min={r.min} max={r.max} step={step} onChange={(e) => onChange(e.target.value)} aria-label={r.label} />
            <button type="button" onClick={() => bump(1)} aria-label={`Raise ${r.label}`} disabled={Number(value) >= r.max}>+</button>
          </div>
        )}
        <span className="sr-meta">
          {r.kind === "limit" && Number(value) === 0 ? <em>No limit</em> : r.kind === "limit" ? <button type="button" className="link-btn" onClick={() => onChange("0")}>No limit</button> : null}
          <span>default {show(r, r.default)}</span>
          {changed && <button type="button" className="link-btn" onClick={() => onChange(String(r.default))}>reset</button>}
        </span>
      </div>
    </div>
  );
}

export default function StrategyRules() {
  const q = useLoad(api.strategyRules, []);
  const [tab, setTabState] = useState(readTab);
  const [draft, setDraft] = useState({});
  const [err, setErr] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState("");
  const [confirmReset, setConfirmReset] = useState(false);
  const setTab = (t) => { setTabState(t); setDraft({}); setErr(""); setConfirmReset(false); try { localStorage.setItem(RULES_TAB_KEY, t); } catch { /* private window */ } };
  const d = q.data;
  const st = d?.strategies?.find((s) => s.id === tab) || d?.strategies?.[0];
  useEffect(() => { setSaved(""); }, [tab]);

  const values = useMemo(() => Object.fromEntries((st?.rules || []).map((r) => [r.key, draft[r.key] ?? String(r.value ?? "")])), [st, draft]);
  const pending = (st?.rules || []).filter((r) => !same(values[r.key], r.value));
  const groups = useMemo(() => (st?.rules || []).reduce((a, r) => { (a[r.group] ||= []).push(r); return a; }, {}), [st]);

  if (!d) return <section className="card orb-loading">{q.error ? <ErrorNote error={q.error} /> : <Spinner />}</section>;
  const changedCount = (s) => s.rules.filter((r) => !same(r.value, r.default)).length;
  const save = async () => {
    setSaving(true); setErr(""); setSaved("");
    try {
      await api.strategyRulesSave(st.id, Object.fromEntries(pending.map((r) => [r.key, values[r.key]])));
      setDraft({}); setSaved(`Saved ${pending.length} change${pending.length === 1 ? "" : "s"}. ${st.name} uses them from its next pass.`);
      q.reload(true);
    } catch (e) { setErr(e.message); } finally { setSaving(false); }
  };
  const reset = async () => {
    if (!confirmReset) { setConfirmReset(true); return; }
    setConfirmReset(false); setErr("");
    try { await api.strategyRulesReset(st.id); setDraft({}); setSaved(`${st.name} is back on its default rules.`); q.reload(true); } catch (e) { setErr(e.message); }
  };
  const history = (d.history || []).filter((h) => h.strategy === st.id);
  const label = Object.fromEntries((st.rules || []).map((r) => [r.key, r]));

  return (
    <div className="tt-page sr">
      <nav className="es-tabs" aria-label="Strategies">
        {d.strategies.map((s) => (
          <button key={s.id} type="button" className={s.id === st.id ? "on" : ""} aria-current={s.id === st.id ? "page" : undefined} onClick={() => setTab(s.id)}>
            {s.name}{changedCount(s) > 0 && <em title="Rules changed from the defaults">{changedCount(s)} changed</em>}
          </button>
        ))}
      </nav>
      <div className="sr-intro">
        <p>{st.about}</p>
        <button type="button" className={`ghost small${confirmReset ? " danger" : ""}`} onClick={reset} disabled={!changedCount(st)}>{confirmReset ? "Click again to reset every rule" : "Reset to defaults"}</button>
      </div>

      <div className="sr-groups">
        {Object.entries(groups).map(([g, rules]) => (
          <section key={g} className="card sr-group">
            <header><h3>{g}</h3><span className="muted small">{rules.length} rule{rules.length === 1 ? "" : "s"}</span></header>
            {rules.map((r) => <Field key={r.key} r={r} value={values[r.key]} onChange={(v) => setDraft((x) => ({ ...x, [r.key]: v }))} />)}
          </section>
        ))}
      </div>

      <section className="card sr-history">
        <header><h3>Change history</h3><span className="muted small">{st.name}</span></header>
        {history.length ? (
          <ul>
            {history.map((h) => (
              <li key={h.at}>
                <time>{new Date(h.at).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" })}</time>
                <span>{h.changes.map((c) => <span key={c.key} className="sr-chg"><b>{c.label}</b> {show(label[c.key] || { kind: "num" }, c.from ?? 0)} → <b>{show(label[c.key] || { kind: "num" }, c.to ?? 0)}</b></span>)}</span>
              </li>
            ))}
          </ul>
        ) : <p className="muted small">No changes yet: {st.name} runs on its default rules.</p>}
        <p className="muted small">{d.note}</p>
      </section>

      {(pending.length > 0 || err || saved) && (
        <div className={`sr-bar${err ? " bad" : ""}`} role="status">
          <span>{err || (pending.length ? `${pending.length} unsaved change${pending.length === 1 ? "" : "s"} to ${st.name}` : saved)}</span>
          {pending.length > 0 && (
            <span className="sr-bar-act">
              <button type="button" className="ghost small" onClick={() => { setDraft({}); setErr(""); }}>Discard</button>
              <button type="button" className="primary small" disabled={saving} onClick={save}>{saving ? "Saving…" : "Save changes"}</button>
            </span>
          )}
          {!pending.length && <button type="button" className="link-btn" onClick={() => { setErr(""); setSaved(""); }}>Dismiss</button>}
        </div>
      )}
    </div>
  );
}
