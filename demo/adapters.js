// Private LLM Platform console: data adapters. Corey Mathie, 2026.
//
// DemoAdapter runs the gateway's real Python modules in the browser (Pyodide) through demo/engine.py.
// LiveAdapter calls a running gateway's HTTP API. Both implement the same interface, so every screen
// is written once. Methods return plain JSON-able objects; failed calls return {status, detail}.

const PYODIDE_URL = "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/";

async function sha256(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function fetchText(url) {
  const r = await fetch(url, { cache: "no-cache" });
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return r.text();
}

const lastN = (key, n = 8) => String(key).slice(-n);

const storage = {
  get(k) { try { return sessionStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { v == null ? sessionStorage.removeItem(k) : sessionStorage.setItem(k, v); } catch { /* storage blocked */ } },
};

export async function detectMode() {
  const forced = new URLSearchParams(location.search).get("mode");
  if (forced === "demo" || forced === "live") return forced;
  try {
    const r = await fetch("./api-mode", { cache: "no-store", headers: { Accept: "application/json" } });
    if (r.ok) {
      const j = JSON.parse(await r.text());
      if (j.mode === "live") return "live";
    }
  } catch { /* static hosting: no gateway behind this page */ }
  return "demo";
}

// ---------------------------------------------------------------------------------------------
// Demo mode: Pyodide + demo/engine.py
// ---------------------------------------------------------------------------------------------

export class DemoAdapter {
  constructor() {
    this.mode = "demo";
    this.caps = { tamper: true, simulatedModels: true, runEval: true, reset: true, seed: false, editLock: true, hiddenCounts: true };
    this.keyMap = new Map(); // short id -> full key (kept in this tab only)
    this.modules = [];
    this.info = null;
  }

  async boot(progress) {
    const m = window.CONSOLE_MANIFEST;
    progress("runtime", "active");
    if (typeof loadPyodide !== "function") {
      await new Promise((resolve, reject) => {
        const s = document.createElement("script");
        s.src = PYODIDE_URL + "pyodide.js";
        s.onload = resolve;
        s.onerror = () => reject(new Error("Pyodide didn't load from cdn.jsdelivr.net (blocked network or offline)."));
        document.head.appendChild(s);
      });
    }
    const pyodide = await loadPyodide({ indexURL: PYODIDE_URL });
    progress("runtime", "done");

    progress("packages", "active");
    await pyodide.loadPackage(["numpy", "pydantic", "sqlite3"]);
    progress("packages", "done");

    progress("files", "active");
    const root = "/home/pyodide";
    pyodide.FS.mkdirTree(`${root}/gateway`);
    pyodide.FS.mkdirTree(`${root}/scripts`);
    pyodide.FS.writeFile(`${root}/gateway/__init__.py`, "");
    pyodide.FS.writeFile(`${root}/scripts/__init__.py`, "");
    const gw = await Promise.all(m.GATEWAY_FILES.map((f) => fetchText(new URL(`../gateway/${f}`, location.href))));
    for (let i = 0; i < m.GATEWAY_FILES.length; i++) {
      pyodide.FS.writeFile(`${root}/gateway/${m.GATEWAY_FILES[i]}`, gw[i]);
      this.modules.push({ path: `gateway/${m.GATEWAY_FILES[i]}`, lines: gw[i].split("\n").length, sha: (await sha256(gw[i])).slice(0, 8) });
    }
    const sc = await Promise.all(m.SCRIPT_FILES.map((f) => fetchText(new URL(`../scripts/${f}`, location.href))));
    m.SCRIPT_FILES.forEach((f, i) => pyodide.FS.writeFile(`${root}/scripts/${f}`, sc[i]));
    const demo = await Promise.all(m.DEMO_FILES.map((f) => fetchText(new URL(f, location.href))));
    m.DEMO_FILES.forEach((f, i) => pyodide.FS.writeFile(`${root}/${f}`, demo[i]));
    for (let i = 0; i < m.SCRIPT_FILES.length; i++) {
      this.modules.push({ path: `scripts/${m.SCRIPT_FILES[i]}`, lines: sc[i].split("\n").length, sha: (await sha256(sc[i])).slice(0, 8) });
    }
    progress("files", "done");

    progress("engine", "active");
    this.pyodide = pyodide;
    this.py = pyodide.runPython(`import sys\nsys.path.insert(0, '${root}')\nimport engine\nengine`);
    this.info = await this.call("info");
    await this.call("create_key", "billing-app");
    await this.call("create_key", "qa-app", false, ["engineering"]);
    progress("engine", "done");

    progress("samples", "active");
    for (const name of m.SAMPLES) await this.call("add_document", "policies", name, await fetchText(new URL(name, location.href)));
    for (const item of m.RESTRICTED_SAMPLES) {
      await this.call("add_document", "policies", item.file, await fetchText(new URL(item.file, location.href)), item.acl);
    }
    // A little sample traffic so the overview and audit log have something to show (labelled simulated).
    for (const [p, q] of [
      ["priya", "What is the level 3 salary band?"],
      ["dana", "What is the level 3 salary band?"],
      ["dana", "How fast must the payments on-call engineer acknowledge a page?"],
      ["kiosk", "What is the hotel cap per night?"],
      ["audrey", "How long are audit logs retained?"],
    ]) await this.call("ask_as", p, "policies", q);
    progress("samples", "done");
    return this.describe();
  }

  describe() {
    return {
      mode: "demo",
      label: "Demo · runs in your browser",
      host: location.host || "this page",
      version: "0.7.0",
      backend: "demo (simulated, no LLM)",
      python: this.info?.python,
      stubbed: this.info?.stubbed_packages || [],
      modules: this.modules,
    };
  }

  async call(method, ...args) {
    const out = await this.py.call(method, JSON.stringify(args));
    return JSON.parse(out);
  }

  // identity
  personas() { return this.call("personas"); }
  whoami(persona, collection) { return this.call("whoami", persona, collection); }
  // chat
  ask(persona, collection, question, o = {}) {
    return this.call("ask_as", persona, collection, question, o.top_k || 4, o.mode || null, o.reranker || null);
  }
  chat(persona, prompt) { return this.call("chat_as", persona, prompt); }
  // overview, audit
  overview() { return this.call("overview"); }
  auditEntries(limit = 300) { return this.call("audit_entries", limit); }
  verifyAudit() { return this.call("verify"); }
  tamper(line, mode) { return this.call("tamper", line, mode); }
  restoreAudit() { return this.call("restore"); }
  // documents
  async collections() { return this.call("collections"); }
  documents(c) { return this.call("list_documents", c); }
  addText(c, title, text, acl) { return this.call("add_document", c, title, text, acl || []); }
  async uploadFile(c, file, acl) {
    if (!/\.(md|markdown|txt|csv|json|html?)$/i.test(file.name)) {
      return { status: 400, detail: "Demo mode reads text files only (.md, .txt, .csv, .json, .html). PDF extraction runs on the server (pypdf)." };
    }
    return this.addText(c, file.name, await file.text(), acl);
  }
  removeDocument(c, id) { return this.call("remove_document", c, id); }
  setDocumentAcl(c, id, acl) { return this.call("set_document_acl", c, id, acl); }
  setCollectionAcl(c, acl) { return this.call("set_collection_acl", c, acl); }
  accessMatrix(c) { return this.call("access_matrix", c); }
  // keys and users
  async keys() {
    const rows = await this.call("list_keys");
    return rows.map((k) => { const id = lastN(k.key); this.keyMap.set(id, k.key); const { key, ...rest } = k; return { id, ...rest }; });
  }
  async createKey(label, isAdmin, groups) {
    const out = await this.call("create_key", label, !!isAdmin, groups || []);
    return { status: 200, key: out.key, label: out.label };
  }
  revokeKey(id) { return this.call("revoke_key", this.keyMap.get(id) || id); }
  async sendWithKey(id) {
    return this.call("chat", this.keyMap.get(id) || id, "Summarize our travel policy in one sentence.");
  }
  users() { return this.call("users"); }
  rateLimits() { return this.call("rate_limits"); }
  // models
  models() { return this.call("models"); }
  verifyModels() { return this.call("verify_models"); }
  pinServed() { return this.call("pin_served"); }
  simulateRepull(name) { return this.call("simulate_repull", name); }
  checkModel(name) { return this.call("check_model", name); }
  validateLock(text) { return this.call("validate_lock", text); }
  setLock(text) { return this.call("set_lock", text); }
  mlbom() { return this.call("mlbom"); }
  // policy
  policy() { return this.call("policy"); }
  validatePolicy(doc) { return this.call("validate_policy", doc); }
  applyPolicy(doc) { return this.call("apply_policy", doc); }
  // evals
  async evals() { return JSON.parse(await fetchText(new URL("data/rag_eval.json", location.href))); }
  async runEvalInBrowser(files) {
    const root = "/home/pyodide";
    this.pyodide.FS.mkdirTree(`${root}/evals/golden/docs`);
    const texts = await Promise.all(files.map((f) => fetchText(new URL(`../${f}`, location.href))));
    files.forEach((f, i) => this.pyodide.FS.writeFile(`${root}/${f}`, texts[i]));
    return this.call("run_rag_eval");
  }
  reset() { return this.call("reset"); }
}

// ---------------------------------------------------------------------------------------------
// Live mode: the gateway's HTTP API (same origin; the console is served at /console/)
// ---------------------------------------------------------------------------------------------

export class LiveAdapter {
  constructor() {
    this.mode = "live";
    this.caps = { tamper: false, simulatedModels: false, runEval: false, reset: false, seed: true, editLock: false, hiddenCounts: false };
    this.base = new URL("../", location.href);
    this.keyMap = new Map();
    this.credential = storage.get("plp.credential") || "";
    this.remember = storage.get("plp.credential") != null;
    this.meta = null;
  }

  async boot(progress) {
    progress("connect", "active");
    const r = await fetch(new URL("console/api-mode", this.base), { cache: "no-store" });
    this.meta = r.ok ? await r.json() : {};
    progress("connect", "done");
    return this.describe();
  }

  describe() {
    return {
      mode: "live",
      label: `Live · connected to ${location.host}`,
      host: location.host,
      version: this.meta?.version || "",
      backend: this.meta?.backend || "",
      modules: [],
    };
  }

  setCredential(value, remember) {
    this.credential = (value || "").trim();
    this.remember = !!remember;
    storage.set("plp.credential", remember && this.credential ? this.credential : null);
  }

  hasCredential() { return !!this.credential; }

  async req(method, path, { body, key, form } = {}) {
    const token = key || this.credential;
    if (!token) return { status: 401, detail: "Sign in on the Settings screen with an admin API key or access token." };
    const headers = { Authorization: `Bearer ${token}` };
    let payload;
    if (form) payload = form;
    else if (body !== undefined) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
    let r;
    try {
      r = await fetch(new URL(path.replace(/^\//, ""), this.base), { method, headers, body: payload });
    } catch (e) {
      return { status: 0, detail: `Network error: ${e.message}` };
    }
    let data = null;
    try { data = await r.json(); } catch { data = null; }
    if (!r.ok) {
      const d = data && data.detail;
      return { status: r.status, detail: typeof d === "string" ? d : d ? JSON.stringify(d) : r.statusText, errors: Array.isArray(d) ? d : undefined };
    }
    if (Array.isArray(data)) return data;
    return { ...(data || {}), status: r.status };
  }

  _keyFor(persona) {
    if (!persona || persona === "self") return this.credential;
    return this.keyMap.get(persona.replace(/^key:/, "")) || "";
  }

  // identity
  async personas() {
    const out = [{ id: "self", name: "You (signed-in credential)", kind: "admin", note: "the admin key or token entered in Settings" }];
    const keys = await this.keys();
    if (!Array.isArray(keys)) return out;
    for (const k of keys) {
      if (k.revoked) continue;
      const role = k.is_admin ? "admin" : "user";
      out.push({ id: `key:${k.id}`, name: k.label, kind: "api_key", note: `API key, ${role}, ${k.groups.length ? k.groups.join(", ") : "no groups"}` });
    }
    return out;
  }
  async whoami(persona, collection) {
    const key = this._keyFor(persona);
    const me = await this.req("GET", "/v1/me", { key });
    if (me.status && me.status >= 400) return me;
    const docs = await this.req("GET", `/v1/collections/${encodeURIComponent(collection)}/documents`, { key });
    const roles = me.roles || [];
    return { ...me, can_chat: roles.includes("user") || roles.includes("admin"), visible: Array.isArray(docs) ? docs.map((d) => d.title) : [] };
  }
  // chat
  async ask(persona, collection, question, o = {}) {
    const key = this._keyFor(persona);
    const body = { question, top_k: o.top_k || 4 };
    if (o.mode) body.retrieval_mode = o.mode;
    if (o.reranker) body.reranker = o.reranker;
    const before = await this.req("GET", "/admin/audit/entries?limit=1");
    const lastLine = before.entries && before.entries.length ? before.entries[0].line : 0;
    const out = await this.req("POST", `/v1/collections/${encodeURIComponent(collection)}/ask`, { key, body });
    // The API never tells a caller what it can't see; the console (as admin) reads the decision from the audit log.
    const audit = await this.req("GET", "/admin/audit/entries?limit=10");
    if (audit.entries) {
      const hit = audit.entries.find((e) => e.line > lastLine && (e.event === "document_question" || e.event === "document_question_denied"));
      if (hit) { out.access = hit.payload.access; out.asked_as = hit.payload.key; out.audit_line = hit.line; }
    }
    return out;
  }
  async chat(persona, prompt) {
    const out = await this.req("POST", "/v1/chat/completions", { key: this._keyFor(persona), body: { messages: [{ role: "user", content: prompt }] } });
    if (out.choices) return { status: 200, content: out.choices[0].message.content, model: out.model, total_tokens: out.usage?.total_tokens };
    return out;
  }
  // overview, audit
  overview() { return this.req("GET", "/admin/overview"); }
  auditEntries(limit = 300) { return this.req("GET", `/admin/audit/entries?limit=${limit}`); }
  verifyAudit() { return this.req("GET", "/admin/audit/verify"); }
  // documents
  async collections() {
    const cols = await this.req("GET", "/v1/collections");
    if (!Array.isArray(cols)) return cols;
    for (const c of cols) {
      const acl = await this.req("GET", `/admin/collections/${encodeURIComponent(c.name)}/acl`);
      c.acl = acl.principals || [];
    }
    return cols;
  }
  documents(c) { return this.req("GET", `/v1/collections/${encodeURIComponent(c)}/documents`); }
  addText(c, title, text, acl) { return this.req("POST", `/v1/collections/${encodeURIComponent(c)}/documents/text`, { body: { title, text, acl: acl || [] } }); }
  uploadFile(c, file, acl) {
    const form = new FormData();
    form.append("file", file, file.name);
    form.append("acl", (acl || []).join(" "));
    return this.req("POST", `/v1/collections/${encodeURIComponent(c)}/documents`, { form });
  }
  removeDocument(c, id) { return this.req("DELETE", `/v1/collections/${encodeURIComponent(c)}/documents/${encodeURIComponent(id)}`); }
  setDocumentAcl(c, id, acl) { return this.req("PUT", `/v1/collections/${encodeURIComponent(c)}/documents/${encodeURIComponent(id)}/acl`, { body: { principals: acl } }); }
  setCollectionAcl(c, acl) { return this.req("PUT", `/admin/collections/${encodeURIComponent(c)}/acl`, { body: { principals: acl } }); }
  accessMatrix(c) { return this.req("GET", `/admin/access-matrix?collection=${encodeURIComponent(c)}`); }
  // keys and users
  async keys() {
    const rows = await this.req("GET", "/admin/keys");
    if (!Array.isArray(rows)) return rows;
    return rows.map((k) => {
      const id = lastN(k.key);
      this.keyMap.set(id, k.key);
      return { id, masked: k.key.slice(0, 12) + "…", label: k.label, is_admin: k.is_admin, groups: k.groups || [], created_at: k.created_at,
        revoked_at: k.revoked_at, revoked: !!k.revoked_at, requests_total: k.requests_total, tokens_total: k.tokens_total };
    });
  }
  async createKey(label, isAdmin, groups) { return this.req("POST", "/admin/keys", { body: { label, is_admin: !!isAdmin, groups: groups || [] } }); }
  async revokeKey(id) {
    const out = await this.req("DELETE", `/admin/keys/${encodeURIComponent(this.keyMap.get(id) || id)}`);
    return { status: out.status, detail: out.detail || out.status };
  }
  async sendWithKey(id) {
    const out = await this.chat(`key:${id}`, "Summarize our travel policy in one sentence.");
    return out.status === 200 ? { status: 200, total_tokens: out.total_tokens } : out;
  }
  users() { return this.req("GET", "/admin/users"); }
  rateLimits() { return this.req("GET", "/admin/rate-limits"); }
  // models
  async models() {
    const [health, served, verification] = await Promise.all([
      fetch(new URL("health", this.base)).then((r) => r.json()).catch(() => ({})),
      this.req("GET", "/v1/models"),
      this.req("GET", "/admin/models/verification"),
    ]);
    const pol = await this.req("GET", "/admin/policy");
    return {
      backend: { name: health.backend || "", healthy: !!health.backend_ok, chat_model: "", embed_model: "" },
      served: served.data ? served.data.map((m) => m.id) : [],
      served_error: served.data ? null : served.detail,
      verification,
      policy: pol.policy ? pol.policy.GATEWAY_MODEL_POLICY : "",
      lock_text: "",
    };
  }
  verifyModels() { return this.req("POST", "/admin/models/verify"); }
  validateLock(text) { return this.req("POST", "/admin/models/lock/validate", { body: { text } }); }
  mlbom() { return this.req("GET", "/admin/models/mlbom"); }
  // policy
  policy() { return this.req("GET", "/admin/policy"); }
  validatePolicy(doc) { return this.req("POST", "/admin/policy/validate", { body: doc }); }
  async applyPolicy(doc) {
    const out = await this.req("PUT", "/admin/policy", { body: doc });
    if (out.errors) return { status: 422, detail: out.errors };
    return out;
  }
  // evals
  async evals() { return JSON.parse(await fetchText(new URL("data/rag_eval.json", location.href))); }
  // first-run sample data, through the real API
  async seedSamples() {
    const m = window.CONSOLE_MANIFEST;
    const results = [];
    for (const name of m.SAMPLES) results.push(await this.addText("policies", name, await fetchText(new URL(name, location.href)), []));
    for (const item of m.RESTRICTED_SAMPLES) {
      results.push(await this.addText("policies", item.file, await fetchText(new URL(item.file, location.href)), item.acl));
    }
    const existing = await this.keys();
    const have = new Set(Array.isArray(existing) ? existing.filter((k) => !k.revoked).map((k) => k.label) : []);
    for (const [label, groups] of [["hr-assistant", ["hr"]], ["eng-assistant", ["engineering"]], ["lobby-kiosk", []]]) {
      if (!have.has(label)) results.push(await this.createKey(label, false, groups));
    }
    const failed = results.find((r) => r.status >= 400);
    return failed || { status: 201, created: results.length };
  }
}
