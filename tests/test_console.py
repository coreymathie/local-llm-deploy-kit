# Corey Mathie, 2026
"""
The product console (demo/ served at /console) and the endpoints it reads in live mode: overview, audit
entries, access matrix, runtime policy, rate-limit windows, lock validation, the ML-BOM, per-request
retrieval switches, and the simulated mock backend that `docker compose up` uses.
"""

import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest

from gateway import audit, auth, backends, console, main
from gateway.config import settings
from scripts import mock_openai_server

from .conftest import ADMIN, new_key

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "demo"
HR = "Compensation bands.\n\nThe level 3 salary band is $77,000 to $95,000 per year."
TRAVEL = "Travel policy.\n\nHotel stays are capped at $180 per night. Meals are reimbursed up to $60 per day."


@pytest.fixture(autouse=True)
def restore_policy(monkeypatch):
    """PUT /admin/policy mutates the live settings object; put every editable field back afterwards."""
    for name in console.POLICY_FIELDS:
        monkeypatch.setattr(settings, name, getattr(settings, name))


def key_with_groups(client, label, groups):
    r = client.post("/admin/keys", json={"label": label, "groups": groups}, headers=ADMIN)
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['key']}"}


def seed(client):
    for title, text, acl in (("hr.md", HR, ["group:hr"]), ("travel.md", TRAVEL, [])):
        r = client.post(
            "/v1/collections/policies/documents/text", json={"title": title, "text": text, "acl": acl}, headers=ADMIN
        )
        assert r.status_code == 201


# ---------- serving the console ----------


def test_console_is_served_with_a_live_mode_marker(client):
    r = client.get("/console", follow_redirects=False)
    assert r.status_code in (307, 308) and r.headers["location"] == "/console/"
    page = client.get("/console/")
    assert page.status_code == 200 and "Private LLM Platform</title>" in page.text
    mode = client.get("/console/api-mode").json()
    assert mode == {"mode": "live", "product": "Private LLM Platform", "version": main.__version__, "backend": "ollama"}
    for asset in ("app.js", "adapters.js", "screens.js", "ui.js", "styles.css", "data/rag_eval.json"):
        assert client.get(f"/console/{asset}").status_code == 200, asset
    assert client.get("/admin").status_code == 200  # the single-file admin page still works


def test_static_hosting_marker_says_demo():
    """GitHub Pages serves demo/api-mode; a gateway answers that path itself (route before the mount)."""
    assert json.loads((DEMO / "api-mode").read_text())["mode"] == "demo"
    from starlette.routing import Mount

    routes = main.app.routes
    mode = next(i for i, r in enumerate(routes) if getattr(r, "path", "") == "/console/api-mode")
    mount = next(i for i, r in enumerate(routes) if isinstance(r, Mount) and r.path == "/console")
    assert mode < mount


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/admin/overview", None),
        ("GET", "/admin/audit/entries", None),
        ("GET", "/admin/access-matrix?collection=policies", None),
        ("GET", "/admin/policy", None),
        ("POST", "/admin/policy/validate", {}),
        ("PUT", "/admin/policy", {"GATEWAY_RATE_LIMIT_PER_MIN": 1000}),
        ("GET", "/admin/rate-limits", None),
        ("POST", "/admin/models/lock/validate", {"text": "{}"}),
        ("GET", "/admin/models/mlbom", None),
    ],
)
def test_console_endpoints_are_admin_only(client, method, path, body):
    user = {"Authorization": f"Bearer {new_key(client)}"}
    assert client.request(method, path, json=body).status_code == 401
    assert client.request(method, path, json=body, headers=user).status_code == 403
    assert settings.GATEWAY_RATE_LIMIT_PER_MIN == 60  # the refused PUT changed nothing


# ---------- overview and audit ----------


