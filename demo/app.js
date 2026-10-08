// Private LLM Platform console: boot, routing, guided tour. Corey Mathie, 2026.
import { DemoAdapter, LiveAdapter, detectMode } from "./adapters.js";
import { $, esc, closeOverlays, bindTooltips, toast } from "./ui.js";
import { SCREENS } from "./screens.js";
import { crumbs, initShell, openKeys, setActive } from "./shell.js";

// Sidebar groups and sub-pages (the console shell is shared in design with the portfolio's other consoles).
const ROUTES = {
  overview: { group: "Monitor", label: "Overview", key: "o", subs: { session: "This session" } },
  audit: { group: "Monitor", label: "Audit", key: "a" },
  chat: { group: "Use", label: "Chat", key: "c" },
  documents: { group: "Use", label: "Documents", key: "d" },
  users: { group: "Govern", label: "Users & Keys", key: "u" },
  policies: { group: "Govern", label: "Policies", key: "y" },
  models: { group: "Govern", label: "Models", key: "m" },
  evals: { group: "Govern", label: "Evals", key: "e" },
  settings: { group: "Configure", label: "Settings", key: "s" },
};
const GROUPS = ["Monitor", "Use", "Govern", "Configure"];

const S = {
  A: null, info: null, personas: [], titles: null,
  chat: { persona: null, persona2: null, compare: false, collection: "policies", kind: "rag", mode: "hybrid", reranker: "none", top_k: 4, thread: [], selected: -1, srcTab: 0, draft: "" },
  docsState: { collection: "policies", addTab: "paste" },
  auditFilter: { event: "", decision: "", q: "" },
  policyState: { text: null, message: "", scenario: null, before: null, after: null },
  evalConfig: "hybrid+none",
};

const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage blocked: the tour just shows again */ } },
};

// ---------- boot ----------

const STEPS = {
  demo: [["runtime", "Python runtime (Pyodide 0.26.4, WebAssembly)"], ["packages", "numpy, pydantic, sqlite3"], ["files", "Gateway modules fetched from this site"], ["engine", "Engine started, API keys created"], ["samples", "Sample documents ingested, sample questions asked"]],
  live: [["connect", "Gateway API reachable"]],
};

function bootSteps(mode) {
  $("#bootSteps").innerHTML = STEPS[mode].map(([id, label]) => `<li data-step="${id}">${esc(label)}</li>`).join("");
}
function progress(id, state) { const li = document.querySelector(`[data-step="${id}"]`); if (li) li.className = state; }

async function boot() {
  bindTooltips();
  const mode = await detectMode();
  S.A = mode === "live" ? new LiveAdapter() : new DemoAdapter();
  bootSteps(mode);
  $("#bootNote").textContent = mode === "demo"
    ? "Demo mode: loading the gateway's real Python modules into your browser. Nothing you type leaves this tab."
    : "Live mode: this console is served by a gateway; it talks to its HTTP API.";
  let current = STEPS[mode][0][0];
  try {
    const wrapped = (id, state) => { current = id; progress(id, state); };
    S.info = await S.A.boot(wrapped);
  } catch (err) {
    progress(current, "fail");
    $("#bootTitle").textContent = "The console couldn't start.";
    $("#bootNote").innerHTML = `${esc(err.message || err)} ${mode === "demo" ? "Demo mode needs <code>cdn.jsdelivr.net</code> for the Python runtime; everything else is served from this site." : ""}`;
    document.body.dataset.ready = "error";
    console.error(err);
    return;
  }
  $("#bootModules").innerHTML = (S.info.modules || []).map((m) => `<span class="module">${esc(m.path)} <small>${m.lines} lines · ${esc(m.sha)}</small></span>`).join("");
  const badge = $("#modeBadge");
  badge.classList.add(mode);
  $("#modeText").innerHTML = mode === "demo" ? `Demo<span class="long"> · runs in your browser</span>` : `Live<span class="long"> · connected to ${esc(location.host)}</span>`;
  badge.title = S.info.label;
  $("#footMode").innerHTML = mode === "demo" ? "Demo: the gateway's Python code in WebAssembly, simulated model." : `Live: gateway v${esc(S.info.version)}, backend <code>${esc(S.info.backend)}</code>.`;
  $("#brandSub").textContent = mode === "demo" ? "Console · demo" : "Console · live";
  S.personas = mode === "live" && !S.A.hasCredential() ? [] : await S.A.personas().catch(() => []);
  if (!Array.isArray(S.personas)) S.personas = [];
  S.chat.persona = mode === "demo" ? "priya" : "self";
  S.chat.persona2 = mode === "demo" ? "dana" : "self";
  S.startTour = startTour;
  buildNav();
  $("#boot").classList.add("hidden");
  if (mode === "live" && !S.A.hasCredential() && !location.hash.startsWith("#/settings")) {
    location.hash = "#/settings";
    toast("Sign in with an admin API key to use the live console.");
  }
  await route();
  document.body.dataset.ready = "true";
  const skip = new URLSearchParams(location.search).get("tour") === "0";
  if (!skip && store.get("plp.tourDone") !== "1") startTour();
}

