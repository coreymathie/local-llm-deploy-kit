// Private LLM Platform console: small UI helpers and inline-SVG charts (no chart library). Corey Mathie, 2026.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export const fmt = (n) => (typeof n === "number" ? n.toLocaleString("en-US") : esc(n ?? "–"));
export const pct = (x) => (typeof x === "number" ? (x * 100).toFixed(1) + "%" : "–");

export function time(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return esc(ts);
  return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

export function isError(x) { return !x || (!Array.isArray(x) && typeof x.status === "number" && (x.status >= 400 || x.status === 0)); }
export function errText(x) {
  if (!x) return "No response";
  const d = x.detail;
  if (Array.isArray(d)) return d.map((e) => (e.field ? `${e.field}: ${e.message}` : e.msg || JSON.stringify(e))).join("; ");
  return `${x.status ? x.status + " · " : ""}${typeof d === "string" ? d : JSON.stringify(d)}`;
}

export const loading = (what = "Loading…") => `<div class="loading">${esc(what)}</div>`;
export const empty = (what) => `<div class="empty">${what}</div>`;
export const errorBox = (x, hint = "") => `<div class="error-state" role="alert">${esc(errText(x))}${hint ? `<div class="muted small" style="margin-top:6px">${hint}</div>` : ""}</div>`;

export function statusPill(code) {
  const cls = code >= 200 && code < 300 ? "ok" : code === 429 || code === 404 ? "warn" : "bad";
  return `<span class="pill ${cls}">${esc(code)}</span>`;
}
export function aclChips(acl, emptyText = "inherits collection") {
  if (!acl || !acl.length) return `<span class="muted small">${esc(emptyText)}</span>`;
  return `<span class="chips">${acl.map((a) => `<span class="pill info">${esc(a)}</span>`).join("")}</span>`;
}
export const sim = (text = "simulated") => `<span class="pill sim" title="Not a real model or service: a deterministic stand-in">${esc(text)}</span>`;

export function parseAcl(text) {
  return String(text || "").replace(/,/g, " ").split(/\s+/).map((s) => s.trim()).filter(Boolean);
}

// ---------- toasts, drawer, modal ----------

export function toast(msg, kind = "") {
  const box = $("#toasts");
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  box.appendChild(el);
  while (box.children.length > 3) box.firstElementChild.remove();
  setTimeout(() => el.remove(), 3500);
}

let lastFocus = null;
export function openDrawer(title, html) {
  lastFocus = document.activeElement;
  $("#drawerTitle").textContent = title;
  $("#drawerBody").innerHTML = html;
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
  $("#scrim").classList.add("open");
  $("#drawerClose").focus();
}
export function closeOverlays() {
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  $("#modal").classList.remove("open");
  $("#scrim").classList.remove("open");
  $("#sidebar").classList.remove("open");
  $("#menuBtn").setAttribute("aria-expanded", "false");
  if (lastFocus && document.body.contains(lastFocus)) lastFocus.focus();
  lastFocus = null;
}
export function openModal(html) {
  lastFocus = document.activeElement;
  const m = $("#modal");
  m.innerHTML = html;
  m.classList.add("open");
  $("#scrim").classList.add("open");
  const first = m.querySelector("input, textarea, select, button");
  if (first) first.focus();
  return m;
}

// ---------- tooltip for chart marks ----------

export function bindTooltips() {
  const tip = $("#tooltip");
  const show = (el, x, y) => {
    tip.textContent = el.getAttribute("data-tip");
    tip.style.display = "block";
    const w = tip.offsetWidth;
    tip.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, x + 12)) + "px";
    tip.style.top = y + 14 + "px";
  };
  document.addEventListener("mousemove", (e) => {
    const el = e.target.closest && e.target.closest("[data-tip]");
    if (el) show(el, e.clientX, e.clientY); else tip.style.display = "none";
  });
  document.addEventListener("focusin", (e) => {
    const el = e.target.closest && e.target.closest("[data-tip]");
    if (el) { const r = el.getBoundingClientRect(); show(el, r.left + r.width / 2, r.top); } else tip.style.display = "none";
  });
}

// ---------- charts (inline SVG) ----------