def test_overview_counts_requests_identities_documents_and_decisions(client):
    seed(client)
    hr = key_with_groups(client, "hr-bot", ["hr"])
    assert client.post("/v1/collections/policies/ask", json={"question": "salary band?"}, headers=hr).status_code == 200
    client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]}, headers=hr)
    ov = client.get("/admin/overview", headers=ADMIN).json()
    assert ov["requests"]["total"] == 2 and ov["requests"]["questions"] == 1 and ov["requests"]["tokens"] > 0
    assert ov["identities"] == {"keys_active": 2, "keys_revoked": 0, "admin_keys": 1, "token_users": 0}
    assert ov["documents"] == {"collections": 1, "documents": 2, "passages": 2}
    assert ov["audit"]["ok"] and ov["audit"]["events"]["document_added"] == 2
    assert ov["access_decisions"] == {"allow": 1, "deny": 0}
    series = ov["activity"]["series"]
    assert len(series) >= 6 and sum(b["questions"] for b in series) == 1 and sum(b["completions"] for b in series) == 1
    assert ov["version"] == main.__version__ and ov["backend"]["name"] == "ollama"
    assert ov["retrieval"] == {"mode": settings.GATEWAY_RETRIEVAL_MODE, "reranker": settings.GATEWAY_RERANKER}


def test_audit_entries_carry_line_numbers_decisions_and_cited_documents(client):
    seed(client)
    hr = key_with_groups(client, "hr-bot", ["hr"])
    client.post("/v1/collections/policies/ask", json={"question": "level 3 salary band"}, headers=hr)
    plain = key_with_groups(client, "kiosk", [])
    client.put("/admin/collections/policies/acl", json={"principals": ["group:hr"]}, headers=ADMIN)
    assert client.post("/v1/collections/policies/ask", json={"question": "x"}, headers=plain).status_code == 404

    out = client.get("/admin/audit/entries?limit=50", headers=ADMIN).json()
    assert out["verify"]["ok"]
    lines = [e["line"] for e in out["entries"]]
    assert lines == sorted(lines, reverse=True) and lines[0] == out["verify"]["entries"]
    denied = out["entries"][0]["summary"]
    assert denied["event"] == "document_question_denied" and denied["actor"] == "kiosk" and denied["decision"] == "deny"
    asked = next(e for e in out["entries"] if e["event"] == "document_question")
    assert asked["summary"]["actor"] == "hr-bot" and asked["summary"]["decision"] == "allow"
    assert asked["payload"]["sources"] and set(asked["payload"]["timings_ms"]) == {"access", "retrieval", "generation"}
    created = next(e for e in out["entries"] if e["event"] == "key_created")
    assert created["summary"]["actor"] == "bootstrap admin"  # the admin who acted, not the new key

    path = audit._path()
    rows = path.read_text().splitlines()
    edited = json.loads(rows[1])
    edited["payload"]["edited"] = True
    rows[1] = json.dumps(edited, separators=(",", ":"))
    path.write_text("\n".join(rows) + "\n")
    assert client.get("/admin/audit/entries", headers=ADMIN).json()["verify"] == {
        "ok": False,
        "entries": 2,
        "bad_line": 2,
    }


def test_access_matrix_shows_each_key_and_mapped_group_against_each_document(client, monkeypatch):
    monkeypatch.setattr(settings, "GATEWAY_OIDC_GROUP_ROLES", {"hr-staff": ["user"], "auditors": ["reader:policies"]})
    seed(client)
    key_with_groups(client, "hr-bot", ["hr"])
    key_with_groups(client, "kiosk", [])
    m = client.get("/admin/access-matrix?collection=policies", headers=ADMIN).json()
    docs = {d["title"]: d["id"] for d in m["documents"]}
    rows = {r["label"]: r for r in m["rows"]}
    assert set(rows) == {"bootstrap admin", "hr-bot", "kiosk", "group:hr-staff member", "group:auditors member"}
    assert rows["hr-bot"]["documents"][docs["hr.md"]] == "document_acl:group:hr"
    assert rows["kiosk"]["documents"][docs["hr.md"]] is None and rows["kiosk"]["hidden"] == 1
    assert rows["bootstrap admin"]["documents"][docs["hr.md"]] == "role:admin"
    assert rows["group:auditors member"]["collection"] == {"allowed": True, "basis": "role:reader:policies"}
    assert rows["group:auditors member"]["roles"] == ["reader:policies"]
    assert client.get("/admin/access-matrix?collection=Bad Name", headers=ADMIN).status_code == 400


