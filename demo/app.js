// Private LLM Platform console: boot, routing, guided tour. Corey Mathie, 2026.
import { DemoAdapter, LiveAdapter, detectMode } from "./adapters.js";
import { loadCatalog } from "./assistant.js";
import { $, esc, closeOverlays, bindTooltips, toast } from "./ui.js";
import { SCREENS } from "./screens.js";
import { crumbs, initShell, openKeys, setActive } from "./shell.js";

// The assistant is what employees use; the admin screens are what the platform team uses.
const ROUTES = {
  chat: { group: "Assistant", label: "Ask", key: "c" },
  overview: { group: "Admin", label: "Usage and impact", key: "o", subs: { session: "This session" } },
  documents: { group: "Admin", label: "Documents", key: "d" },
  users: { group: "Admin", label: "People and keys", key: "u" },
  audit: { group: "Admin", label: "Audit log", key: "a" },
  policies: { group: "Governance", label: "Access policy", key: "y" },
  models: { group: "Governance", label: "Models", key: "m" },
  evals: { group: "Governance", label: "Answer quality", key: "e" },
  settings: { group: "Governance", label: "Settings", key: "s" },
};
const GROUPS = ["Assistant", "Admin", "Governance"];
// Screens that render from static files (no Python runtime needed): the assistant handles its own start-up state.
const STATIC = new Set(["chat", "overview"]);

const S = {
  A: null, info: null, personas: [], titles: null, ready: false, engineError: null,
  chat: { persona: "priya", persona2: "dana", compare: false, collection: "*", kind: "rag", mode: "hybrid", reranker: "none", top_k: 4, sessions: [], current: null, pane: null, draft: "" },
  docsState: { collection: "policies", addTab: "paste" },
  auditFilter: { event: "", decision: "", q: "" },
  policyState: { text: null, message: "", scenario: null, before: null, after: null },
  evalConfig: "hybrid+none",
};

const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage blocked: the choice lasts for this page */ } },
};

// ---------- business / technical view, theme ----------

