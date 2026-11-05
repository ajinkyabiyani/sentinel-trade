// UI scan (Visual_Architect_Agent): run in the browser on any Sentinel Trade page (paste into the console or the
// browser tool's javascript runner). Read-only. Returns a JSON report of layout and accessibility flaws:
//   contrast  text below WCAG 3:1 against its real (composited) background
//   overflow  elements wider than their container, or the page scrolling sideways
//   clipped   text cut off by its box (scrollWidth > clientWidth with overflow hidden / ellipsis)
//   unlabeled buttons / inputs with no accessible name
//   tiny      tap targets under 24 px
(() => {
  const parse = (c) => { const m = c.match(/[\d.]+/g); return m ? m.map(Number) : null; };
  const lum = ([r, g, b]) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
  const blend = (top, bot) => { const a = top[3] ?? 1; return [0, 1, 2].map((i) => top[i] * a + bot[i] * (1 - a)); };
  const bgOf = (el) => {
    const stack = [];
    for (let e = el; e; e = e.parentElement) {
      const c = parse(getComputedStyle(e).backgroundColor);
      if (c && (c[3] ?? 1) > 0) stack.push(c);
      if (c && (c[3] ?? 1) >= 1) break;
    }
    let base = document.documentElement.dataset.theme === "light" ? [244, 247, 251] : [10, 15, 25];
    for (let i = stack.length - 1; i >= 0; i--) base = blend(stack[i], base);
    return base;
  };
  const label = (el) => `${el.tagName.toLowerCase()}${el.className && typeof el.className === "string" ? "." + el.className.trim().split(/\s+/).slice(0, 2).join(".") : ""}`;
  const out = { page: location.hash || "/", theme: document.documentElement.dataset.theme || "dark", width: innerWidth,
    contrast: [], overflow: [], clipped: [], unlabeled: [], tiny: [] };
  const seen = new Set();
  const root = document.querySelector(".stage") || document.body;
  for (const el of root.querySelectorAll("*")) {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === "hidden" || +cs.opacity === 0) continue;
    const hasText = [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim());
    if (hasText) {
      const fg = parse(cs.color);
      if (fg) {
        const bg = bgOf(el), f = blend(fg, bg);
        const L1 = lum(f), L2 = lum(bg), ratio = (Math.max(L1, L2) + 0.05) / (Math.min(L1, L2) + 0.05);
        const key = `${label(el)}|${cs.color}`;
        if (ratio < 3 && !seen.has(key)) { seen.add(key); out.contrast.push({ el: label(el), ratio: +ratio.toFixed(2), text: el.textContent.trim().slice(0, 40) }); }
      }
      if (el.scrollWidth > el.clientWidth + 1 && (cs.overflow === "hidden" || cs.textOverflow === "ellipsis") && out.clipped.length < 30)
        out.clipped.push({ el: label(el), text: el.textContent.trim().slice(0, 40) });
    }
    const p = el.parentElement;
    if (p && r.right > p.getBoundingClientRect().right + 2 && getComputedStyle(p).overflowX === "visible" && out.overflow.length < 30)
      out.overflow.push({ el: label(el), by_px: Math.round(r.right - p.getBoundingClientRect().right) });
    if ((el.tagName === "BUTTON" || el.tagName === "INPUT" || el.tagName === "SELECT") && !(el.innerText || "").trim()
        && !el.getAttribute("aria-label") && !el.getAttribute("title") && !el.labels?.length && !el.placeholder)
      out.unlabeled.push(label(el));
    if ((el.tagName === "BUTTON" || el.tagName === "A") && (r.width < 24 || r.height < 24) && out.tiny.length < 30)
      out.tiny.push({ el: label(el), size: `${Math.round(r.width)}x${Math.round(r.height)}` });
  }
  out.page_scrolls_sideways = document.documentElement.scrollWidth > innerWidth + 1;
  out.summary = `${out.contrast.length} contrast, ${out.overflow.length} overflow, ${out.clipped.length} clipped, ${out.unlabeled.length} unlabeled, ${out.tiny.length} tiny`;
  return out;
})()