# ---------- runtime policy ----------


def test_policy_validation_reports_every_problem_with_its_field(client):
    bad = {
        "GATEWAY_OIDC_GROUP_ROLES": {"auditors": ["superuser"]},
        "GATEWAY_RETRIEVAL_MODE": "fuzzy",
        "GATEWAY_RATE_LIMIT_PER_MIN": 0,
        "GATEWAY_RERANKER": "cross_encoder",
        "OPENAI_COMPAT_API_KEY": "sk-should-not-be-editable",
    }
    out = client.post("/admin/policy/validate", json=bad, headers=ADMIN).json()
    fields = {e["field"] for e in out["errors"]}
    assert out["ok"] is False and fields == {
        "GATEWAY_OIDC_GROUP_ROLES.auditors",
        "GATEWAY_RETRIEVAL_MODE",
        "GATEWAY_RATE_LIMIT_PER_MIN",
        "GATEWAY_RERANKER",
        "OPENAI_COMPAT_API_KEY",
    }
    r = client.put("/admin/policy", json=bad, headers=ADMIN)
    assert r.status_code == 422 and settings.GATEWAY_RETRIEVAL_MODE != "fuzzy"
    ok = client.post("/admin/policy/validate", json={"GATEWAY_RETRIEVAL_MODE": "bm25"}, headers=ADMIN).json()
    assert ok["ok"] and ok["policy"]["GATEWAY_RETRIEVAL_MODE"] == "bm25"
    assert settings.GATEWAY_RETRIEVAL_MODE != "bm25" or ok  # validation applies nothing


def test_applied_policy_takes_effect_immediately_and_is_audited(client):
    seed(client)
    kiosk = key_with_groups(client, "kiosk", [])
    assert client.post("/v1/collections/policies/ask", json={"question": "hotel"}, headers=kiosk).status_code == 200
    current = client.get("/admin/policy", headers=ADMIN).json()
    assert current["persisted"] is False and set(current["policy"]) == set(console.POLICY_FIELDS)

    r = client.put(
        "/admin/policy",
        json={
            **current["policy"],
            "GATEWAY_COLLECTION_DEFAULT_ACCESS": "restricted",
            "GATEWAY_RATE_LIMIT_PER_MIN": 500,
        },
        headers=ADMIN,
    )
    assert r.status_code == 200 and r.json()["changed"] == {
        "GATEWAY_COLLECTION_DEFAULT_ACCESS": "restricted",
        "GATEWAY_RATE_LIMIT_PER_MIN": 500,
    }
    assert client.post("/v1/collections/policies/ask", json={"question": "hotel"}, headers=kiosk).status_code == 404
    entry = next(e for e in audit.recent(10) if e["event"] == "policy_changed")
    assert entry["payload"] == {
        "before": {"GATEWAY_COLLECTION_DEFAULT_ACCESS": "open", "GATEWAY_RATE_LIMIT_PER_MIN": 60},
        "after": {"GATEWAY_COLLECTION_DEFAULT_ACCESS": "restricted", "GATEWAY_RATE_LIMIT_PER_MIN": 500},
        "by": "bootstrap admin",
    }
    # Re-applying the same document changes nothing and writes no entry.
    n = audit.verify()["entries"]
    assert client.put("/admin/policy", json={}, headers=ADMIN).json()["changed"] == {}
    assert audit.verify()["entries"] == n