function setView(v, remember = true) {
  const tech = v === "technical";
  document.body.classList.toggle("tech", tech);
  document.querySelectorAll("[data-view]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.view === v)));
  if (remember) store.set("plp.view", v);
  paintBadge();
}
function initView() {
  const q = new URLSearchParams(location.search).get("view");
  const v = q === "technical" || q === "business" ? q : store.get("plp.view") === "technical" ? "technical" : "business";
  setView(v, q === "technical" || q === "business");
  document.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", () => { setView(b.dataset.view); route(); }));
}
function initTheme() {
  const saved = store.get("plp.theme");
  if (saved === "dark" || saved === "light") document.documentElement.dataset.theme = saved;
  const btn = $("#themeBtn");
  const current = () => document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const paint = () => { const dark = current() === "dark"; btn.setAttribute("aria-pressed", String(dark)); btn.title = dark ? "Switch to light mode" : "Switch to dark mode"; btn.setAttribute("aria-label", btn.title); };
  btn.onclick = () => { const next = current() === "dark" ? "light" : "dark"; document.documentElement.dataset.theme = next; store.set("plp.theme", next); paint(); };
  paint();
}

function paintBadge() {
  const badge = $("#modeBadge");
  if (!badge || !S.A) return;
  const tech = document.body.classList.contains("tech");
  badge.className = `badge ${S.A.mode}${S.ready ? "" : S.engineError ? " error" : " starting"}`;
  const text = S.engineError ? "Offline" : S.A.mode === "live" ? `Live<span class="long"> · ${esc(location.host)}</span>` : tech ? `Demo<span class="long"> · runs in your browser</span>` : `Demo<span class="long"> workspace</span>`;
  $("#modeText").innerHTML = text;
  badge.title = S.engineError ? "The assistant couldn't start" : S.A.mode === "demo" ? "A demo workspace: the gateway's real code runs in your browser; the model is simulated" : "Connected to a running gateway";
}

// ---------- boot ----------

async function boot() {
  bindTooltips();
  initView();
  initTheme();
  const mode = await detectMode();
  S.A = mode === "live" ? new LiveAdapter() : new DemoAdapter();
  paintBadge();
  buildNav();
  $("#boot").classList.add("hidden");
  await loadCatalog();
  route();
  try {
    S.info = await S.A.boot(() => {});
  } catch (err) {
    console.error(err);
    S.engineError = err;
    document.body.dataset.ready = "error";
    paintBadge();
    route();
    return;
  }
  $("#footMode").innerHTML = mode === "demo"
    ? `Demo workspace<span class="tech-only">: the gateway's Python code in WebAssembly, simulated model</span>.`
    : `Live: gateway v${esc(S.info.version)}<span class="tech-only">, backend <code>${esc(S.info.backend)}</code></span>.`;
  S.personas = mode === "live" && !S.A.hasCredential() ? [] : await S.A.personas().catch(() => []);
  if (!Array.isArray(S.personas)) S.personas = [];
  if (mode === "live") { S.chat.persona = "self"; S.chat.persona2 = "self"; }
  S.startTour = startTour;
  S.ready = true;
  paintBadge();
  if (mode === "live" && !S.A.hasCredential() && !location.hash.startsWith("#/settings")) {
    location.hash = "#/settings";
    toast("Sign in with an admin API key to use the live console.");
  }
  await route();
  document.body.dataset.ready = "true";
  const skip = new URLSearchParams(location.search).get("tour") === "0";
  if (!skip && store.get("plp.tourDone") !== "1") $("#tourInvite").hidden = false;
}

// ---------- navigation ----------

function buildNav() {
  const icon = (s) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${s.icon}</svg>`;
  $("#nav").innerHTML = GROUPS.map((g) => {
    const items = Object.keys(ROUTES).filter((id) => ROUTES[id].group === g).map((id) => SCREENS.find((s) => s.id === id)).filter(Boolean);
    return `<div class="nav-group"><div class="nav-label" id="nl-${g.toLowerCase()}">${esc(g)}</div><div class="nav-items" role="list" aria-labelledby="nl-${g.toLowerCase()}">${items.map((s) => {
      const subs = Object.entries(ROUTES[s.id].subs || {});
      return `<div role="listitem"><a href="#/${s.id}" data-nav="${s.id}" data-route="${s.id}">${icon(s)}<span>${esc(ROUTES[s.id].label)}</span>${s.id === "audit" ? '<b class="count" id="nav-audit-count" hidden></b>' : ""}</a>${subs.length ? `<div class="sub">${subs.map(([k, label]) => `<a href="#/${s.id}/${k}" data-route="${s.id}" data-sub="${k}" class="sub-link"><span>${esc(label)}</span></a>`).join("")}</div>` : ""}</div>`;
    }).join("")}</div></div>`;
  }).join("");
  $("#nav").addEventListener("click", () => { $("#sidebar").classList.remove("open"); $("#scrim").classList.remove("open"); });
}

let routing = Promise.resolve();
function route() {
  routing = routing.then(renderRoute, renderRoute);
  return routing;
}

function engineWait(view) {
  if (S.engineError) {
    const demo = S.A?.mode === "demo";
    view.innerHTML = `<div class="card engine-wait error" role="alert"><h2>The assistant couldn't start</h2>
      <p>${demo ? "This demo runs the platform's real code in your browser. It downloads about 15 MB the first time, and some company networks block that download." : esc(S.engineError.message || S.engineError)}</p>
      <p class="muted small">Usage and impact still works.<span class="tech-only"> Error: ${esc(S.engineError.message || S.engineError)}</span></p>
      <div class="row"><button class="primary" onclick="location.reload()">Try again</button><a class="btn" href="#/overview">Usage and impact</a></div></div>`;
    return;
  }
  view.innerHTML = `<div class="card engine-wait"><div class="spinner" aria-hidden="true"></div><h2>Starting the platform</h2><p class="muted">This screen runs the gateway's real code in your browser. It takes a few seconds the first time.</p></div>`;
}

async function renderRoute() {
  const [, rawId = "chat", subRaw = ""] = location.hash.match(/^#\/([\w-]+)(?:\/([\w-]+))?/) || [];
  const screen = SCREENS.find((s) => s.id === rawId);
  const view = document.createElement("div");
  if (!screen) {
    view.innerHTML = `<div class="empty notfound"><b>There's no page at this address.</b> <a href="#/chat">Ask a question</a> or <a href="#/overview">see usage and impact</a>.</div>`;
    $("#view").replaceChildren(view);
    view.dataset.loaded = "true";
    return;
  }
  const sub = ROUTES[screen.id]?.subs?.[subRaw] ? subRaw : "";
  S.sub = sub;
  setActive(screen.id, sub);
  $("#pageTitle").innerHTML = crumbs(screen.id, sub);
  document.title = `${sub ? ROUTES[screen.id].subs[sub] : ROUTES[screen.id].label} · Cypress Harbor Assistant`;
  view.dataset.screen = screen.id;
  document.body.dataset.screen = screen.id;
  $("#view").replaceChildren(view);
  try {
    if (!S.ready && !(STATIC.has(screen.id) && !sub)) engineWait(view);
    else await screen.render(view, S);
  } catch (err) {
    console.error(err);
    view.innerHTML = `<div class="card error-state" role="alert">Something went wrong on this screen: ${esc(err.message || err)}</div>`;
  }
  view.dataset.loaded = "true";
  window.dispatchEvent(new CustomEvent("screen:rendered", { detail: { screen: screen.id } }));
}

window.addEventListener("hashchange", () => { closeOverlays(); route(); $("#view").focus({ preventScroll: true }); window.scrollTo(0, 0); });
$("#menuBtn").onclick = () => {
  const open = !$("#sidebar").classList.contains("open");
  $("#sidebar").classList.toggle("open", open);
  $("#scrim").classList.toggle("open", open);
  $("#menuBtn").setAttribute("aria-expanded", String(open));
};
$("#scrim").onclick = closeOverlays;
$("#drawerClose").onclick = closeOverlays;
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeOverlays(); endTour(false); } });
$("#tourBtn").onclick = () => startTour();
$("#tourInviteStart").onclick = () => startTour();
$("#tourInviteNo").onclick = () => { $("#tourInvite").hidden = true; store.set("plp.tourDone", "1"); };