// ---------- navigation ----------

function buildNav() {
  const icon = (s) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${s.icon}</svg>`;
  $("#nav").innerHTML = GROUPS.map((g) => {
    const items = SCREENS.filter((s) => ROUTES[s.id]?.group === g);
    return `<div class="nav-group"><div class="nav-label" id="nl-${g.toLowerCase()}">${esc(g)}</div><div class="nav-items" role="list" aria-labelledby="nl-${g.toLowerCase()}">${items.map((s) => {
      const subs = Object.entries(ROUTES[s.id].subs || {});
      return `<div role="listitem"><a href="#/${s.id}" data-nav="${s.id}" data-route="${s.id}">${icon(s)}<span>${esc(s.title)}</span>${s.id === "audit" ? '<b class="count" id="nav-audit-count" hidden></b>' : ""}</a>${subs.length ? `<div class="sub">${subs.map(([k, label]) => `<a href="#/${s.id}/${k}" data-route="${s.id}" data-sub="${k}" class="sub-link"><span>${esc(label)}</span></a>`).join("")}</div>` : ""}</div>`;
    }).join("")}</div></div>`;
  }).join("");
  $("#nav").addEventListener("click", () => { $("#sidebar").classList.remove("open"); $("#scrim").classList.remove("open"); });
}

let routing = Promise.resolve();
function route() {
  routing = routing.then(renderRoute, renderRoute);
  return routing;
}

async function renderRoute() {
  const [, id = "overview", subRaw = ""] = location.hash.match(/^#\/([\w-]+)(?:\/([\w-]+))?/) || [];
  const screen = SCREENS.find((s) => s.id === id) || SCREENS[0];
  const sub = ROUTES[screen.id]?.subs?.[subRaw] ? subRaw : "";
  S.sub = sub;
  setActive(screen.id, sub);
  $("#pageTitle").innerHTML = crumbs(screen.id, sub);
  document.title = `${sub ? ROUTES[screen.id].subs[sub] : screen.title} · Private LLM Platform`;
  const view = document.createElement("div");
  view.dataset.screen = screen.id;
  $("#view").replaceChildren(view);
  try {
    await screen.render(view, S);
  } catch (err) {
    console.error(err);
    view.innerHTML = `<div class="card error-state" role="alert">Something went wrong on this screen: ${esc(err.message || err)}</div>`;
  }
  view.dataset.loaded = "true";
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

initShell({
  home: "Cypress Harbor CU",
  storageKey: "plp",
  navLinks: "#nav a[data-route]",
  routes: ROUTES,
  go: (hash) => { if (location.hash === hash) route(); else location.hash = hash; },
  commands: () => [
    { section: "Actions", label: "Ask the same question as two employees", hint: "Chat › compare", run: () => { S.chat.compare = true; location.hash = "#/chat"; } },
    { section: "Actions", label: "See who can read which document", hint: "Documents › access matrix", hash: "#/documents" },
    { section: "Actions", label: "Verify the audit chain", hint: "Audit", hash: "#/audit" },
    { section: "Actions", label: "Check model pins and the ML-BOM", hint: "Models", hash: "#/models" },
    { section: "Actions", label: "Re-run the retrieval eval", hint: "Evals", hash: "#/evals" },
    { section: "Actions", label: "Take the guided tour", hint: "Help", run: () => startTour() },
    { section: "Actions", label: "Show keyboard shortcuts", hint: "Help", run: openKeys },
    ...(S.personas || []).filter((p) => p.kind === "oidc").map((p) => ({ section: "Ask as", label: p.name, hint: p.note, run: () => { S.chat.persona = p.id; location.hash = "#/chat"; if (location.hash === "#/chat") route(); } })),
  ],
});
$("#keys-btn").onclick = openKeys;

// ---------- guided tour ----------

const TOUR = [
  { nav: "overview", title: "Welcome", text: "This console runs the gateway's real code. In demo mode it is all in your browser; in live mode it talks to a running gateway. Everything simulated is labelled." },
  { nav: "chat", title: "Permission-aware chat", text: "Ask as different people. Priya Shah (HR) gets the salary band, cited; Dana Ortiz (Engineering) asking the same question never sees that document." },
  { nav: "documents", title: "Documents and access lists", text: "Upload or paste documents, set access lists per collection and document, and see the access matrix of who can read what." },
  { nav: "audit", title: "Tamper-evident audit", text: "Every question records its access decision and cited documents in a SHA-256 hash chain. Click a row for the full decision timeline." },
  { nav: "policies", title: "Policies you can edit", text: "Change group-to-role mapping or default access, validate with the gateway's own validator, apply, and re-run a scenario." },
];
let tourIdx = -1;

function startTour() { tourIdx = 0; showTour(); }
function endTour(done = true) {
  if (tourIdx < 0) return;
  tourIdx = -1;
  $("#tourCard").style.display = "none";
  $("#tourRing").style.display = "none";
  if (done) store.set("plp.tourDone", "1");
}
function showTour() {
  const step = TOUR[tourIdx];
  const card = $("#tourCard"), ring = $("#tourRing");
  const target = document.querySelector(`[data-nav="${step.nav}"]`);
  const visible = target && target.getBoundingClientRect().width > 0 && getComputedStyle($("#sidebar")).transform === "none";
  if (visible) {
    const r = target.getBoundingClientRect();
    Object.assign(ring.style, { display: "block", left: r.left - 4 + "px", top: r.top - 4 + "px", width: r.width + 8 + "px", height: r.height + 8 + "px" });
  } else ring.style.display = "none";
  card.innerHTML = `<div class="steps">Step ${tourIdx + 1} of ${TOUR.length}</div><h2 style="margin:4px 0 6px">${esc(step.title)}</h2><p class="muted">${esc(step.text)}</p>
    <div class="row" style="justify-content:space-between;margin-top:10px"><button class="ghost sm" id="tourSkip">Skip tour</button>
    <div class="row"><a class="btn sm" href="#/${step.nav}" id="tourGo">Open ${esc(step.nav)}</a><button class="primary sm" id="tourNext">${tourIdx === TOUR.length - 1 ? "Done" : "Next"}</button></div></div>`;
  card.style.display = "block";
  $("#tourSkip").onclick = () => endTour(true);
  $("#tourNext").onclick = () => { if (tourIdx >= TOUR.length - 1) endTour(true); else { tourIdx++; showTour(); } };
}
window.addEventListener("resize", () => { if (tourIdx >= 0) showTour(); });

boot();
