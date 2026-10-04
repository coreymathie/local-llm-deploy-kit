# Corey Mathie, 2026
"""
Headless smoke test of the browser demo (demo/index.html) with Playwright.

    pip install playwright && playwright install chromium
    python scripts/demo_smoke.py                      # Pyodide from cdn.jsdelivr.net
    python scripts/demo_smoke.py --pyodide-dir ./pyodide-0.26.4/pyodide   # offline copy of the release

Serves the repo root on a local port (like GitHub Pages does), opens /demo/,
waits for Pyodide and the gateway modules to load, then drives every panel:
keys + rate limit + revocation, redaction, audit verify/tamper/restore,
document Q&A with citations and an injection-flagged document, and identity:
personas in different groups get different answers and citations. Also checks
there is no horizontal scroll at phone width. Exits non-zero on any failure.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import re
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CDN = "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/"


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def serve(port: int) -> http.server.ThreadingHTTPServer:
    handler = functools.partial(_Quiet, directory=str(ROOT))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--pyodide-dir", type=Path, default=None, help="serve Pyodide from a local release folder")
    ap.add_argument("--screenshots", type=Path, default=None, help="folder for desktop/mobile screenshots")
    args = ap.parse_args(argv)

    from playwright.sync_api import expect, sync_playwright

    httpd = serve(args.port)
    url = f"http://127.0.0.1:{args.port}/demo/"
    errors: list[str] = []
    checks: list[str] = []

    def ok(msg: str) -> None:
        checks.append(msg)
        print(f"  ok  {msg}")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1366, "height": 900})
        page.on("console", lambda m: m.type == "error" and errors.append(m.text))
        page.on("pageerror", lambda e: errors.append(str(e)))
        if args.pyodide_dir:
            local = args.pyodide_dir.resolve()

            def fulfill(route):
                name = route.request.url.split(CDN, 1)[1].split("?", 1)[0]
                f = local / name
                if f.is_file():
                    ctype = "application/wasm" if name.endswith(".wasm") else None
                    route.fulfill(path=str(f), content_type=ctype, headers={"Access-Control-Allow-Origin": "*"})
                else:
                    route.fulfill(status=404, body=f"not in local pyodide dir: {name}")

            page.route(f"{CDN}**", fulfill)

        page.goto(url)
        page.wait_for_function("document.body.dataset.ready", timeout=180_000)
        if page.evaluate("document.body.dataset.ready") != "true":
            print("demo failed to start:", page.inner_text("#loaderNote"))
            return 1
        ok("Pyodide loaded, gateway modules fetched, engine started, samples ingested")
        modules = page.locator("#modules .module").all_inner_texts()
        assert any("gateway/auth.py" in m for m in modules) and any("gateway/rag.py" in m for m in modules)
        ok(f"{len(modules)} gateway modules listed with line counts and hashes")

        # 1. keys: burst over the limit, then revoke
        row = page.locator('#keysBody tr[data-label="billing-app"]')
        row.locator('button[data-act="burst"]').click()
        expect(page.locator("#respLog .pill")).to_have_count(12)
        statuses = page.locator("#respLog .pill").all_inner_texts()
        assert statuses.count("200") == 10 and statuses.count("429") == 2, statuses
        ok("burst of 12 at 10/min: 10 x 200, 2 x 429")
        expect(row.locator("td.num").first).to_have_text("10")
        row.locator('button[data-act="revoke"]').click()
        expect(row.locator(".pill.bad")).to_have_text("revoked")
        row.locator('button[data-act="send"]').click()
        expect(page.locator("#respLog > div").first).to_contain_text("invalid or revoked key")
        ok("revoked key gets 401")

        # 2. redaction
        page.click("#redactBtn")
        expect(page.locator("#redactOut")).to_contain_text("[REDACTED_SSN]")
        out = page.inner_text("#redactOut")
        assert "[REDACTED_SSN]" in out and "[REDACTED_PAN]" in out and "[REDACTED_PHONE]" in out, out
        assert "1234 5678 9012 3456" in out and "123-45-6789" not in out
        ok("redaction: SSN, card, phone, email, DOB masked; Luhn-invalid number kept")

        # 3. audit: verify, tamper, detect, restore
        page.click("#verifyBtn")
        expect(page.locator("#verifyOut")).to_contain_text("Chain intact")
        page.select_option("#tamperLine", "2")
        page.select_option("#tamperMode", "edit")
        page.click("#tamperBtn")
        expect(page.locator("#verifyOut")).to_contain_text("Tampering detected at line 2")
        ok("edited audit line 2 -> verify reports line 2")
        page.click("#restoreBtn")
        expect(page.locator("#verifyOut")).to_contain_text("Chain intact")
        page.select_option("#tamperLine", "2")
        page.select_option("#tamperMode", "edit_rehash")
        page.click("#tamperBtn")
        expect(page.locator("#verifyOut")).to_contain_text("Tampering detected at line 3")
        ok("edit + recomputed hash on line 2 -> caught at line 3")
        page.click("#restoreBtn")
        expect(page.locator("#verifyOut")).to_contain_text("Chain intact")
        events = page.locator("#auditBody td:nth-child(3)").all_inner_texts()
        assert "completion" in events and "key_revoked" in events, events
        ok("audit shows completion (redacted prompt) and key_revoked entries")

        # 4. document Q&A
        page.click('#suggest button[data-q="What is the hotel cap per night?"]')
        expect(page.locator("#answerBox .answer")).to_contain_text("$180 per night")
        first = page.locator("#sources .source").first
        expect(first).to_contain_text("sample-travel-expense-policy.md")
        expect(first.locator(".pill.ok")).to_have_text("cited")
        page.locator("#answerBox .cite").first.click()
        ok("question answered from the travel policy with a [1] citation")
        expect(first).to_contain_text("RRF")
        page.select_option("#retrievalMode", "bm25")
        expect(page.locator("#answerBox .answer")).to_contain_text("retrieval bm25")
        expect(page.locator("#sources .source").first).to_contain_text("sample-travel-expense-policy.md")
        assert "cosine" not in page.inner_text("#sources")  # BM25 mode doesn't embed the question
        page.check("#rerankLexical")
        expect(page.locator("#answerBox .answer")).to_contain_text("lexical reranker")
        expect(page.locator("#sources .source").first).to_contain_text("rerank")
        page.uncheck("#rerankLexical")
        page.select_option("#retrievalMode", "hybrid")
        expect(page.locator("#answerBox .answer")).to_contain_text("retrieval hybrid")
        ok("retrieval switches: hybrid (cosine + BM25 + RRF), BM25 only, lexical reranker")
        page.click("#poisonBtn")
        expect(page.locator("#sources")).to_contain_text("vendor-notes-UNTRUSTED.md")
        expect(page.locator("#sources .pill.bad").first).to_contain_text("injection")
        # Only the extracted answer must not repeat the injected instruction; the explanation
        # banner below it quotes the instruction on purpose.
        answer = page.inner_text("#answerBox .answer")
        assert "ignore all previous" not in answer.lower() and "admin key" not in answer.lower(), answer
        expect(page.locator("#answerBox .banner.warn")).to_contain_text("don't make")
        ok("poisoned document retrieved, flagged, its instruction not repeated, limitation explained")

        # 5. identity and permission-aware retrieval
        page.click('#personas button[data-persona="priya"]')
        expect(page.locator("#whoBox")).to_contain_text("user:priya")
        page.click('#idSuggest button[data-q="What is the level 3 salary band?"]')
        expect(page.locator("#idAnswer .answer")).to_contain_text("$77,000")
        first = page.locator("#idSources .source").first
        expect(first).to_contain_text("restricted-hr-compensation-bands.md")
        expect(first.locator(".pill.ok")).to_have_text("cited")
        ok("HR user (SSO, group hr) gets the salary band from the HR-only document, cited")
        page.click('#personas button[data-persona="dana"]')
        expect(page.locator("#whoBox")).to_contain_text("user:dana")
        page.click("#idAsk")
        expect(page.locator("#idAnswer .answer")).to_contain_text("Asked as user:dana")
        assert "77,000" not in page.inner_text("#idAnswer") + page.inner_text("#idSources")
        assert "compensation" not in page.inner_text("#idSources")
        ok("engineering user asking the same question: no HR passage in answer or sources")
        page.click('#idSuggest button[data-q="How fast must the payments on-call engineer acknowledge a page?"]')
        expect(page.locator("#idSources .source").first).to_contain_text("restricted-payments-oncall-runbook.md")
        ok("engineering user gets the engineering-only runbook")
        page.click('#personas button[data-persona="audrey"]')
        expect(page.locator("#whoBox")).to_contain_text("reader:policies")
        page.click("#idChat")
        expect(page.locator("#idAnswer")).to_contain_text("403")
        ok("collection reader can't use raw chat (403)")
        events = page.locator("#auditBody").inner_text()
        assert "access=" in events, events[:400]
        ok("audit entries record the access decision")

        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        if args.screenshots:
            args.screenshots.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(args.screenshots / "demo-desktop.png"), full_page=True)

        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(300)
        overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert overflow <= 0, f"horizontal scroll at 390px: {overflow}px"
        ok("no horizontal page scroll at 390 px")
        if args.screenshots:
            page.screenshot(path=str(args.screenshots / "demo-mobile.png"), full_page=True)
        browser.close()

    httpd.shutdown()
    real_errors = [e for e in errors if not re.search(r"favicon", e)]
    if real_errors:
        print("console errors:", *real_errors, sep="\n  ")
        return 1
    print(f"\n{len(checks)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
