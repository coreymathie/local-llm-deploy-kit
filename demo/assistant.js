// Private LLM Platform console: the assistant (chat) screen and the document catalog. Corey Mathie, 2026.
// The employee-facing side of the platform: ask a question, get an answer that cites only documents the asker
// may read, open the cited passage. Admin screens live in screens.js.
import { $, $$, esc, isError, errText, toast } from "./ui.js";

// ---------- document catalog (demo/data/library.json: titles, owners, review dates) ----------

let CATALOG = null;
export async function loadCatalog() {
  if (CATALOG) return CATALOG;
  try {
    const r = await fetch("./data/library.json", { cache: "no-cache" });
    CATALOG = r.ok ? await r.json() : { collections: [], documents: [] };
  } catch {
    CATALOG = { collections: [], documents: [] };
  }
  CATALOG.byFile = new Map(CATALOG.documents.map((d) => [d.file, d]));
  CATALOG.labels = new Map(CATALOG.collections.map((c) => [c.name, c.label]));
  return CATALOG;
}
export const catalog = () => CATALOG || { documents: [], collections: [], byFile: new Map(), labels: new Map() };
export const docMeta = (file) => catalog().byFile.get(file) || null;
export const docTitle = (file) => docMeta(file)?.title || file;
export const collectionLabel = (name) => catalog().labels.get(name) || name;
export const reviewed = (iso) => (iso ? new Date(`${iso}T12:00:00`).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }) : "");
const TYPE = { pdf: "PDF", docx: "DOC", xlsx: "XLS", md: "WEB" };
export const docIcon = (type) => `<span class="doc-ico t-${esc(type || "md")}" aria-hidden="true">${esc(TYPE[type] || "DOC")}</span>`;

// ---------- people ----------

const SUGGEST = {
  priya: ["What is the level 3 salary band?", "How many days of PTO do I get?", "What is the 401k match?", "Can I accept a gift from a member?"],
  dana: ["How fast must the payments on-call engineer acknowledge a page?", "When is the production change freeze?", "How long is a vendor account valid?", "How long must passwords be?"],
  marcus: ["How fast must a potential OFAC match go to the BSA officer?", "When must unusual activity be referred to the BSA team?", "How long can we delay a suspicious transaction for an older member?", "How fast must a complaint be acknowledged?"],
  audrey: ["How long are audit logs retained?", "Who approves out-of-cycle salary adjustments?", "What is the gift limit for employees?", "When is the production change freeze?"],
  kiosk: ["What time do branches close on Saturday?", "How much is a stop payment?", "What rate is a new car loan?", "How do I become a member?"],
};
const DEFAULT_SUGGEST = ["What is the hotel cap per night?", "How long is an oral stop payment good for?", "What should I do if my laptop is stolen?", "Which wires need a callback?"];
const PEOPLE = new Set(["priya", "dana", "marcus", "audrey", "kiosk"]);

