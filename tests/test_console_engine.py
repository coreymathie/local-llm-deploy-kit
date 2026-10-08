# Corey Mathie, 2026
"""
The console's demo-mode engine (demo/engine.py) under CPython: the same calls the page makes through
Pyodide for the Overview, Chat, Documents, Users & Keys, Models, Policies and Evals screens.
"""

import asyncio
import json
from pathlib import Path

import pytest

from gateway import audit, auth, backends, supply_chain
from gateway.config import settings

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "demo"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def engine(tmp_path):
    from demo import engine as demo_engine

    saved = settings.model_dump()
    eng = demo_engine.DemoEngine(workdir=str(tmp_path / "demo"))
    for path in sorted(DEMO.glob("sample-*.md")):
        run(eng.add_document("policies", path.name, path.read_text()))
    run(eng.add_document("policies", "hr.md", (DEMO / "restricted-hr-compensation-bands.md").read_text(), ["group:hr"]))
    yield eng
    for k, v in saved.items():
        setattr(settings, k, v)
    backends.set_backend(None)
    supply_chain.reset()
    auth._calls.clear()


def test_engine_version_matches_the_gateway():
    from demo import engine as demo_engine
    from gateway.main import __version__

    assert demo_engine.VERSION == __version__


def test_overview_and_audit_entries_in_the_browser_engine(engine):
    run(engine.ask_as("priya", "policies", "What is the level 3 salary band?"))
    ov = engine.overview()
    assert ov["documents"] == {"collections": 1, "documents": 11, "passages": 13}
    assert ov["requests"]["questions"] == 1 and ov["identities"]["token_users"] == 1
    assert ov["backend"]["name"] == "demo" and ov["audit"]["ok"]
    out = engine.audit_entries(5)
    assert out["entries"][0]["summary"]["actor"] == "user:priya" and out["entries"][0]["summary"]["decision"] == "allow"


def test_access_matrix_names_the_personas(engine):
    m = engine.access_matrix("policies")
    rows = {r["persona"] or r["label"]: r for r in m["rows"]}
    hr = next(d["id"] for d in m["documents"] if d["title"] == "hr.md")
    assert rows["Priya Shah (HR)"]["documents"][hr] == "document_acl:group:hr"
    assert rows["Dana Ortiz (Engineering)"]["documents"][hr] is None
    assert rows["Audrey Kim (Internal audit)"]["collection"]["basis"] == "role:reader:policies"
    before = auth._calls.copy()
    engine.access_matrix("policies")
    assert auth._calls == before  # building the matrix doesn't spend anyone's rate limit


def test_per_request_retrieval_and_key_personas(engine):
    out = run(engine.ask_as("admin", "policies", "What is the hotel cap per night?", 4, "bm25", "lexical"))
    assert out["retrieval"] == {"mode": "bm25", "reranker": "lexical"} and set(out["timings_ms"]) == {
        "access",
        "retrieval",
        "generation",
    }
    created = engine.create_key("hr-assistant", False, ["hr"])
    persona = next(p for p in engine.personas() if p["name"] == "hr-assistant")
    assert persona["id"].startswith("key:hr-assistant:") and created["key"].endswith(persona["id"].rsplit(":", 1)[1])
    answer = run(engine.ask_as(persona["id"], "policies", "What is the level 3 salary band?"))
    assert "$77,000" in answer["answer"] and answer["asked_as"] == "hr-assistant"
    engine.revoke_key(created["key"])
    assert run(engine.ask_as(persona["id"], "policies", "x"))["status"] == 401


def test_policy_change_turns_the_auditors_403_into_200(engine):
    assert run(engine.chat_as("audrey", "hi"))["status"] == 403
    bad = engine.validate_policy({"GATEWAY_OIDC_GROUP_ROLES": {"auditors": ["root"]}})
    assert bad["ok"] is False and bad["errors"][0]["field"] == "GATEWAY_OIDC_GROUP_ROLES.auditors"
    roles = {**settings.GATEWAY_OIDC_GROUP_ROLES, "auditors": ["reader:policies", "user"]}
    out = engine.apply_policy({"GATEWAY_OIDC_GROUP_ROLES": roles})
    assert out["status"] == 200 and list(out["changed"]) == ["GATEWAY_OIDC_GROUP_ROLES"]
    assert run(engine.chat_as("audrey", "hi"))["status"] == 200
    entry = [r["entry"] for r in engine.audit_lines() if r["entry"]["event"] == "policy_changed"][-1]
    assert entry["payload"]["after"]["GATEWAY_OIDC_GROUP_ROLES"]["auditors"] == ["reader:policies", "user"]
    assert engine.apply_policy({"GATEWAY_COLLECTION_DEFAULT_ACCESS": "restricted"})["status"] == 200
    assert run(engine.ask_as("kiosk", "policies", "hotel cap"))["status"] == 404


def test_supply_chain_simulator_pin_repull_enforce(engine):
    state = run(engine.models())
    assert {m["status"] for m in state["verification"]["models"].values()} == {"unpinned"}
    assert run(engine.check_model("llama3.1:8b")) == {"status": 200, "model_status": "unpinned", "policy": "warn"}
    pinned = run(engine.pin_served())
    assert pinned["status"] == 200 and pinned["verification"]["ok"] is True
    lock = json.loads(Path(settings.GATEWAY_MODEL_LOCK_FILE).read_text())
    assert lock["models"][0]["license"] == "Llama 3.1 Community License"  # metadata kept, as pin_models.py does
    repulled = run(engine.simulate_repull("llama3.1:8b"))
    assert repulled["verification"]["models"]["llama3.1:8b"]["status"] == "mismatch"
    engine.apply_policy({"GATEWAY_MODEL_POLICY": "enforce"})
    refused = run(engine.check_model("llama3.1:8b"))
    assert refused["status"] == 403 and "mismatch" in refused["detail"]
    assert run(engine.check_model("nomic-embed-text:latest"))["model_status"] == "verified"
    bom = engine.mlbom()
    assert [c["name"] for c in bom["components"]] == ["llama3.1:8b", "nomic-embed-text:latest"]
    assert engine.validate_lock("{}")["ok"] is False
    assert run(engine.set_lock('{"version": 1, "models": [{"name": "x", "digest": "zz"}]}'))["status"] == 400
    assert audit.verify()["ok"]


def test_rag_eval_runs_inside_the_engine_and_matches_the_committed_results(engine):
    committed = json.loads((DEMO / "data" / "rag_eval.json").read_text())
    out = run(engine.run_rag_eval())
    assert out["problems"] == [] and out["report"]["results"] == committed["report"]["results"]
    # The engine keeps working afterwards (evaluate() swaps the backend and the database).
    assert run(engine.ask_as("priya", "policies", "What is the level 3 salary band?"))["status"] == 200
