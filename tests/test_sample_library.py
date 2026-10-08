# Corey Mathie, 2026
"""The demo's document library: reproducible, consistent with the overview, and searched safely across collections."""

import asyncio
import json
from pathlib import Path

import pytest

from gateway import backends
from gateway.config import settings
from scripts import sample_library

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo"


def run(coro):
    return asyncio.run(coro)


def test_committed_library_matches_the_generator():
    assert sample_library.main(["--check"]) == 0


def test_catalog_lists_every_document_once_with_an_owner_and_review_date():
    cat = json.loads((DEMO / "data" / "library.json").read_text())
    paths = [d["path"] for d in cat["documents"]]
    assert len(paths) == len(set(paths)) and len(paths) >= 50
    for d in cat["documents"]:
        assert (DEMO / d["path"]).is_file(), d["path"]
        assert d["owner"] and d["reviewed"] and d["passages"] >= 1
        assert "fictional" not in (DEMO / d["path"]).read_text().lower(), "the workspace is labelled, not each page"
    counts = {c["name"]: c["documents"] for c in cat["collections"]}
    assert sum(counts.values()) == len(paths)


def test_overview_collections_come_from_the_library():
    cat = json.loads((DEMO / "data" / "library.json").read_text())
    company = json.loads((DEMO / "data" / "sample_company.json").read_text())
    assert [(c["name"], c["documents"]) for c in company["collections"]] == [
        (c["name"], c["documents"]) for c in cat["collections"]
    ]


@pytest.fixture
def library_engine(tmp_path):
    from demo import engine as demo_engine

    saved = settings.model_dump()
    eng = demo_engine.DemoEngine(workdir=str(tmp_path / "demo"), rate_limit=100)
    cat = json.loads((DEMO / "data" / "library.json").read_text())
    for d in cat["documents"]:
        out = run(eng.add_document(d["collection"], d["file"], (DEMO / d["path"]).read_text(), d["acl"]))
        assert out["status"] == 201, d["file"]
    yield eng
    for k, v in saved.items():
        setattr(settings, k, v)
    backends.set_backend(None)


def test_all_sources_answers_from_the_right_collection(library_engine):
    out = run(library_engine.ask_as("priya", "*", "How long is an oral stop payment good for?"))
    assert out["status"] == 200 and "14 days" in out["answer"]
    assert out["sources"][0]["collection"] == "member-services" and out["sources"][0]["cited"]
    assert out["access"]["collections_searched"] == 5


def test_all_sources_still_hides_restricted_documents(library_engine):
    salary = "What is the level 3 salary band?"
    priya = run(library_engine.ask_as("priya", "*", salary))
    assert priya["answer"].startswith("Level 3 salary band is $77,000") and "Level 1" not in priya["answer"]
    for persona in ("dana", "marcus", "kiosk"):
        out = run(library_engine.ask_as(persona, "*", salary))
        assert "77,000" not in json.dumps(out), persona
    ofac = "How fast must a potential OFAC match go to the BSA officer?"
    assert "1 hour" in run(library_engine.ask_as("marcus", "*", ofac))["answer"]
    assert "1 hour" not in json.dumps(run(library_engine.ask_as("priya", "*", ofac)))


def test_off_topic_questions_are_declined_instead_of_quoting_a_passage(library_engine):
    from demo.engine import NO_ANSWER

    for q in ("What is our CEO's name?", "Who won the Super Bowl?"):
        out = run(library_engine.ask_as("priya", "*", q))
        assert out["status"] == 200 and out["answer"] == NO_ANSWER, q
        assert not any(s["cited"] for s in out["sources"])