const firstName = (name) => String(name || "").split(/[\s(]/)[0];
const initials = (name) => String(name || "?").replace(/\(.*\)/, "").trim().split(/\s+/).map((w) => w[0]).slice(0, 2).join("").toUpperCase();
function greeting() {
  const h = new Date().getHours();
  return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}
const tech = () => document.body.classList.contains("tech");

function personaList(S) {
  // The business view lists the people; API keys and the bootstrap admin appear in the technical view.
  const list = S.personas.filter((p) => tech() || PEOPLE.has(p.id) || S.A.mode === "live");
  return list.length ? list : S.personas;
}
const personaOf = (S, id) => S.personas.find((p) => p.id === id) || { id, name: id, note: "" };

// ---------- answers ----------

export const NO_ANSWER_PREFIX = "I don't know";

function renderAnswer(text, key) {
  return esc(text).replace(/\s?\[(\d+(?:\s*,\s*\d+)*)\]/g, (_, nums) =>
    nums.split(/\s*,\s*/).map((n) => `<button type="button" class="cite" data-cite="${key}:${n}" aria-label="Open source ${n}">${n}</button>`).join(""));
}

function friendlyError(res) {
  if (res.status === 403) return "Your role can use the documents you're cleared for, but not free-form chat.";
  if (res.status === 404) return "There's nothing here you can read yet. Ask an administrator to give you access to a collection.";
  if (res.status === 429) return "You're asking faster than your rate limit allows. Wait a minute and try again.";
  if (res.status === 401) return "Your sign-in has expired or this key was revoked.";
  return errText({ status: res.status, detail: res.detail });
}

const restricted = (rule) => /^(document_acl|doc_acl):/.test(rule || "");
function accessText(rule) {
  if (!rule) return "";
  if (restricted(rule)) {
    const who = rule.split(":").slice(1).join(":");
    return who.startsWith("group:") ? `Restricted to the ${who.slice(6)} group` : `Restricted to ${who}`;
  }
  return "Open to everyone who can read this collection";
}

function citedSources(res) {
  const seen = new Set();
  return (res.sources || []).filter((s) => s.cited && !seen.has(s.doc_id) && seen.add(s.doc_id));
}

function sourceChips(res, key) {
  const cited = citedSources(res);
  if (!cited.length) return "";
  return `<div class="src-chips" aria-label="Sources">${cited.map((s) => {
    const m = docMeta(s.title);
    return `<button type="button" class="src-chip" data-cite="${key}:${s.n}">${docIcon(m?.type)}<span><b>${esc(docTitle(s.title))}</b><small>${esc(collectionLabel(s.collection || m?.collection || ""))}</small></span></button>`;
  }).join("")}</div>`;
}

function techDetails(S, res) {
  const bits = [];
  if (res.asked_as) bits.push(`as <code>${esc(res.asked_as)}</code>`);
  if (res.access) bits.push(`${esc(res.access.decision)} · ${esc(res.access.basis)} · ${res.access.documents_visible} visible / ${res.access.documents_hidden} hidden`);
  if (res.retrieval) bits.push(`${esc(res.retrieval.mode)}${res.retrieval.reranker !== "none" ? " + " + esc(res.retrieval.reranker) : ""}`);
  if (res.timings_ms) bits.push(`${res.timings_ms.access} / ${res.timings_ms.retrieval} / ${res.timings_ms.generation} ms`);
  if (res.usage) bits.push(`${res.usage.total_tokens} tokens`);
  if (res.model) bits.push(`<code>${esc(res.model)}</code>`);
  if (res.audit_line) bits.push(`<a href="#/audit?line=${res.audit_line}">audit line ${res.audit_line}</a>`);
  const rows = (res.sources || []).map((s) => `<tr><td>[${s.n}]</td><td>${esc(s.title)}<div class="muted tiny">${esc(s.access || "")}</div></td><td class="num">${s.score}</td><td class="small">${Object.entries(s.scores || {}).map(([k, v]) => `${esc(k)} ${typeof v === "number" ? v.toFixed(3) : esc(v)}`).join(" · ")}</td><td>${s.cited ? "cited" : ""}${(s.injection_flags || []).length ? ` <span class="pill bad">injection: ${esc(s.injection_flags.join(", "))}</span>` : ""}</td></tr>`).join("");
  return `<details class="tech-only answer-details"><summary>Retrieval details</summary><div class="meta">${bits.join("<span>·</span>")}</div>
    ${rows ? `<div class="table-wrap"><table class="small"><thead><tr><th></th><th>Passage from</th><th class="num">Score</th><th>Components</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>` : ""}</details>`;
}

function answerBody(S, res, key, live) {
  if (!res) return `<div class="typing" aria-label="Thinking"><i></i><i></i><i></i></div>`;
  if (res.status !== 200) return `<div class="answer refused" role="status">${esc(friendlyError(res))}</div>${techDetails(S, res)}`;
  const text = res.answer ?? res.content ?? "";
  if (text.startsWith(NO_ANSWER_PREFIX)) {
    return `<div class="answer not-found"><b>I couldn't find that in the documents you can access.</b><span class="hint">Try different words, or ask about a policy or procedure by name.</span></div>${techDetails(S, res)}`;
  }
  const flagged = (res.sources || []).filter((s) => s.injection_flags && s.injection_flags.length);
  const shown = live != null ? text.split(" ").slice(0, live).join(" ") : text;
  return `<div class="answer" data-answer>${renderAnswer(shown, key)}${live != null ? '<span class="caret" aria-hidden="true"></span>' : ""}</div>
    ${live == null ? sourceChips(res, key) : ""}
    ${flagged.length ? `<div class="banner warn small">A document found for this question contains text that looks like instructions to the assistant. It was flagged, logged and not followed.</div>` : ""}
    ${live == null ? techDetails(S, res) : ""}`;
}

function actionsRow(i, msg) {
  // Shown once the answer has finished streaming; buttons redrawn every tick would swallow clicks.
  if (msg.pending || msg.live != null || msg.results.some((r) => r.res.status !== 200)) return "";
  const fb = msg.feedback || "";
  return `<div class="msg-actions" role="group" aria-label="Answer actions">
    <button type="button" class="icon-act" data-act="copy" data-i="${i}" title="Copy" aria-label="Copy the answer"><svg viewBox="0 0 24 24"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg></button>
    <button type="button" class="icon-act" data-act="regen" data-i="${i}" title="Ask again" aria-label="Ask again"><svg viewBox="0 0 24 24"><path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"/></svg></button>
    <button type="button" class="icon-act ${fb === "up" ? "on" : ""}" data-act="up" data-i="${i}" title="Helpful" aria-label="Helpful" aria-pressed="${fb === "up"}"><svg viewBox="0 0 24 24"><path d="M7 11v9H4v-9zM7 11l4-8a2 2 0 0 1 3 2l-1 5h6a2 2 0 0 1 2 2.3l-1.4 7A2 2 0 0 1 17.6 21H7"/></svg></button>
    <button type="button" class="icon-act ${fb === "down" ? "on" : ""}" data-act="down" data-i="${i}" title="Not helpful" aria-label="Not helpful" aria-pressed="${fb === "down"}"><svg viewBox="0 0 24 24"><path d="M17 13V4h3v9zM17 13l-4 8a2 2 0 0 1-3-2l1-5H5a2 2 0 0 1-2-2.3l1.4-7A2 2 0 0 1 6.4 3H17"/></svg></button>
  </div>`;
}

// ---------- the screen ----------

function newSession(c) {
  const s = { id: Date.now() + Math.random(), title: "New conversation", thread: [] };
  c.sessions.unshift(s);
  c.current = s.id;
  c.pane = null;
  return s;
}
const session = (c) => c.sessions.find((s) => s.id === c.current) || newSession(c);

export async function chat(view, S) {
  const A = S.A, c = S.chat;
  await loadCatalog();
  c.sessions = c.sessions || [];
  const ready = !!S.ready;
  if (ready) {
    if (!S.personas.some((p) => p.id === c.persona)) c.persona = (personaList(S)[0] || {}).id;
    if (!S.personas.some((p) => p.id === c.persona2)) c.persona2 = (personaList(S)[1] || personaList(S)[0] || {}).id;
  }
  const cols = ready ? await A.collections() : [];
  const colNames = Array.isArray(cols) ? cols.map((x) => x.name) : [];
  if (c.collection !== "*" && !colNames.includes(c.collection)) c.collection = "*";
  const me = personaOf(S, c.persona);
  const sess = session(c);

  view.innerHTML = `
  <div class="assistant ${c.pane ? "with-pane" : ""}">
    <aside class="history" aria-label="Conversations">
      <button type="button" class="primary new-chat" id="newChat"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>New conversation</button>
      <div class="hist-label">Today</div>
      <ul class="hist-list">${c.sessions.filter((s) => s.thread.length || s.id === c.current).map((s) => `<li><button type="button" data-session="${s.id}" ${s.id === c.current ? 'aria-current="true"' : ""}>${esc(s.title)}</button></li>`).join("")}</ul>
      <div class="hist-label">Popular this week</div>
      <ul class="hist-list popular">${["How long is an oral stop payment good for?", "What is the hotel cap per night?", "When do branches close for a hurricane?", "What is the 401k match?"].map((q) => `<li><button type="button" data-q="${esc(q)}">${esc(q)}</button></li>`).join("")}</ul>
    </aside>
    <section class="convo" aria-label="Assistant">
      <div class="convo-head">
        <label class="asking"><span class="avatar" aria-hidden="true">${esc(initials(me.name))}</span><span class="asking-text"><small>Asking as</small><select id="persona" aria-label="Ask as" ${ready ? "" : "disabled"}>${(ready ? personaList(S) : [{ id: "", name: "Starting…" }]).map((p) => `<option value="${esc(p.id)}" ${p.id === c.persona ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select></span></label>
        <label class="scope"><small>Search in</small><select id="collection" aria-label="Search in" ${ready ? "" : "disabled"}><option value="*">All sources I can read</option>${colNames.map((n) => `<option value="${esc(n)}" ${n === c.collection ? "selected" : ""}>${esc(collectionLabel(n))}</option>`).join("")}</select></label>
        <label class="check compare-toggle"><input type="checkbox" id="compare" ${c.compare ? "checked" : ""} ${ready ? "" : "disabled"}/> Compare with</label>
        <select id="persona2" aria-label="Second person" ${c.compare ? "" : "hidden"}>${personaList(S).map((p) => `<option value="${esc(p.id)}" ${p.id === c.persona2 ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select>
        <details class="advanced tech-only" ${c.advanced ? "open" : ""}><summary>Advanced</summary>
          <div class="adv-grid">
            <label class="field">Answer from<select id="kind"><option value="rag" ${c.kind === "rag" ? "selected" : ""}>Documents, cited</option><option value="model" ${c.kind === "model" ? "selected" : ""}>Model only (raw chat)</option></select></label>
            <label class="field">Retrieval<select id="mode">${["hybrid", "bm25", "vector"].map((m) => `<option ${m === c.mode ? "selected" : ""}>${m}</option>`).join("")}</select></label>
            <label class="field">Passages<select id="topk">${[2, 4, 6, 8, 12].map((k) => `<option ${k === c.top_k ? "selected" : ""}>${k}</option>`).join("")}</select></label>
            <label class="check"><input type="checkbox" id="rerank" ${c.reranker === "lexical" ? "checked" : ""}/> Lexical reranker</label>
          </div>
          <div class="whocard" id="who"></div>
        </details>
      </div>
      <div class="thread" id="thread" aria-live="polite"></div>
      <form class="composer" id="composer">
        <label class="sr-only" for="prompt">Ask a question</label>
        <textarea id="prompt" rows="1" placeholder="${ready ? "Ask about a policy, procedure or product…" : "The assistant is starting…"}" ${ready ? "" : "disabled"}>${esc(c.draft || "")}</textarea>
        <button class="primary send" id="send" type="submit" aria-label="Send" ${ready ? "" : "disabled"}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg></button>
      </form>
      <p class="composer-note" id="composerNote"></p>
    </section>
    <aside class="source-pane" id="pane" aria-label="Source" ${c.pane ? "" : "hidden"}></aside>
  </div>`;

  const paint = () => {
    // Looked up on every paint: a question sent while chat() re-renders finishes into the new DOM.
    const thread = $("#thread", view);
    if (!thread) return;
    const t = sess.thread;
    if (!t.length) {
      const sugg = SUGGEST[c.persona] || DEFAULT_SUGGEST;
      const name = PEOPLE.has(c.persona) && c.persona !== "kiosk" ? `, ${esc(firstName(me.name))}` : "";
      thread.innerHTML = `<div class="hero">
        <div class="hero-mark" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M12 3l7 3v6c0 4.5-3 7.6-7 9-4-1.4-7-4.5-7-9V6z"/><path d="M9 12l2 2 4-4"/></svg></div>
        <h1>${greeting()}${name}</h1>
        <p class="hero-sub">${ready ? `Ask about Cypress Harbor's policies, procedures and products. Answers cite the documents ${c.persona === "kiosk" ? "this kiosk" : "you"} can read${S.whoCount ? `: ${S.whoCount} of ${catalog().documents.length}` : ""}.` : "Getting the assistant ready. It runs entirely inside the credit union's network."}</p>
        <div class="sugg-grid">${sugg.map((q) => `<button type="button" class="sugg" data-q="${esc(q)}" ${ready ? "" : "disabled"}><span>${esc(q)}</span><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg></button>`).join("")}</div>
      </div>`;
    } else {
      thread.innerHTML = t.map((m, i) => m.role === "user"
        ? `<div class="msg user"><div class="bubble">${esc(m.text)}</div></div>`
        : `<div class="msg bot" data-i="${i}"><span class="bot-avatar" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M12 3l7 3v6c0 4.5-3 7.6-7 9-4-1.4-7-4.5-7-9V6z"/></svg></span>
            <div class="bot-body">${m.results.length > 1
              ? `<div class="compare">${m.results.map((r, j) => `<div class="cmp-col"><div class="cmp-who"><span class="avatar sm" aria-hidden="true">${esc(initials(r.personaName))}</span>${esc(r.personaName)}</div>${answerBody(S, r.res, `${i}:${j}`, m.live)}</div>`).join("")}</div>`
              : answerBody(S, m.results[0]?.res, `${i}:0`, m.live)}
            ${m.live == null ? actionsRow(i, m) : ""}</div></div>`).join("");
    }
    thread.scrollTop = thread.scrollHeight;
    $("#composerNote", view).innerHTML = c.kind === "model"
      ? "Model-only chat: no documents and no citations. For policy questions, answer from documents."
      : `Answers come only from documents ${c.persona === "kiosk" ? "the kiosk" : (PEOPLE.has(c.persona) && esc(firstName(me.name))) || "you"} may read, with citations. Check the source before acting on anything that affects a member's money.${A.mode === "demo" ? '<span class="tech-only"> Demo answerer: extractive (best-matching sentences), no LLM in the browser.</span>' : ""}`;
    paintPane();
  };

  const paintPane = () => {
    const pane = $("#pane", view);
    const root = view.querySelector(".assistant");
    if (!c.pane) { pane.hidden = true; root.classList.remove("with-pane"); return; }
    const [i, j, n] = c.pane;
    const res = sess.thread[i]?.results[j]?.res;
    const src = (res?.sources || []).find((s) => s.n === n);
    if (!src) { c.pane = null; pane.hidden = true; return; }
    const m = docMeta(src.title);
    const quoted = String(res.answer || "").split(/(?<=\])\.?\s*/).filter((x) => new RegExp(`\\[${n}\\]`).test(x)).map((x) => x.replace(/\s*\[\d+\]\.?$/, "").trim()).filter(Boolean);
    let passage = esc(src.excerpt.replace(/^#.*\n+/, ""));
    for (const q of quoted) { const e = esc(q); if (passage.includes(e)) passage = passage.replace(e, `<mark>${e}</mark>`); }
    const others = (res.sources || []).filter((s) => s.n !== n);
    pane.hidden = false;
    root.classList.add("with-pane");
    pane.innerHTML = `<div class="pane-head"><span class="pane-n">${n}</span><div><h2>${esc(docTitle(src.title))}</h2>
        <p class="muted small">${esc(collectionLabel(src.collection || m?.collection || ""))}${m ? ` · ${esc(m.department)} · owner ${esc(m.owner)}` : ""}</p></div>
        <button type="button" class="ghost icon" id="paneClose" aria-label="Close the source"><svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg></button></div>
      ${m ? `<div class="pane-meta">${docIcon(m.type)}<span>Version ${esc(m.version)} · reviewed ${esc(reviewed(m.reviewed))}</span></div>` : ""}
      <div class="pane-access">${restricted(src.access) ? '<span class="pill warn">Restricted</span>' : '<span class="pill ok">Open to staff</span>'} <span class="small muted">${esc(accessText(src.access))}</span><span class="tech-only muted tiny mono">${esc(src.access || "")}</span></div>
      <h3 class="pane-h">Passage ${src.cited ? "quoted in the answer" : "retrieved"}</h3>
      <div class="passage">${passage.split(/\n{2,}/).map((para) => /^#{1,6} /.test(para) ? `<h4>${para.replace(/^#{1,6} /, "")}</h4>` : `<p>${para.replace(/\n/g, " ")}</p>`).join("")}</div>
      ${others.length ? `<h3 class="pane-h">Other passages considered</h3><ul class="others">${others.map((s) => `<li><button type="button" data-cite="${i}:${j}:${s.n}"><span class="pane-n sm">${s.n}</span>${esc(docTitle(s.title))}${s.cited ? ' <span class="pill ok">cited</span>' : ""}</button></li>`).join("")}</ul>` : ""}
      <p class="tech-only muted tiny" style="margin-top:10px">${esc(src.title)} · passage ${src.chunk} · score ${src.score}</p>`;
    $("#paneClose", view).onclick = () => { c.pane = null; paintPane(); };
  };

  const stream = (msg) => {
    const words = Math.max(...msg.results.map((r) => String(r.res?.answer || "").split(" ").length));
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches || words < 4) { msg.live = null; paint(); return; }
    msg.live = 0;
    const tick = () => {
      if (msg.live == null) return;
      msg.live = Math.min(words, msg.live + 3);
      if (msg.live >= words) { msg.live = null; paint(); return; }
      paint();
      setTimeout(tick, 45);
    };
    tick();
  };

  const send = async (text, replaceAt = -1) => {
    text = String(text || "").trim();
    if (!text || !S.ready) return;
    c.draft = "";
    $("#prompt", view).value = "";
    const ids = c.compare ? [c.persona, c.persona2] : [c.persona];
    const msg = { role: "bot", kind: c.kind, pending: true, results: [], question: text };
    if (replaceAt >= 0) sess.thread[replaceAt] = msg;
    else { sess.thread.push({ role: "user", text }); sess.thread.push(msg); }
    if (sess.title === "New conversation") {
      sess.title = text.length > 48 ? text.slice(0, 46) + "…" : text;
      const hb = view.querySelector('.history [aria-current="true"]');
      if (hb) hb.textContent = sess.title;
    }
    paint();
    $("#send", view).disabled = true;
    try {
      for (const id of ids) {
        const res = c.kind === "model"
          ? await A.chat(id, text)
          : await A.ask(id, c.collection, text, { top_k: c.top_k, mode: c.mode, reranker: c.reranker });
        msg.results.push({ persona: id, personaName: personaOf(S, id).name, res });
      }
    } catch (e) {
      msg.results.push({ persona: ids[0], personaName: personaOf(S, ids[0]).name, res: { status: 0, detail: String(e.message || e) } });
    }
    msg.pending = false;
    $("#send", view).disabled = false;
    if (c.kind === "model") msg.results.forEach((r) => { if (r.res.status === 200) r.res.answer = r.res.content; });
    stream(msg);
  };

  const refreshWho = async () => {
    if (!S.ready) return;
    const w = await A.whoami(c.persona, c.collection);
    if (isError(w)) { $("#who", view).innerHTML = `<span class="pill bad">${esc(errText(w))}</span>`; return; }
    S.whoCount = (w.visible || []).length;
    $("#who", view).innerHTML = `<span><b>${esc(me.name)}</b> · <code>${esc(w.label)}</code></span>
      <span>roles ${(w.roles || []).map((r) => `<span class="pill info">${esc(r)}</span>`).join(" ") || '<span class="pill bad">none</span>'}</span>
      <span>groups ${(w.groups || []).map((g) => `<span class="pill">${esc(g)}</span>`).join(" ") || '<span class="muted">none</span>'}</span>
      <span class="muted">sees ${S.whoCount} document(s)${w.hidden_count !== undefined ? ` · ${w.hidden_count} hidden` : ""}</span>`;
    if (!sess.thread.length) paint();
  };

  // ---------- events ----------
  $("#newChat", view).onclick = () => { newSession(c); chat(view, S); $("#prompt")?.focus(); };
  $$("[data-session]", view).forEach((b) => (b.onclick = () => { c.current = Number(b.dataset.session); c.pane = null; chat(view, S); }));
  $$(".history [data-q]", view).forEach((b) => (b.onclick = () => {
    if (!sess.thread.length) { send(b.dataset.q); return; }
    newSession(c);
    S.pendingQuestion = b.dataset.q;
    chat(view, S);
  }));
  $("#persona", view).onchange = (e) => { c.persona = e.target.value; S.whoCount = null; if (!sess.thread.length) chat(view, S); else refreshWho(); };
  $("#persona2", view).onchange = (e) => { c.persona2 = e.target.value; };
  $("#compare", view).onchange = (e) => { c.compare = e.target.checked; $("#persona2", view).hidden = !c.compare; };
  $("#collection", view).onchange = (e) => { c.collection = e.target.value; refreshWho(); };
  $(".advanced", view).ontoggle = (e) => { c.advanced = e.target.open; };
  $("#kind", view).onchange = (e) => { c.kind = e.target.value; paint(); };
  $("#mode", view).onchange = (e) => { c.mode = e.target.value; };
  $("#rerank", view).onchange = (e) => { c.reranker = e.target.checked ? "lexical" : "none"; };
  $("#topk", view).onchange = (e) => { c.top_k = Number(e.target.value); };
  const prompt = $("#prompt", view);
  const grow = () => { prompt.style.height = "auto"; prompt.style.height = Math.min(180, prompt.scrollHeight) + "px"; };
  prompt.oninput = (e) => { c.draft = e.target.value; grow(); };
  prompt.onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(e.target.value); } };
  $("#composer", view).onsubmit = (e) => { e.preventDefault(); send(prompt.value); };
  // Assigned, not added: chat() re-renders into the same element, and stacked listeners would
  // toggle feedback twice per click.
  view.onclick = (e) => {
    const sug = e.target.closest(".hero [data-q]");
    if (sug) { send(sug.dataset.q); return; }
    const cite = e.target.closest("[data-cite]");
    if (cite) {
      const [i, j, n] = cite.dataset.cite.split(":").map(Number);
      c.pane = [i, j, n];
      paintPane();
      if (window.matchMedia("(max-width: 1100px)").matches) $("#pane", view).scrollIntoView({ block: "start", behavior: "smooth" });
      return;
    }
    const act = e.target.closest("[data-act]");
    if (act) {
      const i = Number(act.dataset.i), m = sess.thread[i];
      if (act.dataset.act === "copy") {
        const text = m.results.map((r) => (m.results.length > 1 ? `${r.personaName}: ` : "") + (r.res.answer || r.res.content || "")).join("\n\n");
        navigator.clipboard?.writeText(text).then(() => toast("Answer copied"), () => toast("Copy isn't available in this browser", "bad"));
      } else if (act.dataset.act === "regen") {
        send(m.question, i);
      } else {
        m.feedback = m.feedback === act.dataset.act ? "" : act.dataset.act;
        if (m.feedback) toast(m.feedback === "up" ? "Thanks. Feedback helps the team tune the library." : "Thanks. The document owner will see this answer in the review queue.");
        paint();
      }
    }
  };
  paint();
  grow();
  refreshWho();
  if (S.pendingQuestion && S.ready) { const q = S.pendingQuestion; S.pendingQuestion = null; send(q); }
}