initShell({
  home: "Cypress Harbor CU",
  crumbsHome: "#/chat",
  storageKey: "plp",
  navLinks: "#nav a[data-route]",
  routes: ROUTES,
  go: (hash) => { if (location.hash === hash) route(); else location.hash = hash; },
  commands: () => [
    { section: "Actions", label: "Ask a question", hint: "Assistant", hash: "#/chat" },
    { section: "Actions", label: "Ask the same question as two employees", hint: "Assistant › compare", run: () => { S.chat.compare = true; location.hash = "#/chat"; route(); } },
    { section: "Actions", label: "See who can read which document", hint: "Documents › access", hash: "#/documents" },
    { section: "Actions", label: "Verify the audit log", hint: "Audit log", hash: "#/audit" },
    { section: "Actions", label: "Check the models in production", hint: "Models", hash: "#/models" },
    { section: "Actions", label: document.body.classList.contains("tech") ? "Switch to the business view" : "Switch to the technical view", hint: "Show or hide code, keys and scores", run: () => { setView(document.body.classList.contains("tech") ? "business" : "technical"); route(); } },
    { section: "Actions", label: "Take the guided tour", hint: "Help", run: () => startTour() },
    { section: "Actions", label: "Show keyboard shortcuts", hint: "Help", run: openKeys },
    ...(S.personas || []).filter((p) => p.kind === "oidc").map((p) => ({ section: "Ask as", label: p.name, hint: p.note, run: () => { S.chat.persona = p.id; location.hash = "#/chat"; route(); } })),
  ],
});
$("#keys-btn").onclick = openKeys;

// ---------- guided tour: offered once, and each step opens the screen it describes ----------

const TOUR = [
  { hash: "#/chat", sel: ".sugg-grid, .thread", title: "Ask like you'd ask a colleague", text: "Employees ask about policies and procedures in plain language. Answers come only from documents the asker may read, and every answer cites them." },
  { hash: "#/chat", sel: ".asking", title: "Same question, different person", text: "Switch who's asking. Priya in HR sees the salary bands; Dana in engineering asks the same question and that document never reaches her answer." },
  { hash: "#/documents", sel: "#docTable, .card", title: "The library and who can read it", text: "57 documents in five collections, each with an owner and a review date. Access is set per collection and per document." },
  { hash: "#/audit", sel: "#au .card, .card", title: "Every answer is on the record", text: "Each question records who asked, the access decision and the documents cited, in a log that shows if anyone edits it." },
  { hash: "#/overview", sel: ".kpis", title: "What it's worth to the business", text: "Adoption by department, hours saved, cost per answer, and the governance checks examiners ask about." },
];
let tourIdx = -1;

function navigateAndWait(hash) {
  if (location.hash === hash) return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => { window.removeEventListener("screen:rendered", done); clearTimeout(t); resolve(); };
    const t = setTimeout(done, 8000);
    window.addEventListener("screen:rendered", done);
    location.hash = hash;
  });
}
function startTour() { $("#tourInvite").hidden = true; tourIdx = 0; showTour(); }
function endTour(done = true) {
  if (tourIdx < 0) return;
  tourIdx = -1;
  $("#tourCard").style.display = "none";
  document.querySelectorAll(".tour-target").forEach((n) => n.classList.remove("tour-target"));
  if (done) store.set("plp.tourDone", "1");
}
async function showTour() {
  const step = TOUR[tourIdx], idx = tourIdx;
  const card = $("#tourCard");
  card.innerHTML = `<div class="steps">Step ${idx + 1} of ${TOUR.length}</div><h2 style="margin:4px 0 6px">${esc(step.title)}</h2><p class="muted">${esc(step.text)}</p>
    <div class="row" style="justify-content:space-between;margin-top:10px"><button class="ghost sm" id="tourSkip">Skip tour</button>
    <div class="row"><button class="sm" id="tourBack" ${idx === 0 ? "disabled" : ""}>Back</button><button class="primary sm" id="tourNext">${idx === TOUR.length - 1 ? "Done" : "Next"}</button></div></div>`;
  card.style.display = "block";
  $("#tourSkip").onclick = () => endTour(true);
  $("#tourBack").onclick = () => { tourIdx = Math.max(0, tourIdx - 1); showTour(); };
  $("#tourNext").onclick = () => { if (tourIdx >= TOUR.length - 1) endTour(true); else { tourIdx++; showTour(); } };
  await navigateAndWait(step.hash);
  if (tourIdx !== idx) return;
  document.querySelectorAll(".tour-target").forEach((n) => n.classList.remove("tour-target"));
  const target = document.querySelector(step.sel);
  if (target) { target.classList.add("tour-target"); target.scrollIntoView({ block: "center", behavior: "smooth" }); }
  $("#tourNext").focus({ preventScroll: true });
}

boot();
