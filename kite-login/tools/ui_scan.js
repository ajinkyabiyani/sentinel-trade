// UI scan (Visual_Architect_Agent): run in the browser on any Sentinel Trade page (paste into the console or the
// browser tool's javascript runner). Read-only. Returns a JSON report of layout and accessibility flaws:
//   contrast  text below WCAG 3:1 against its real (composited) background
//   overflow  elements wider than their container, or the page scrolling sideways
//   clipped   text cut off by its box (scrollWidth > clientWidth with overflow hidden / ellipsis)
//   unlabeled buttons / inputs with no accessible name
//   tiny      tap targets under 24 px
//   palette   colours left from older palettes (cyan accents, navy surfaces), and dark patches in daylight or light patches in dark
(() => {
  const parse = (c) => { const m = c.match(/[\d.]+/g); if (!m) return null; const v = m.map(Number); if (c.startsWith("color(srgb")) return [v[0] * 255, v[1] * 255, v[2] * 255, v[3] ?? 1]; return v; };
  const lum = ([r, g, b]) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
  const blend = (top, bot) => { const a = top[3] ?? 1; return [0, 1, 2].map((i) => top[i] * a + bot[i] * (1 - a)); };
  const bgOf = (el) => {
    const stack = [];
    for (let e = el; e; e = e.parentElement) {
      const c = parse(getComputedStyle(e).backgroundColor);
      if (c && (c[3] ?? 1) > 0) stack.push(c);
      if (c && (c[3] ?? 1) >= 1) break;
    }
    let base = document.documentElement.dataset.theme === "light" ? [255, 255, 255] : [10, 10, 11];
    for (let i = stack.length - 1; i >= 0; i--) base = blend(stack[i], base);
    return base;
  };
  const label = (el) => `${el.tagName.toLowerCase()}${el.className && typeof el.className === "string" ? "." + el.className.trim().split(/\s+/).slice(0, 2).join(".") : ""}`;
  const out = { page: location.hash || "/", theme: document.documentElement.dataset.theme || "dark", width: innerWidth,
    contrast: [], overflow: [], clipped: [], unlabeled: [], tiny: [], palette: [] };
  const light = (document.documentElement.dataset.theme || "dark") === "light";
  const hsl = ([r, g, b]) => { r /= 255; g /= 255; b /= 255; const mx = Math.max(r, g, b), mn = Math.min(r, g, b), l = (mx + mn) / 2, d = mx - mn; if (!d) return [0, 0, l]; const s2 = d / (1 - Math.abs(2 * l - 1)); let h = mx === r ? ((g - b) / d) % 6 : mx === g ? (b - r) / d + 2 : (r - g) / d + 4; return [(h * 60 + 360) % 360, s2, l]; };
  const oldHue = (c) => { if (!c || (c[3] ?? 1) < 0.15) return false; const [h, s2, l] = hsl(c); return (s2 > 0.35 && l > 0.15 && l < 0.92 && h >= 172 && h <= 205) || (s2 > 0.25 && l < 0.22 && h >= 200 && h <= 235); };   // leftover cyan, or navy surfaces
  const palSeen = new Set();
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
    for (const [prop, val] of [["color", cs.color], ["background", cs.backgroundColor], ["border", cs.borderTopWidth !== "0px" ? cs.borderTopColor : ""]]) {
      const c = parse(val || "");
      const k = `${label(el)}|${prop}`;
      if (c && oldHue(c) && !palSeen.has(k) && out.palette.length < 40) { palSeen.add(k); out.palette.push({ el: label(el), prop, value: val, text: (el.innerText || "").trim().slice(0, 30) }); }
    }
    const bgc = parse(cs.backgroundColor);
    if (bgc && (bgc[3] ?? 1) > 0.6 && r.width > 40 && r.height > 16) {
      const L = lum(bgc), k = `${label(el)}|patch`;
      if ((light ? L < 0.12 : L > 0.6) && !palSeen.has(k) && out.palette.length < 40 && !el.closest("svg") && el.tagName !== "IMG" && !/logo|avatar|brand|cta|badge|sf-badge|primary|buy|sell|spinner/.test(String(el.className))) { palSeen.add(k); out.palette.push({ el: label(el), prop: light ? "dark patch" : "light patch", value: cs.backgroundColor, text: (el.innerText || "").trim().slice(0, 30) }); }
    }
    const p = el.parentElement;
    if (p && r.right > p.getBoundingClientRect().right + 2 && getComputedStyle(p).overflowX === "visible" && out.overflow.length < 30)
      out.overflow.push({ el: label(el), by_px: Math.round(r.right - p.getBoundingClientRect().right) });
    if ((el.tagName === "BUTTON" || el.tagName === "INPUT" || el.tagName === "SELECT") && !(el.textContent || "").trim()
        && !el.getAttribute("aria-label") && !el.getAttribute("title") && !el.labels?.length && !el.placeholder)
      out.unlabeled.push(label(el));
    if ((el.tagName === "BUTTON" || el.tagName === "A") && (r.width < 24 || r.height < 24) && out.tiny.length < 30)
      out.tiny.push({ el: label(el), size: `${Math.round(r.width)}x${Math.round(r.height)}` });
  }
  out.page_scrolls_sideways = document.documentElement.scrollWidth > innerWidth + 1;
  out.summary = `${out.contrast.length} contrast, ${out.overflow.length} overflow, ${out.clipped.length} clipped, ${out.unlabeled.length} unlabeled, ${out.tiny.length} tiny, ${out.palette.length} palette`;
  return out;
})()
