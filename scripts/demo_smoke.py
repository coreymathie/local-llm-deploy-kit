# Corey Mathie, 2026
"""
Headless smoke test of the console (demo/) in both modes, with Playwright.

    pip install playwright && playwright install chromium
    python scripts/demo_smoke.py                                  # demo mode, Pyodide from cdn.jsdelivr.net
    python scripts/demo_smoke.py --pyodide-dir ./pyodide-0.26.4/pyodide   # offline copy of the Pyodide release
    python scripts/demo_smoke.py --live --skip-demo               # live mode only (no Pyodide needed)
    python scripts/demo_smoke.py --live --screenshots out/        # both, with desktop and mobile screenshots

Demo mode: serves the repo root on a local port (like GitHub Pages does), opens /demo/, waits for Pyodide
and the gateway modules, then visits every screen and performs its key interaction: the guided tour,
overview counters and charts, permission-aware chat (personas, compare, retrieval switches, raw chat 403),
documents (add, untrusted document, access list edit, access matrix), keys (create, burst over the rate
limit, revoke), audit (verify, tamper, restore, decision timeline), models (pin, re-pull mismatch, enforce
403, ML-BOM, lock validation), policies (invalid policy errors, apply, scenario before/after), evals
(scorecard, re-run in the browser matches the committed results) and settings.

Live mode: starts scripts/mock_openai_server.py (MOCK_MODE=simulated) and the gateway with uvicorn on free
ports, opens /console/, signs in with a generated admin key, loads the sample data through the API, and
exercises Overview, Chat and Audit, then visits the other screens.

Both: no console errors, no horizontal page scroll at 390 px. Exits non-zero on any failure.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CDN = "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/"
SCREENS = [
    "overview",
    "overview/session",
    "chat",
    "documents",
    "users",
    "audit",
    "models",
    "policies",
    "evals",
    "settings",
]


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def serve(port: int) -> http.server.ThreadingHTTPServer:
    handler = functools.partial(_Quiet, directory=str(ROOT))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Run:
    def __init__(self, shots: Path | None):
        self.checks: list[str] = []
        self.errors: list[str] = []
        self.shots = shots

    def ok(self, msg: str) -> None:
        self.checks.append(msg)
        print(f"  ok  {msg}")

    def watch(self, page) -> None:
        page.on("console", lambda m: m.type == "error" and self.errors.append(m.text))
        page.on("pageerror", lambda e: self.errors.append(str(e)))

    def shot(self, page, name: str, full: bool = True) -> None:
        if self.shots:
            self.shots.mkdir(parents=True, exist_ok=True)
            page.evaluate("window.scrollTo(0, 0); document.getElementById('toasts').replaceChildren()")
            page.screenshot(path=str(self.shots / f"{name}.png"), full_page=full)


def goto(page, screen: str) -> None:
    page.evaluate(f"location.hash = '#/{screen}'")
    base = screen.split("/")[0]
    page.wait_for_selector(f'#view > [data-screen="{base}"][data-loaded="true"]', timeout=60_000)


def no_overflow(page) -> int:
    return page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")


def mobile_pass(run: Run, page, prefix: str) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    for s in SCREENS:
        goto(page, s)
        page.wait_for_timeout(150)
        overflow = no_overflow(page)
        assert overflow <= 0, f"{prefix} {s}: horizontal scroll at 390px ({overflow}px)"
        if s in ("overview", "chat", "audit"):
            run.shot(page, f"{prefix}-mobile-{s}")
    run.ok(f"{prefix}: no horizontal page scroll at 390 px on all {len(SCREENS)} screens")
    page.click("#menuBtn")
    page.wait_for_selector("#sidebar.open")
    assert page.locator("#nav a").first.is_visible()
    run.shot(page, f"{prefix}-mobile-menu", full=False)
    page.keyboard.press("Escape")
    run.ok(f"{prefix}: navigation collapses to a menu button on phones")
    page.set_viewport_size({"width": 1366, "height": 900})


# ------------------------------------------------------------------------------------------------
# Demo mode
# ------------------------------------------------------------------------------------------------


def demo(run: Run, p, port: int, pyodide_dir: Path | None) -> None:
    from playwright.sync_api import expect

    httpd = serve(port)
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1366, "height": 900})
    run.watch(page)
    if pyodide_dir:
        local = pyodide_dir.resolve()

        def fulfill(route):
            name = route.request.url.split(CDN, 1)[1].split("?", 1)[0]
            f = local / name
            if f.is_file():
                ctype = "application/wasm" if name.endswith(".wasm") else None
                route.fulfill(path=str(f), content_type=ctype, headers={"Access-Control-Allow-Origin": "*"})
            else:
                route.fulfill(status=404, body=f"not in local pyodide dir: {name}")

        page.route(f"{CDN}**", fulfill)

    started = time.time()
    page.goto(f"http://127.0.0.1:{port}/demo/")
    page.wait_for_function("document.body.dataset.ready", timeout=240_000)
    assert page.evaluate("document.body.dataset.ready") == "true", page.inner_text("#bootNote")
    run.ok(f"demo: Pyodide loaded, gateway modules fetched, samples ingested ({time.time() - started:.0f} s)")
    expect(page.locator("#modeBadge")).to_contain_text("Demo")
    expect(page.locator("#modeBadge")).to_have_class(re.compile("demo"))
    run.ok("demo: header badge says Demo · runs in your browser")

    # Guided tour (first visit), dismissal remembered
    expect(page.locator("#tourCard")).to_be_visible()
    for _ in range(4):
        page.click("#tourNext")
    expect(page.locator("#tourCard")).to_contain_text("Step 5 of 5")
    page.click("#tourNext")
    expect(page.locator("#tourCard")).to_be_hidden()
    assert page.evaluate("localStorage.getItem('plp.tourDone')") == "1"
    run.ok("demo: 5-step guided tour shown on first visit; dismissal stored")

    # 1. Overview: business impact for the sample company, then this session
    goto(page, "overview")
    page.wait_for_selector("#bizVolume svg.chart")
    expect(page.locator(".banner.sample")).to_contain_text("fictional")
    assert page.locator(".kpi.sample").count() == 8, page.locator(".kpi.sample").count()
    assert page.locator("table.depts tbody tr").count() == 10
    expect(page.locator("#pageTitle .crumbs")).to_contain_text("Monitor")
    before = page.inner_text(".kpi.sample .value")
    page.click("[data-range='7']")
    page.wait_for_selector("[data-range='7'][aria-pressed='true']")
    assert page.inner_text(".kpi.sample .value") != before
    page.click("[data-range='30']")
    page.wait_for_selector("[data-range='30'][aria-pressed='true']")
    run.ok("demo overview: business impact for the sample company (8 KPIs, daily chart, departments, range switch)")
    run.shot(page, "demo-overview-business")
    page.keyboard.press("Control+k")
    page.wait_for_selector("#palette:not([hidden])")
    page.fill("#palette-input", "audit")
    page.keyboard.press("Enter")
    page.wait_for_selector('#view > [data-screen="audit"][data-loaded="true"]')
    assert page.locator("#palette").is_hidden()
    page.keyboard.press("g")
    page.keyboard.press("e")
    page.wait_for_selector('#view > [data-screen="evals"][data-loaded="true"]')
    page.keyboard.press("?")
    expect(page.locator("#keys")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#keys")).to_be_hidden()
    page.click("#nav-collapse")
    assert page.evaluate("document.body.classList.contains('nav-collapsed')")
    page.click("#nav-collapse")
    run.ok("demo navigation: command palette, g + letter shortcuts, shortcuts sheet, collapsible sidebar")
    goto(page, "overview/session")
    expect(page.locator("#nav a[data-sub='session']")).to_have_attribute("aria-current", "page")
    assert int(page.inner_text('[data-kpi="requests"]').replace(",", "")) >= 5
    expect(page.locator('[data-kpi="chain"]')).to_have_text("Intact")
    assert page.locator("#view svg.chart").count() == 2 and page.locator(".try").count() == 5
    run.ok("demo overview: KPIs from the engine, chain intact, 2 charts, 5 guided cards")
    run.shot(page, "demo-overview")

    # 2. Chat: personas, citations, compare, retrieval switches, raw chat
    goto(page, "chat")
    page.select_option("#persona", "priya")
    expect(page.locator("#who")).to_contain_text("user:priya")
    page.click('#suggest button[data-q="What is the level 3 salary band?"]')
    last = page.locator("#thread .msg.bot").last
    expect(last.locator(".answer")).to_contain_text("$77,000")
    first = page.locator("#sources .source").first
    expect(first).to_contain_text("restricted-hr-compensation-bands.md")
    expect(first.locator(".pill.ok")).to_have_text("cited")
    last.locator(".cite").first.click()
    expect(page.locator("#sources .source.hl")).to_have_count(1)
    run.ok("demo chat: HR user gets the salary band from the HR-only document, cited; citation opens the source")
    page.select_option("#persona", "dana")
    expect(page.locator("#who")).to_contain_text("user:dana")
    page.fill("#prompt", "What is the level 3 salary band?")
    page.click("#send")
    expect(page.locator("#thread .msg.bot").last.locator(".meta")).to_contain_text("user:dana")
    assert "77,000" not in page.locator("#thread .msg.bot").last.inner_text()
    assert "compensation" not in page.inner_text("#sources")
    run.ok("demo chat: engineering user asking the same question gets no HR passage")
    page.check("#compare")
    page.select_option("#persona", "priya")
    page.select_option("#persona2", "dana")
    page.fill("#prompt", "What is the level 3 salary band?")
    page.click("#send")
    cols = page.locator("#thread .msg.bot").last.locator(".compare > div")
    expect(cols).to_have_count(2)
    expect(cols.nth(0)).to_contain_text("$77,000")
    assert "77,000" not in cols.nth(1).inner_text()
    run.shot(page, "console", full=False)  # README image (docs/img/console.png)
    page.uncheck("#compare")
    run.ok("demo chat: side-by-side compare shows two different permission-aware answers")
    page.select_option("#persona", "admin")
    page.select_option("#mode", "bm25")
    page.check("#rerank")
    page.click('#suggest button[data-q="What is the hotel cap per night?"]')
    expect(page.locator("#thread .msg.bot").last.locator(".meta")).to_contain_text("bm25 + lexical")
    expect(page.locator("#sources .source").first).to_contain_text("sample-travel-expense-policy.md")
    assert "vector" not in page.inner_text("#sources")  # BM25 mode doesn't embed the question
    page.select_option("#mode", "hybrid")
    page.uncheck("#rerank")
    page.click('#suggest button[data-q="What is the hotel cap per night?"]')
    expect(page.locator("#thread .msg.bot").last.locator(".meta")).to_contain_text("hybrid")
    expect(page.locator("#sources .source").first).to_contain_text("rrf")
    run.ok("demo chat: per-request retrieval switches (bm25 + lexical reranker, hybrid with RRF)")
    page.select_option("#persona", "audrey")
    page.select_option("#kind", "model")
    page.fill("#prompt", "hello")
    page.click("#send")
    expect(page.locator("#thread .msg.bot").last).to_contain_text("403")
    page.select_option("#kind", "rag")
    run.ok("demo chat: collection reader can't use raw chat (403)")
    run.shot(page, "demo-chat")

    # 3. Documents: add, untrusted document, access list, matrix
    goto(page, "documents")
    page.fill("#addTitle", "parking-policy.md")
    page.fill("#addText", "Parking\n\nEmployees park in garage B. Visitor parking needs a pass from reception.")
    page.click("#addForm button[type=submit]")
    expect(page.locator("#docTable")).to_contain_text("parking-policy.md")
    page.fill("#addTitle", '<img id="xss" src="x">.md')
    page.fill("#addText", "Markup in a title must render as text.")
    page.click("#addForm button[type=submit]")
    expect(page.locator("#docTable")).to_contain_text('<img id="xss" src="x">.md')
    assert page.locator("#xss").count() == 0
    page.once("dialog", lambda d: d.accept())
    page.locator("#docTable tr", has_text="xss").locator('button[data-act="del"]').click()
    expect(page.locator("#docTable")).not_to_contain_text("xss")
    page.click("#poisonBtn")
    expect(page.locator("#docTable")).to_contain_text("vendor-notes-UNTRUSTED.md")
    run.ok("demo documents: pasted, HTML-titled (rendered as text) and untrusted documents added")
    row = page.locator("#docTable tr", has_text="sample-travel-expense-policy.md")
    row.locator('button[data-act="acl"]').click()
    page.fill("#aclInput", "group:finance")
    page.click("#aclForm button[type=submit]")
    expect(page.locator("#docTable tr", has_text="sample-travel-expense-policy.md")).to_contain_text("group:finance")
    kiosk = page.locator("#matrix tr", has_text="Branch lobby kiosk")
    titles = page.locator("#matrix th.doc").all_inner_texts()
    col = titles.index("sample-travel-expense-policy.md")
    expect(kiosk.locator("td.cell").nth(col + 1)).to_have_attribute("aria-label", "cannot read")
    run.ok("demo documents: access list edited; the access matrix hides the document from the kiosk key")
    row = page.locator("#docTable tr", has_text="sample-travel-expense-policy.md")
    row.locator('button[data-act="acl"]').click()
    page.fill("#aclInput", "not-an-entry")
    page.click("#aclForm button[type=submit]")
    expect(page.locator("#aclErr")).to_contain_text("invalid access-list entry")
    page.fill("#aclInput", "")
    page.click("#aclForm button[type=submit]")
    expect(page.locator("#docTable tr", has_text="sample-travel-expense-policy.md")).to_contain_text("inherits")
    run.ok("demo documents: invalid access-list entry rejected by identity.check_acl; list cleared again")
    run.shot(page, "demo-documents")
    goto(page, "chat")
    page.select_option("#persona", "admin")
    page.click('#suggest button[data-q="What is the hotel cap per night?"]')
    expect(page.locator("#sources")).to_contain_text("vendor-notes-UNTRUSTED.md")
    expect(page.locator("#sources .pill.bad").first).to_contain_text("injection")
    answer = page.locator("#thread .msg.bot").last.locator("[data-answer]").inner_text().lower()
    assert "ignore all previous" not in answer and "admin key" not in answer, answer
    expect(page.locator("#thread .msg.bot").last.locator(".banner.warn")).to_contain_text("prompt injection")
    run.ok("demo chat: untrusted document retrieved, flagged, its instruction not repeated")

    # 4. Users & Keys: create, burst over the limit, revoke
    goto(page, "users")
    page.fill("#keyLabel", "smoke-app")
    page.fill("#keyGroups", "hr")
    page.click("#keyForm button[type=submit]")
    expect(page.locator("#newKeyValue")).to_contain_text("sk-local-")
    expect(page.locator('#keysTable tr[data-label="smoke-app"]')).to_contain_text("group:hr")
    run.ok("demo keys: key created with a group; value shown once")
    page.locator('#keysTable tr[data-label="billing-app"] button[data-act="burst"]').click()
    expect(page.locator("#sendLog .pill.warn").first).to_have_text("429")
    statuses = page.locator("#sendLog .pill").all_inner_texts()
    assert statuses.count("200") == 30 and statuses.count("429") == 2, statuses
    run.ok("demo keys: burst of 32 at 30/min: 30 x 200, 2 x 429 (auth._rate_limit)")
    page.once("dialog", lambda d: d.accept())
    page.locator('#keysTable tr[data-label="billing-app"] button[data-act="revoke"]').click()
    expect(page.locator('#keysTable tr[data-label="billing-app"] .pill.bad')).to_have_text("revoked")
    run.ok("demo keys: key revoked")
    run.shot(page, "demo-users")

    # 5. Audit: verify, tamper, restore, timeline
    goto(page, "audit")
    page.click("#verifyBtn")
    expect(page.locator("#verifyOut")).to_contain_text("Chain intact")
    page.select_option("#tamperLine", "2")
    page.select_option("#tamperMode", "edit")
    page.click("#tamperBtn")
    expect(page.locator("#verifyOut")).to_contain_text("Tampering detected at line 2")
    page.click("#restoreBtn")
    expect(page.locator("#verifyOut")).to_contain_text("Chain intact")
    page.select_option("#tamperLine", "2")
    page.select_option("#tamperMode", "edit_rehash")
    page.click("#tamperBtn")
    expect(page.locator("#verifyOut")).to_contain_text("Tampering detected at line 3")
    page.click("#restoreBtn")
    expect(page.locator("#verifyOut")).to_contain_text("Chain intact")
    run.ok("demo audit: edit at line 2 -> line 2; edit + rehash -> caught at line 3; restore -> intact")
    events = page.locator("#auditBody td:nth-child(3)").all_inner_texts()
    assert {"document_question", "key_revoked", "document_acl_changed", "completion"} <= set(events), set(events)
    assert "access=" in page.inner_text("#auditBody")
    page.select_option("#fEvent", "document_question")
    page.fill("#fSearch", "priya")
    expect(page.locator("#auditCount")).not_to_contain_text("0 of")
    page.locator("#auditBody tr").first.click()
    expect(page.locator("#drawer")).to_have_class(re.compile("open"))
    expect(page.locator("#drawerBody")).to_contain_text("Access decision")
    expect(page.locator("#drawerBody")).to_contain_text("Hash chain")
    expect(page.locator("#drawerBody")).to_contain_text("restricted-hr-compensation-bands.md")
    page.wait_for_timeout(350)  # drawer slide-in
    run.shot(page, "demo-audit-drawer", full=False)
    page.click("#drawerClose")
    run.ok("demo audit: filters and search; row opens a decision timeline with cited documents and hashes")

    # 6. Models: pin, re-pull, enforce, ML-BOM, lock validation
    goto(page, "models")
    page.click("#pinBtn")
    expect(page.locator("#scStatus")).to_have_text("Verified")
    page.click("#repullBtn")
    expect(page.locator('#modelTable tr[data-model="llama3.1:8b"]')).to_contain_text("mismatch")
    page.click('#policySeg [data-pol="enforce"]')
    expect(page.locator('#policySeg [data-pol="enforce"]')).to_have_attribute("aria-pressed", "true")
    page.click("#tryModel")
    expect(page.locator("#tryOut")).to_contain_text("403")
    page.click('#policySeg [data-pol="warn"]')
    expect(page.locator('#policySeg [data-pol="warn"]')).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#mo")).to_contain_text("machine-learning-model")
    page.fill("#lockText", '{"version": 1, "models": [{"name": "x", "digest": "sha256:nothex"}]}')
    page.click("#lockValidate")
    expect(page.locator("#lockOut")).to_contain_text("digest must be a SHA-256 hex string")
    run.ok("demo models: pin -> verified; re-pull -> mismatch; enforce -> 403; ML-BOM; lock validation error")
    run.shot(page, "demo-models")

    # 7. Policies: invalid policy, apply, scenario before/after
    goto(page, "policies")
    page.select_option("#scSel", "audrey-chat")
    page.click("#scRun")
    expect(page.locator("#scBefore")).to_contain_text("403")
    original = page.input_value("#polText")
    page.fill("#polText", original.replace('"auditors": ["reader:policies"]', '"auditors": ["superuser"]'))
    page.click("#polValidate")
    expect(page.locator("#polErrList")).to_contain_text("unknown role 'superuser'")
    granted = '"auditors": ["reader:policies", "user"]'
    page.fill("#polText", original.replace('"auditors": ["reader:policies"]', granted))
    page.click("#polApply")
    expect(page.locator("#polOk")).to_contain_text("GATEWAY_OIDC_GROUP_ROLES")
    expect(page.locator("#scAfter")).to_contain_text("200")
    run.ok("demo policies: bad role rejected inline; applied mapping turns the auditor's 403 into 200")
    run.shot(page, "demo-policies")
    page.fill("#polText", original)
    page.click("#polApply")
    expect(page.locator("#polOk")).to_be_visible()

    # 8. Evals
    goto(page, "evals")
    expect(page.locator("#gate")).to_have_text("Pass")
    assert page.locator("#scorecard tbody tr").count() == 4
    page.click('#scorecard tr[data-config="bm25+none"]')
    expect(page.locator("#view")).to_contain_text("Misses · bm25+none")
    page.click("#runEval")
    expect(page.locator("#evalMatch")).to_contain_text("Identical", timeout=180_000)
    run.ok("demo evals: gate passes; scripts/rag_eval.py re-run in the browser matches the committed results")
    run.shot(page, "demo-evals")

    # 9. Settings
    goto(page, "settings")
    assert page.locator(".modules .module").count() >= 15
    expect(page.locator("#view")).to_contain_text("What is simulated")
    run.ok("demo settings: modules running in the tab listed with hashes; simulated parts listed")

    # Back button and deep links
    page.go_back()
    page.wait_for_selector('#view > [data-screen="evals"][data-loaded="true"]')
    run.ok("demo: browser back returns to the previous screen")

    mobile_pass(run, page, "demo")
    browser.close()
    httpd.shutdown()


# ------------------------------------------------------------------------------------------------
# Live mode
# ------------------------------------------------------------------------------------------------


def _wait_http(url: str, proc: subprocess.Popen, timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"process exited: {proc.args}")
        try:
            with urllib.request.urlopen(url, timeout=2):
                return
        except OSError:
            time.sleep(0.3)
    raise RuntimeError(f"timed out waiting for {url}")


def live(run: Run, p) -> None:
    from playwright.sync_api import expect

    mock_port, gw_port = free_port(), free_port()
    admin_key = "sk-local-" + secrets.token_urlsafe(24)
    tmp = tempfile.mkdtemp(prefix="plp-live-")
    env = {
        **os.environ,
        "MOCK_MODE": "simulated",
        "BACKEND": "openai_compatible",
        "OPENAI_COMPAT_BASE_URL": f"http://127.0.0.1:{mock_port}/v1",
        "GATEWAY_DEFAULT_MODEL": "mock-model",
        "GATEWAY_EMBED_MODEL": "mock-embed",
        "GATEWAY_DB_PATH": f"{tmp}/gateway.db",
        "GATEWAY_LOG_DIR": f"{tmp}/logs",
        "GATEWAY_ADMIN_BOOTSTRAP_KEY": admin_key,
        # The smoke test drives every screen (twice, at two widths) inside a minute with one admin key,
        # far faster than a person; the default 60/min would turn the last screens into 429s.
        "GATEWAY_RATE_LIMIT_PER_MIN": "240",
    }
    uv = [sys.executable, "-m", "uvicorn", "--host", "127.0.0.1", "--log-level", "warning"]
    procs = [
        subprocess.Popen([*uv, "--port", str(mock_port), "scripts.mock_openai_server:app"], cwd=ROOT, env=env),
        subprocess.Popen([*uv, "--port", str(gw_port), "gateway.main:app"], cwd=ROOT, env=env),
    ]
    try:
        _wait_http(f"http://127.0.0.1:{mock_port}/v1/models", procs[0])
        _wait_http(f"http://127.0.0.1:{gw_port}/health", procs[1])
        run.ok("live: mock backend (simulated mode) and gateway started with uvicorn")
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1366, "height": 900})
        run.watch(page)
        page.goto(f"http://127.0.0.1:{gw_port}/console/?tour=0")
        page.wait_for_function("document.body.dataset.ready", timeout=60_000)
        assert page.evaluate("document.body.dataset.ready") == "true"
        expect(page.locator("#modeBadge")).to_contain_text("Live")
        expect(page.locator("#modeBadge")).to_have_class(re.compile("live"))
        page.wait_for_selector('#view > [data-screen="settings"][data-loaded="true"]')
        run.ok("live: /console served by the gateway, detected live mode, asks for a credential")
        page.fill("#cred", admin_key)
        page.click("#credForm button[type=submit]")
        expect(page.locator("#credOk")).to_contain_text("bootstrap admin")
        run.ok("live settings: signed in with the admin key (GET /v1/me)")

        goto(page, "overview")
        page.wait_for_selector("#bizVolume svg.chart")
        run.ok("live overview: business impact loads the sample company file")
        goto(page, "overview/session")
        page.click("#seedBtn")
        expect(page.locator('[data-kpi="documents"]')).to_have_text("13")
        expect(page.locator('[data-kpi="chain"]')).to_have_text("Intact")
        run.ok("live overview: sample documents and keys loaded through the API; counters from /admin/overview")
        run.shot(page, "live-overview")

        goto(page, "chat")
        page.select_option("#persona", label="hr-assistant")
        expect(page.locator("#who")).to_contain_text("hr-assistant")
        page.click('#suggest button[data-q="What is the level 3 salary band?"]')
        last = page.locator("#thread .msg.bot").last
        expect(last.locator(".answer")).to_contain_text("77,000")
        expect(page.locator("#sources .source").first).to_contain_text("restricted-hr-compensation-bands.md")
        expect(last.locator(".meta")).to_contain_text("audit line")
        expect(last.locator(".meta")).to_contain_text("allow")
        run.ok("live chat: key with group hr gets the HR document through the gateway and mock backend, cited")
        page.select_option("#persona", label="eng-assistant")
        page.fill("#prompt", "What is the level 3 salary band?")
        page.click("#send")
        expect(page.locator("#thread .msg.bot").last.locator(".meta")).to_contain_text("eng-assistant")
        assert "77,000" not in page.locator("#thread .msg.bot").last.inner_text()
        run.ok("live chat: engineering key asking the same question gets no HR passage")
        run.shot(page, "live-chat")

        goto(page, "audit")
        page.click("#verifyBtn")
        expect(page.locator("#verifyOut")).to_contain_text("Chain intact")
        assert "document_question" in page.inner_text("#auditBody")
        assert page.locator("#tamperBtn").count() == 0  # live mode never edits the log
        page.select_option("#fEvent", "document_question")
        page.locator("#auditBody tr").first.click()
        expect(page.locator("#drawerBody")).to_contain_text("Access decision")
        expect(page.locator("#drawerBody")).to_contain_text("not logged")
        page.click("#drawerClose")
        run.ok("live audit: chain verified, questions listed, decision timeline opens; no tamper controls")
        run.shot(page, "live-audit")

        for s in ("documents", "users", "models", "policies", "evals", "settings"):
            goto(page, s)
            assert page.locator("#view .error-state").count() == 0, f"live {s}: {page.inner_text('#view')[:300]}"
        expect(page.locator("#view")).to_contain_text("Credential")
        run.ok("live: documents, users, models, policies, evals and settings render without errors")
        mobile_pass(run, page, "live")
        browser.close()
    finally:
        for proc in procs:
            proc.terminate()
            proc.wait(timeout=10)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--pyodide-dir", type=Path, default=None, help="serve Pyodide from a local release folder")
    ap.add_argument("--screenshots", type=Path, default=None, help="folder for desktop/mobile screenshots")
    ap.add_argument("--live", action="store_true", help="also run the live-mode smoke (gateway + mock backend)")
    ap.add_argument("--skip-demo", action="store_true", help="skip the demo-mode (Pyodide) smoke")
    args = ap.parse_args(argv)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed; skipping (pip install playwright && playwright install chromium)")
        return 0

    run = Run(args.screenshots)
    with sync_playwright() as p:
        if not args.skip_demo:
            demo(run, p, args.port, args.pyodide_dir)
        if args.live:
            live(run, p)

    real_errors = [e for e in run.errors if not re.search(r"favicon", e)]
    if real_errors:
        print("console errors:", *real_errors, sep="\n  ")
        return 1
    print(f"\n{len(run.checks)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