function niceMax(v, integer = false) {
  if (integer) {  // four integer steps for counts
    const step = Math.max(1, niceMax(Math.ceil(v / 4)));
    return Math.ceil(step) * 4;
  }
  if (v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

function topRounded(x, y, w, h, r) {
  if (h <= 0) return "";
  r = Math.min(r, h, w / 2);
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

/** Stacked columns. series: [{key, label, color}], rows: [{label, [key]: n}] */
export function stackedColumns(rows, series, { height = 210, ariaLabel = "chart" } = {}) {
  const W = 520, H = height, L = 30, B = 26, T = 10, R = 8;
  const totals = rows.map((r) => series.reduce((s, k) => s + (r[k.key] || 0), 0));
  const max = niceMax(Math.max(1, ...totals), true);
  const plotH = H - T - B, plotW = W - L - R;
  const step = plotW / Math.max(1, rows.length), bw = Math.max(6, Math.min(36, step * 0.6));
  let g = "";
  for (let i = 0; i <= 4; i++) {
    const v = (max / 4) * i, y = T + plotH - (v / max) * plotH;
    g += `<line class="gridline" x1="${L}" x2="${W - R}" y1="${y}" y2="${y}"/><text x="${L - 6}" y="${y + 4}" text-anchor="end">${Math.round(v * 10) / 10}</text>`;
  }
  rows.forEach((r, i) => {
    const x = L + i * step + (step - bw) / 2;
    let y = T + plotH;
    const parts = series.filter((s) => r[s.key] > 0);
    parts.forEach((s, j) => {
      const h = (r[s.key] / max) * plotH;
      const top = j === parts.length - 1;
      const gap = j > 0 ? 2 : 0;
      y -= h;
      const tip = `${r.label}: ${s.label} ${r[s.key]}`;
      g += top
        ? `<path class="bar" d="${topRounded(x, y + gap, bw, h - gap, 4)}" fill="${s.color}" data-tip="${esc(tip)}" tabindex="0" aria-label="${esc(tip)}"/>`
        : `<rect class="bar" x="${x}" y="${y + gap}" width="${bw}" height="${Math.max(0, h - gap)}" fill="${s.color}" data-tip="${esc(tip)}" tabindex="0" aria-label="${esc(tip)}"/>`;
    });
    if (i % Math.ceil(rows.length / 6) === 0 || i === rows.length - 1) {
      g += `<text x="${x + bw / 2}" y="${H - 8}" text-anchor="middle">${esc(r.label)}</text>`;
    }
  });
  g += `<line x1="${L}" x2="${W - R}" y1="${T + plotH}" y2="${T + plotH}" stroke="var(--border-strong)"/>`;
  const legend = series.map((s) => `<span><i style="background:${s.color}"></i>${esc(s.label)}</span>`).join("");
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(ariaLabel)}">${g}</svg><div class="legend">${legend}</div>`;
}

/** Horizontal bars, one series. rows: [{label, value}] */
export function hbars(rows, { color = "var(--series-1)", ariaLabel = "chart", fmtValue = (v) => v } = {}) {
  const W = 520, rowH = 26, L = 150, R = 40;
  const H = Math.max(rowH, rows.length * rowH) + 6;
  const max = Math.max(1, ...rows.map((r) => r.value));
  let g = "";
  rows.forEach((r, i) => {
    const y = 4 + i * rowH, w = Math.max(2, ((W - L - R) * r.value) / max);
    const tip = `${r.label}: ${fmtValue(r.value)}`;
    g += `<text x="${L - 8}" y="${y + 15}" text-anchor="end">${esc(r.label.length > 24 ? r.label.slice(0, 23) + "…" : r.label)}</text>`;
    g += `<path class="bar" d="M${L},${y + 4}H${L + w - 4}Q${L + w},${y + 4} ${L + w},${y + 8}V${y + 16}Q${L + w},${y + 20} ${L + w - 4},${y + 20}H${L}Z" fill="${color}" data-tip="${esc(tip)}" tabindex="0" aria-label="${esc(tip)}"/>`;
    g += `<text x="${L + w + 6}" y="${y + 16}">${esc(fmtValue(r.value))}</text>`;
  });
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(ariaLabel)}">${g}</svg>`;
}

/** Grouped columns on a 0..1 scale. groups: [{label, values: {key: v}}], series: [{key,label,color}] */
export function groupedColumns(groups, series, { height = 260, ariaLabel = "chart", min = 0, width = 1000 } = {}) {
  const W = width, H = height, L = 40, B = 28, T = 10, R = 8;
  const plotH = H - T - B, plotW = W - L - R, span = 1 - min;
  const gw = plotW / groups.length, bw = Math.min(26, (gw * 0.75) / series.length);
  let g = "";
  for (let i = 0; i <= 4; i++) {
    const v = min + (span / 4) * i, y = T + plotH - ((v - min) / span) * plotH;
    g += `<line class="gridline" x1="${L}" x2="${W - R}" y1="${y}" y2="${y}"/><text x="${L - 6}" y="${y + 4}" text-anchor="end">${(v * 100).toFixed(0)}%</text>`;
  }
  groups.forEach((grp, i) => {
    const x0 = L + i * gw + (gw - bw * series.length - 2 * (series.length - 1)) / 2;
    series.forEach((s, j) => {
      const v = grp.values[s.key];
      const h = Math.max(1, ((Math.max(v, min) - min) / span) * plotH);
      const x = x0 + j * (bw + 2), y = T + plotH - h;
      const tip = `${grp.label} · ${s.label}: ${(v * 100).toFixed(1)}%`;
      g += `<path class="bar" d="${topRounded(x, y, bw, h, 4)}" fill="${s.color}" data-tip="${esc(tip)}" tabindex="0" aria-label="${esc(tip)}"/>`;
    });
    g += `<text x="${L + i * gw + gw / 2}" y="${H - 8}" text-anchor="middle">${esc(grp.label)}</text>`;
  });
  g += `<line x1="${L}" x2="${W - R}" y1="${T + plotH}" y2="${T + plotH}" stroke="var(--border-strong)"/>`;
  const legend = series.map((s) => `<span><i style="background:${s.color}"></i>${esc(s.label)}</span>`).join("");
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(ariaLabel)}">${g}</svg><div class="legend">${legend}</div>`;
}
