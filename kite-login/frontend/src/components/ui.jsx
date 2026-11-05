import { useCallback, useEffect, useMemo, useRef, useState } from "react";

export function Spinner({ small }) {
  return <span className={`spinner${small ? " small" : ""}`} aria-hidden="true" />;
}

export function Badge({ tone = "", children, title }) {
  return (
    <span className={`badge ${tone}`} title={title}>
      {children}
    </span>
  );
}

const PRODUCT_TONES = { CNC: "blue", MIS: "amber", NRML: "violet", MTF: "teal", CO: "amber" };
export const ProductBadge = ({ product }) => <Badge tone={PRODUCT_TONES[product] || ""}>{product}</Badge>;
export const SideBadge = ({ side }) => <Badge tone={side === "BUY" ? "buy" : "sell"}>{side}</Badge>;

export function Segmented({ options, value, onChange, size, label }) {
  return (
    <div className={`segmented${size ? ` ${size}` : ""}`} role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button
          key={o.id}
          type="button"
          role="radio"
          aria-checked={value === o.id}
          className={value === o.id ? "on" : ""}
          disabled={o.disabled}
          title={o.reason || o.desc}
          onClick={() => onChange(o.id)}
        >
          {o.label}
          {o.sub && <small>{o.sub}</small>}
        </button>
      ))}
    </div>
  );
}

export function Modal({ title, onClose, children, footer, wide }) {
  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal${wide ? " wide" : ""}`} role="dialog" aria-modal="true" aria-label={title}>
        <header>
          <h3>{title}</h3>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
            ×
          </button>
        </header>
        <div className="modal-body">{children}</div>
        {footer && <footer>{footer}</footer>}
      </div>
    </div>
  );
}

export function Stat({ label, value, tone, sub }) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span className={`stat-value ${tone || ""}`}>{value}</span>
      {sub && <span className="stat-sub">{sub}</span>}
    </div>
  );
}

export function Empty({ children }) {
  return <div className="empty">{children}</div>;
}

export function ErrorNote({ error }) {
  return error ? (
    <p className="error" role="alert">
      {error}
    </p>
  ) : null;
}

// Fetch on mount (and every `every` ms while the tab is visible); returns { data, error, loading, reload }.
export function useLoad(fn, deps = [], every = 0) {
  const [state, setState] = useState({ data: null, error: "", loading: true });
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const reload = useCallback(async (quiet) => {
    if (!quiet) setState((s) => ({ ...s, loading: true }));
    try {
      const data = await fnRef.current();
      setState({ data, error: "", loading: false });
    } catch (e) {
      setState((s) => ({ ...s, error: e.message, loading: false }));
    }
  }, []);
  useEffect(() => {
    reload();
    if (!every) return undefined;
    const t = setInterval(() => document.visibilityState === "visible" && reload(true), every);
    return () => clearInterval(t);
  }, deps); // eslint-disable-line react-hooks/exhaustive-deps
  return { ...state, reload };
}

// Sortable table. columns: [{ key, label, align, render(row), sort(row) }]
export function DataTable({ columns, rows, initialSort, rowKey, empty = "Nothing here yet.", limit = 200, rowClass }) {
  const [sort, setSort] = useState(initialSort || null);
  const [shown, setShown] = useState(limit);
  const sorted = useMemo(() => {
    if (!sort) return rows;
    const col = columns.find((c) => c.key === sort.key);
    const get = col?.sort || ((r) => r[sort.key]);
    return [...rows].sort((a, b) => {
      const x = get(a);
      const y = get(b);
      if (x === y) return 0;
      if (x === null || x === undefined || x === "") return 1;
      if (y === null || y === undefined || y === "") return -1;
      const r = typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y));
      return sort.dir === "asc" ? r : -r;
    });
  }, [rows, sort, columns]);

  if (!rows.length) return <Empty>{empty}</Empty>;
  const toggle = (key) =>
    setSort((s) => (s?.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "desc" }));

  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            {columns.map((c) => (
              <th
                key={c.key}
                className={`${c.align || ""}${c.nosort ? "" : " sortable"}`}
                onClick={c.nosort ? undefined : () => toggle(c.key)}
                aria-sort={sort?.key === c.key ? (sort.dir === "asc" ? "ascending" : "descending") : undefined}
              >
                {c.label}
                {sort?.key === c.key && <span className="sort-mark">{sort.dir === "asc" ? "▲" : "▼"}</span>}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.slice(0, shown).map((r, i) => (
            <tr key={rowKey ? rowKey(r) : i} className={rowClass?.(r)}>
              {columns.map((c) => (
                <td key={c.key} className={c.align || ""}>
                  {c.render ? c.render(r) : r[c.key] ?? "–"}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {sorted.length > shown && (
        <button type="button" className="ghost more" onClick={() => setShown((s) => s + limit)}>
          Show more ({sorted.length - shown} left)
        </button>
      )}
    </div>
  );
}