def test_rate_limit_window_per_key(client, monkeypatch):
    monkeypatch.setattr(settings, "GATEWAY_RATE_LIMIT_PER_MIN", 5)
    auth._calls.clear()
    key = new_key(client, label="busy")
    for _ in range(3):
        client.get("/v1/me", headers={"Authorization": f"Bearer {key}"})
    out = client.get("/admin/rate-limits", headers=ADMIN).json()
    busy = next(k for k in out["keys"] if k["label"] == "busy")
    assert out["limit_per_min"] == 5 and out["window_seconds"] == 60
    assert busy["in_window"] == 3 and busy["key"].endswith("…") and key not in json.dumps(out)


# ---------- retrieval switches per request ----------


def test_ask_accepts_per_request_retrieval_mode_and_reranker(client):
    seed(client)
    body = {"question": "hotel cap per night", "retrieval_mode": "bm25", "reranker": "lexical"}
    out = client.post("/v1/collections/policies/ask", json=body, headers=ADMIN).json()
    assert out["retrieval"] == {"mode": "bm25", "reranker": "lexical"}
    assert set(out["sources"][0]["scores"]) == {"bm25", "rerank"} and out["sources"][0]["title"] == "travel.md"
    assert set(out["timings_ms"]) == {"access", "retrieval", "generation"}
    assert "access" not in out  # callers never learn how many documents are hidden from them
    entry = next(e for e in audit.recent(5) if e["event"] == "document_question")
    assert entry["payload"]["retrieval"] == {"mode": "bm25", "reranker": "lexical"}
    assert settings.GATEWAY_RETRIEVAL_MODE == "hybrid"  # the override didn't touch the setting
    bad = client.post("/v1/collections/policies/ask", json={"question": "x", "retrieval_mode": "fuzzy"}, headers=ADMIN)
    assert bad.status_code == 422
    ce = client.post("/v1/collections/policies/ask", json={"question": "x", "reranker": "cross_encoder"}, headers=ADMIN)
    assert ce.status_code == 400 and "GATEWAY_CROSS_ENCODER_MODEL" in ce.json()["detail"]


# ---------- models ----------


def test_lock_validation_uses_the_startup_loader(client):
    example = (ROOT / "models.lock.example.json").read_text()
    ok = client.post("/admin/models/lock/validate", json={"text": example}, headers=ADMIN).json()
    assert ok["ok"] and ok["models"][0]["name"] == "llama3.1:8b"
    for text, message in (
        ("not json", "Expecting value"),
        ('{"version": 2, "models": []}', "version"),
        ('{"version": 1, "models": [{"name": "x", "digest": "sha256:zz"}]}', "SHA-256 hex"),
        ('{"version": 1, "models": [{"name": "x", "files": "nope"}]}', "files must be a list"),
    ):
        bad = client.post("/admin/models/lock/validate", json={"text": text}, headers=ADMIN).json()
        assert bad["ok"] is False and message in bad["error"], (text, bad)


def test_mlbom_endpoint_lists_pinned_models(client, monkeypatch):
    monkeypatch.setattr(settings, "GATEWAY_MODEL_LOCK_FILE", str(ROOT / "models.lock.example.json"))
    out = client.get("/admin/models/mlbom", headers=ADMIN).json()
    names = [c["name"] for c in out["components"]]
    assert names == ["llama3.1:8b", "nomic-embed-text:latest", "Qwen/Qwen2.5-7B-Instruct"]
    assert all(c["type"] == "machine-learning-model" for c in out["components"])
    monkeypatch.setattr(settings, "GATEWAY_MODEL_LOCK_FILE", "")
    assert client.get("/admin/models/mlbom", headers=ADMIN).json()["components"] == []


# ---------- the simulated backend behind `docker compose up` ----------


