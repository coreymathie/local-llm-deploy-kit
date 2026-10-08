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
import json
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
    run.ok("demo: header badge says Demo")

    # Guided tour: offered on the first visit, never forced; each step opens the screen it describes
    expect(page.locator("#tourInvite")).to_be_visible()
    expect(page.locator("#tourCard")).to_be_hidden()
    page.click("#tourInviteStart")
    expect(page.locator("#tourCard")).to_contain_text("Step 1 of 5")
    page.click("#tourNext")
    page.click("#tourNext")
    expect(page.locator("#tourCard")).to_contain_text("Step 3 of 5")
    page.wait_for_selector('#view > [data-screen="documents"][data-loaded="true"]')
    page.click("#tourSkip")
    expect(page.locator("#tourCard")).to_be_hidden()
    assert page.evaluate("localStorage.getItem('plp.tourDone')") == "1"
    run.ok("demo: tour offered, steps open the screens they describe, dismissal stored")

    # Business and technical views
    first_tech = page.locator("#view .tech-only").first
    expect(first_tech).to_be_hidden()
    page.click("[data-view='technical']")
    page.wait_for_selector('#view > [data-screen="documents"][data-loaded="true"]')
    expect(page.locator("#view .tech-only").first).to_be_visible()
    assert page.evaluate("localStorage.getItem('plp.view')") == "technical"
    page.click("[data-view='business']")
    page.click("#themeBtn")
    assert page.evaluate("document.documentElement.dataset.theme") in ("dark", "light")
    page.click("#themeBtn")
    run.ok("demo: business view hides keys, scores and code paths; technical view shows them; theme toggle")

    # 1. Overview: business impact for the sample company, then this session
    goto(page, "overview")
    page.wait_for_selector("#bizVolume svg.chart")
    expect(page.locator(".banner.sample")).to_contain_text("fictional")
    assert page.locator(".kpi.sample").count() == 8, page.locator(".kpi.sample").count()
    assert page.locator("table.depts tbody tr").count() == 10
    expect(page.locator("#pageTitle .crumbs")).to_contain_text("Admin")
    before = page.inner_text(".kpi.sample .value")
    page.click("[data-range='7']")
    page.wait_for_selector("[data-range='7'][aria-pressed='true']")
    assert page.inner_text(".kpi.sample .value") != before
    page.click("[data-range='30']")
    page.wait_for_selector("[data-range='30'][aria-pressed='true']")
    run.ok("demo overview: business impact for the sample company (8 KPIs, daily chart, departments, range switch)")
    run.shot(page, "demo-overview-business")
    run.shot(page, "console-business", full=False)  # README image (docs/img/console-business.png)
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
    expect(page.locator('[data-kpi="documents"]')).to_have_text("57")
    assert page.locator("#view svg.chart").count() == 2 and page.locator(".try").count() == 5
    run.ok("demo overview: KPIs from the engine (57 documents), chain intact, 2 charts, 5 guided cards")
    run.shot(page, "demo-overview")

    # 2. The assistant (business view): suggestions, citations, the source pane, refusals, compare, history
    goto(page, "chat")
    page.select_option("#persona", "priya")
    expect(page.locator(".hero h1")).to_contain_text("Priya")
    assert page.locator(".hero .sugg").count() == 4
    page.click('.hero .sugg[data-q="What is the level 3 salary band?"]')
    last = page.locator("#thread .msg.bot").last
    expect(last.locator(".msg-actions")).to_be_visible()
    expect(last.locator(".answer")).to_contain_text("$77,000")
    assert "Level 1" not in last.locator(".answer").inner_text()
    expect(last.locator(".src-chip")).to_contain_text("Compensation Bands 2026")
    last.locator(".cite").first.click()
    expect(page.locator("#pane")).to_contain_text("Compensation Bands 2026")
    expect(page.locator("#pane .passage mark")).to_contain_text("Level 3 salary band")
    run.ok("demo assistant: HR user gets the level 3 band only, cited; the citation opens the passage, highlighted")
    page.fill("#prompt", "What is our CEO's name?")
    page.keyboard.press("Enter")
    expect(page.locator("#thread .msg.bot").last.locator(".answer.not-found")).to_be_visible()
    run.ok("demo assistant: an off-topic question is declined instead of quoting an unrelated passage")
    page.select_option("#persona", "dana")
    page.fill("#prompt", "What is the level 3 salary band?")
    page.keyboard.press("Enter")
    expect(page.locator("#thread .msg.bot").last.locator(".msg-actions, .answer.not-found").first).to_be_visible()
    assert "77,000" not in page.locator("#thread .msg.bot").last.inner_text()
    run.ok("demo assistant: engineering user asking the same question gets no HR passage")
    page.check("#compare")
    page.select_option("#persona", "priya")
    page.select_option("#persona2", "dana")
    page.fill("#prompt", "What is the level 3 salary band?")
    page.keyboard.press("Enter")
    cols = page.locator("#thread .msg.bot").last.locator(".cmp-col")
    expect(cols).to_have_count(2)
    expect(cols.nth(0)).to_contain_text("$77,000")
    page.wait_for_timeout(600)
    assert "77,000" not in cols.nth(1).inner_text()
    page.locator("#thread .msg.bot").last.locator('[data-act="up"]').click()
    expect(page.locator("#thread .msg.bot").last.locator('[data-act="up"]')).to_have_attribute("aria-pressed", "true")
    page.uncheck("#compare")
    run.ok("demo assistant: side-by-side compare shows two different permission-aware answers; feedback")
    page.click("#newChat")
    expect(page.locator(".hero")).to_be_visible()
    page.locator(".hist-list button", has_text="What is the level 3 salary band").first.click()
    expect(page.locator("#thread .msg.bot")).to_have_count(4)
    run.ok("demo assistant: new conversation, and the earlier one reopens from the history")
    run.shot(page, "demo-chat")
    # README image (docs/img/console.png): one comparison in a fresh conversation, the cited source open
    page.click("#newChat")
    page.check("#compare")
    page.select_option("#persona", "priya")
    page.select_option("#persona2", "dana")
    page.fill("#prompt", "What is the level 3 salary band?")
    page.keyboard.press("Enter")
    bot = page.locator("#thread .msg.bot").last
    expect(bot.locator(".msg-actions")).to_be_visible()
    bot.locator(".src-chip").first.click()
    expect(page.locator("#pane")).to_contain_text("Compensation Bands 2026")
    run.shot(page, "console", full=False)
    page.uncheck("#compare")

    # The rest runs in the technical view, which shows the engineering controls
    page.click("[data-view='technical']")
    page.wait_for_selector('#view > [data-screen="chat"][data-loaded="true"]')
    page.click("#newChat")
    page.click(".advanced summary")
    page.select_option("#persona", "admin")
    page.select_option("#mode", "bm25")
    page.check("#rerank")
    page.fill("#prompt", "What is the hotel cap per night?")
    page.keyboard.press("Enter")
    det = page.locator("#thread .msg.bot").last.locator(".answer-details")
    expect(det.locator(".meta")).to_contain_text("bm25 + lexical")
    expect(det).to_contain_text("sample-travel-expense-policy.md")
    assert "vector" not in det.inner_text()  # BM25 mode doesn't embed the question
    page.select_option("#mode", "hybrid")
    page.uncheck("#rerank")
    page.fill("#prompt", "What is the hotel cap per night?")
    page.keyboard.press("Enter")
    det = page.locator("#thread .msg.bot").last.locator(".answer-details")
    expect(det.locator(".meta")).to_contain_text("hybrid")
    expect(det).to_contain_text("rrf")
    run.ok("demo assistant (technical view): per-request retrieval switches and scores")
    page.select_option("#persona", "audrey")
    page.select_option("#kind", "model")
    page.fill("#prompt", "hello")
    page.keyboard.press("Enter")
    expect(page.locator("#thread .msg.bot").last).to_contain_text("not free-form chat")
    page.select_option("#kind", "rag")
    run.ok("demo assistant: collection reader can't use raw chat (403, explained)")

    # 3. Documents: add, untrusted document, access list, matrix
    goto(page, "documents")
    expect(page.locator("#docTable")).to_contain_text("Compensation Bands 2026")
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
    page.fill("#docFilter", "travel")
    expect(page.locator("#docTable tbody tr:not([hidden])")).to_have_count(1)
    page.fill("#docFilter", "")
    run.ok("demo documents: titles, owners and review dates; added, HTML-titled and untrusted documents; filter")
    travel = "Travel and Expense Policy"
    page.locator("#docTable tr", has_text=travel).locator('button[data-act="acl"]').click()
    page.fill("#aclInput", "group:finance")
    page.click("#aclForm button[type=submit]")
    expect(page.locator("#docTable tr", has_text=travel)).to_contain_text("group:finance")
    kiosk = page.locator("#matrix tr", has_text="Branch lobby kiosk")
    titles = page.locator("#matrix th.doc").all_inner_texts()
    col = titles.index(travel)
    expect(kiosk.locator("td.cell").nth(col + 1)).to_have_attribute("aria-label", "cannot read")
    run.ok("demo documents: access list edited; the access matrix hides the document from the kiosk key")
    page.locator("#docTable tr", has_text=travel).locator('button[data-act="acl"]').click()
    page.fill("#aclInput", "not-an-entry")
    page.click("#aclForm button[type=submit]")
    expect(page.locator("#aclErr")).to_contain_text("invalid access-list entry")
    page.fill("#aclInput", "")
    page.click("#aclForm button[type=submit]")
    expect(page.locator("#docTable tr", has_text=travel)).to_contain_text("Same as the collection")
    run.ok("demo documents: invalid access-list entry rejected by identity.check_acl; list cleared again")
    run.shot(page, "demo-documents")
    goto(page, "chat")
    page.select_option("#persona", "admin")
    page.fill("#prompt", "What is the hotel cap per night?")
    page.keyboard.press("Enter")
    msg = page.locator("#thread .msg.bot").last
    expect(msg.locator(".answer-details")).to_contain_text("vendor-notes-UNTRUSTED.md")
    expect(msg.locator(".answer-details .pill.bad").first).to_contain_text("injection")
    answer = msg.locator("[data-answer]").inner_text().lower()
    assert "ignore all previous" not in answer and "admin key" not in answer, answer
    expect(msg.locator(".banner.warn")).to_contain_text("not followed")
    run.ok("demo assistant: untrusted document retrieved, flagged, its instruction not repeated")

    # 4. People and keys: create, burst over the limit, revoke
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

    # 5. Audit log: verify, tamper, restore, timeline, paging
    goto(page, "audit")
    page.click("#verifyBtn")
    expect(page.locator("#verifyOut")).to_contain_text("Log intact")
    page.select_option("#tamperLine", "2")
    page.select_option("#tamperMode", "edit")
    page.click("#tamperBtn")
    expect(page.locator("#verifyOut")).to_contain_text("Tampering detected at line 2")
    page.click("#restoreBtn")
    expect(page.locator("#verifyOut")).to_contain_text("Log intact")
    page.select_option("#tamperLine", "2")
    page.select_option("#tamperMode", "edit_rehash")
    page.click("#tamperBtn")
    expect(page.locator("#verifyOut")).to_contain_text("Tampering detected at line 3")
    page.click("#restoreBtn")
    expect(page.locator("#verifyOut")).to_contain_text("Log intact")
    run.ok("demo audit: edit at line 2 -> line 2; edit + rehash -> caught at line 3; restore -> intact")
    events = set(page.locator("#fEvent option").evaluate_all("os => os.map((o) => o.value)"))
    assert {"document_question", "key_revoked", "document_acl_changed", "completion"} <= events, events
    assert page.locator("#auditBody tr").count() == 25
    page.click("#auditMore")
    assert page.locator("#auditBody tr").count() == 50
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
    run.ok("demo audit: friendly event names, paging, filters and search; row opens a decision timeline")

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
    expect(page.locator("#view")).to_contain_text("not answered perfectly · bm25+none")
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
        catalog = json.loads((ROOT / "demo" / "data" / "library.json").read_text())
        expect(page.locator('[data-kpi="documents"]')).to_have_text(str(len(catalog["documents"])), timeout=60_000)
        expect(page.locator('[data-kpi="chain"]')).to_have_text("Intact")
        run.ok("live overview: sample library and keys loaded through the API; counters from /admin/overview")
        run.shot(page, "live-overview")

        page.click("[data-view='technical']")
        goto(page, "chat")
        page.select_option("#persona", label="hr-assistant")
        page.fill("#prompt", "What is the level 3 salary band?")
        page.keyboard.press("Enter")
        last = page.locator("#thread .msg.bot").last
        expect(last.locator("[data-answer]")).to_contain_text("77,000", timeout=30_000)
        expect(last.locator(".src-chip").first).to_contain_text("Compensation Bands")
        meta = last.locator(".answer-details .meta")
        expect(meta).to_contain_text("audit line")
        expect(meta).to_contain_text("allow")
        run.ok("live chat: key with group hr gets the HR document through the gateway and mock backend, cited")
        page.select_option("#persona", label="eng-assistant")
        page.fill("#prompt", "What is the level 3 salary band?")
        page.keyboard.press("Enter")
        expect(page.locator("#thread .msg.bot")).to_have_count(2)
        last = page.locator("#thread .msg.bot").last
        expect(last.locator(".msg-actions, .answer.refused, .answer").first).to_be_visible(timeout=30_000)
        page.wait_for_timeout(1500)  # let the streamed answer finish
        assert "77,000" not in last.inner_text()
        run.ok("live chat: engineering key asking the same question gets no HR passage")
        run.shot(page, "live-chat")

        goto(page, "audit")
        page.click("#verifyBtn")
        expect(page.locator("#verifyOut")).to_contain_text("Log intact")
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