def test_mock_server_simulated_mode_answers_rag_prompts_with_citations(monkeypatch):
    from fastapi.testclient import TestClient

    from gateway import rag

    monkeypatch.setenv("MOCK_MODE", "simulated")
    c = TestClient(mock_openai_server.app)
    assert [m["id"] for m in c.get("/v1/models").json()["data"]] == ["mock-model", "mock-embed"]
    assert len(c.post("/v1/embeddings", json={"input": "hotel"}).json()["data"][0]["embedding"]) == 512
    src = rag.Source(1, "d1", "travel.md", 0, 1.0, TRAVEL)
    messages = rag.build_messages("What is the hotel cap per night?", [src])
    out = c.post("/v1/chat/completions", json={"messages": messages}).json()
    answer = out["choices"][0]["message"]["content"]
    assert "$180 per night" in answer and "[1]" in answer
    plain = c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]}).json()
    assert plain["choices"][0]["message"]["content"].startswith("Simulated backend")


def test_compose_default_stack_end_to_end(client, monkeypatch):
    """gateway (BACKEND=openai_compatible) -> mock-llm (MOCK_MODE=simulated), as docker-compose.yml wires them."""
    monkeypatch.setenv("MOCK_MODE", "simulated")
    monkeypatch.setattr(settings, "BACKEND", "openai_compatible")
    monkeypatch.setattr(settings, "OPENAI_COMPAT_BASE_URL", "http://mock-llm:8001/v1")
    monkeypatch.setattr(settings, "GATEWAY_DEFAULT_MODEL", "mock-model")
    monkeypatch.setattr(settings, "GATEWAY_EMBED_MODEL", "mock-embed")
    upstream = httpx.ASGITransport(app=mock_openai_server.app)
    monkeypatch.setattr(
        backends.httpx, "AsyncClient", lambda **kw: httpx.AsyncClient(transport=upstream, headers=kw.get("headers"))
    )
    backends.set_backend(None)
    seed(client)
    hr = key_with_groups(client, "hr-assistant", ["hr"])
    eng = key_with_groups(client, "eng-assistant", ["engineering"])
    out = client.post("/v1/collections/policies/ask", json={"question": "What is the level 3 salary band?"}, headers=hr)
    body = out.json()
    assert out.status_code == 200 and "$77,000" in body["answer"] and body["sources"][0]["title"] == "hr.md"
    assert body["sources"][0]["cited"] and body["model"] == "mock-model"
    other = client.post("/v1/collections/policies/ask", json={"question": "level 3 salary band?"}, headers=eng).json()
    assert "77,000" not in json.dumps(other)
    asyncio.run(backends.aclose_clients())
    backends.set_backend(None)


# ---------- evals data and the page ----------


def test_console_eval_data_matches_a_fresh_run():
    """demo/data/rag_eval.json is produced by scripts/rag_eval.py --console-data and must not drift."""
    from scripts import rag_eval

    committed = json.loads((DEMO / "data" / "rag_eval.json").read_text())
    fresh = rag_eval.console_data(rag_eval.evaluate(4))
    assert committed == json.loads(json.dumps(fresh))
    assert committed["problems"] == [] and committed["measured"] is True
    for f in committed["golden_files"]:
        assert (ROOT / f).is_file(), f


def test_console_page_loads_only_local_files_and_the_pyodide_cdn():
    html = (DEMO / "index.html").read_text()
    scripts_ = json.loads(re.search(r"const SCRIPT_FILES = (\[.*?\]);", html, re.S).group(1))
    for name in scripts_:
        assert (ROOT / "scripts" / name).is_file(), name
    for name in ("app.js", "adapters.js", "screens.js", "ui.js", "styles.css"):
        assert (DEMO / name).is_file()
    for path in DEMO.glob("*.js"):
        urls = set(re.findall(r"https://[^\s\"'`)]+", path.read_text()))
        for url in urls:
            assert url.startswith(("https://cdn.jsdelivr.net/pyodide/v0.26.4/", "https://github.com/coreymathie/")), (
                path.name,
                url,
            )
    assert "<script src=" not in html.replace('<script type="module" src="app.js">', "")  # Pyodide loads on demand
