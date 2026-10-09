# Corey Mathie, 2026
"""The demo's document library: reproducible, consistent with the overview and the personas, and searched safely
across collections. The sample institution must tell one story: one department list, access rules that match each
role, and suggested questions that are answered from the document they are meant to show."""

import asyncio
import json
import re
from datetime import date
from pathlib import Path

import pytest

from gateway import backends
from gateway.config import settings
from scripts import generate_sample_company, sample_library

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo"
CATALOG = json.loads((DEMO / "data" / "library.json").read_text())
RESTRICTED = {
    "restricted-hr-compensation-bands.md",
    "restricted-payments-oncall-runbook.md",
    "restricted-bsa-aml-escalation.md",
}


def run(coro):
    return asyncio.run(coro)


def files(collection=None) -> set[str]:
    return {d["file"] for d in CATALOG["documents"] if collection is None or d["collection"] == collection}


def text(file: str) -> str:
    return (DEMO / next(d["path"] for d in CATALOG["documents"] if d["file"] == file)).read_text()


def test_committed_library_matches_the_generator():
    assert sample_library.main(["--check"]) == 0
    assert sample_library.problems() == []


def test_catalog_lists_every_document_once_with_an_owner_and_review_date():
    paths = [d["path"] for d in CATALOG["documents"]]
    assert len(paths) == len(set(paths)) and len(paths) >= 50
    for d in CATALOG["documents"]:
        assert (DEMO / d["path"]).is_file(), d["path"]
        assert d["owner"] and d["reviewed"] and d["passages"] >= 1
        assert "fictional" not in (DEMO / d["path"]).read_text().lower(), "the workspace is labelled, not each page"
    counts = {c["name"]: c["documents"] for c in CATALOG["collections"]}
    assert sum(counts.values()) == len(paths)


def test_every_document_was_reviewed_within_twelve_months_of_the_data_period():
    end = generate_sample_company.END
    for d in CATALOG["documents"]:
        age = (end - date.fromisoformat(d["reviewed"])).days
        assert 0 <= age <= 365, (d["file"], d["reviewed"])


def test_overview_collections_come_from_the_library():
    company = json.loads((DEMO / "data" / "sample_company.json").read_text())
    assert [(c["name"], c["documents"]) for c in company["collections"]] == [
        (c["name"], c["documents"]) for c in CATALOG["collections"]
    ]
    access = {c["name"]: c["access"] for c in company["collections"]}
    assert [n for n, a in access.items() if "kiosk" in a] == ["member-info"]


def test_one_department_list_for_owners_personas_and_usage_figures():
    from demo.engine import PERSONAS

    departments = CATALOG["departments"]
    assert departments == sample_library.DEPARTMENTS
    assert {d["department"] for d in CATALOG["documents"]} <= set(departments)
    assert {p["department"] for p in PERSONAS.values() if "department" in p} <= set(departments)
    company = json.loads((DEMO / "data" / "sample_company.json").read_text())
    assert sorted(d["department"] for d in company["departments"]) == sorted(departments)
    for name, (dept, _role) in sample_library.PEOPLE.items():  # an owner who is also a persona has one department
        persona = next((p for p in PERSONAS.values() if p["name"].startswith(name)), None)
        assert persona is None or persona["department"] == dept, name


def test_documents_are_in_their_collections_without_duplicates_or_carry_over_wording():
    assert files("policies") >= {"sample-remote-work-policy.md", "restricted-hr-compensation-bands.md"}
    assert files("member-services") >= {"sample-card-dispute-procedure.md", "sample-wire-transfer-verification.md"}
    assert "sample-consumer-lending-guidelines.md" in files("lending")
    assert {"restricted-bsa-aml-escalation.md", "unusual-activity-referral.md"} <= files("compliance")
    assert "sample-branch-security-procedures.md" in files("branch-operations")
    assert "atm-and-debit-card-disputes.md" not in files(), "merged into the card dispute procedure"
    everything = {f: text(f) for f in files()}
    teller_limit = [f for f, t in everything.items() if "drawers are limited to $15,000" in t]
    assert teller_limit == ["cash-handling-limits.md"]
    for f, t in everything.items():
        for phrase in ("AI voice agent", "AI agent", "People Operations", "The company", "customer information"):
            assert phrase not in t, (f, phrase)
        assert not re.search(r"deletion requests?", t, re.I), f  # no GDPR-style erasure rights
    assert "first $275" in everything["mobile-deposit.md"]  # Regulation CC next-day amount since July 2025
    assert "larger than $5,000" not in everything["mobile-deposit.md"]  # a hold can't exceed the daily limit
    assert "$35 deferral fee" in everything["hardship-and-payment-deferral.md"]  # the fee it waives is defined
    id_rule = "photo ID and a Social Security or Individual Taxpayer Identification Number"
    assert id_rule in everything["sample-member-identity-verification.md"]
    assert id_rule in everything["account-opening-checklist.md"]
    assert "applies to all employees" not in everything["restricted-bsa-aml-escalation.md"]


@pytest.fixture
def library_engine(tmp_path):
    from demo import engine as demo_engine

    saved = settings.model_dump()
    eng = demo_engine.DemoEngine(workdir=str(tmp_path / "demo"), rate_limit=1000)
    texts = {d["file"]: (DEMO / d["path"]).read_text() for d in CATALOG["documents"]}
    assert run(eng.load_library(CATALOG, texts))["status"] == 201
    yield eng
    for k, v in saved.items():
        setattr(settings, k, v)
    backends.set_backend(None)


def cited(out) -> list[str]:
    return sorted({s["title"] for s in out.get("sources", []) if s["cited"]})


def test_kiosk_reads_only_member_facing_documents(library_engine):
    from demo.engine import NO_ANSWER

    kiosk = run(library_engine.whoami("kiosk", "*"))
    assert kiosk["groups"] == ["public"] and kiosk["collections"] == ["member-info"]
    assert set(kiosk["visible"]) == files("member-info")  # documents are stored under their file names
    for q in (
        "What is the hotel cap per night?",
        "How much cash is in a teller drawer?",
        "What do we do during a robbery?",
    ):
        out = run(library_engine.ask_as("kiosk", "*", q))
        assert out["answer"] == NO_ANSWER and all(s["collection"] == "member-info" for s in out["sources"]), q
    assert run(library_engine.ask_as("kiosk", "policies", "hotel cap"))["status"] == 404


@pytest.mark.parametrize(
    ("persona", "roles", "hidden"),
    [
        ("priya", ["user"], {"restricted-payments-oncall-runbook.md", "restricted-bsa-aml-escalation.md"}),
        ("dana", ["user"], {"restricted-hr-compensation-bands.md", "restricted-bsa-aml-escalation.md"}),
        ("marcus", ["user"], {"restricted-hr-compensation-bands.md", "restricted-payments-oncall-runbook.md"}),
        ("audrey", ["reader:*"], RESTRICTED),
        ("admin", ["admin"], set()),
    ],
)
def test_each_persona_reads_what_its_role_allows(library_engine, persona, roles, hidden):
    who = run(library_engine.whoami(persona, "*"))
    assert who["roles"] == roles
    assert set(who["visible"]) == files() - hidden
    assert who["can_chat"] is (persona != "audrey")  # internal audit reads; it doesn't chat


def test_every_suggested_question_cites_the_document_it_demonstrates(library_engine):
    from demo.engine import NO_ANSWER, PERSONAS, STAFF_POPULAR, STAFF_SUGGESTED

    checks = [(pid, q, doc) for pid, p in PERSONAS.items() for q, doc in p.get("suggested", []) + p.get("popular", [])]
    checks += [
        (pid, q, doc) for pid in ("admin", "priya", "dana", "marcus") for q, doc in STAFF_SUGGESTED + STAFF_POPULAR
    ]
    for pid, q, doc in checks:
        out = run(library_engine.ask_as(pid, "*", q))
        assert out["status"] == 200, (pid, q)
        if doc is None:  # a refusal the UI announces as such
            assert out["answer"] == NO_ANSWER and not cited(out), (pid, q)
            assert q in PERSONAS[pid]["notes"]
        else:
            assert cited(out) == [doc] and out["sources"][0]["title"] == doc, (pid, q, cited(out))


def test_typed_questions_from_the_audit_cite_the_right_document(library_engine):
    for q, doc, fact in (
        ("How long are records kept?", "sample-data-retention-policy.md", "seven years"),
        ("What do we do during a robbery?", "robbery-response-procedure.md", "comply"),
        ("How are BSA referrals decided?", "unusual-activity-referral.md", "BSA team"),
        ("How do I become a member?", "how-to-become-a-member.md", "photo ID"),
    ):
        out = run(library_engine.ask_as("priya", "*", q))
        assert cited(out) == [doc] and fact in out["answer"], (q, out["answer"])


def test_seeded_traffic_and_keys_match_the_access_model(library_engine):
    from demo.engine import SAMPLE_KEYS, SAMPLE_TRAFFIC

    assert run(library_engine.seed_sample_traffic()) == [200] * len(SAMPLE_TRAFFIC)
    kiosk_q = next(q for p, q in SAMPLE_TRAFFIC if p == "kiosk")
    out = run(library_engine.ask_as("kiosk", "*", kiosk_q))
    assert cited(out) == ["branch-hours-and-holidays.md"]
    library_engine.create_sample_keys()
    personas = {p["name"]: p for p in library_engine.personas()}
    for label, groups in SAMPLE_KEYS:
        who = run(library_engine.whoami(personas[label]["id"], "*"))
        assert who["groups"] == groups
        if groups == ["public"]:
            assert who["collections"] == ["member-info"]
        else:
            assert not set(who["visible"]) & RESTRICTED


def test_live_mode_suggestions_match_the_engine():
    from demo.engine import STAFF_POPULAR, STAFF_SUGGESTED

    js = (DEMO / "adapters.js").read_text()
    block = js[js.index("const LIVE_SUGGESTIONS") : js.index("};", js.index("const LIVE_SUGGESTIONS"))]
    lists = [json.loads(m) for m in re.findall(r"(\[\"[^\]]*\])\.map", block)]
    assert lists == [[q for q, _ in STAFF_SUGGESTED], [q for q, _ in STAFF_POPULAR]]


def test_all_sources_answers_from_the_right_collection(library_engine):
    out = run(library_engine.ask_as("priya", "*", "How long is an oral stop payment good for?"))
    assert out["status"] == 200 and "14 days" in out["answer"]
    assert out["sources"][0]["collection"] == "member-services" and out["sources"][0]["cited"]
    assert out["access"]["collections_searched"] == len(CATALOG["collections"])


def test_all_sources_still_hides_restricted_documents(library_engine):
    salary = "What is the level 3 salary band?"
    priya = run(library_engine.ask_as("priya", "*", salary))
    assert priya["answer"].startswith("Level 3 salary band is $77,000") and "Level 1" not in priya["answer"]
    for persona in ("dana", "marcus", "kiosk", "audrey"):
        out = run(library_engine.ask_as(persona, "*", salary))
        assert "77,000" not in json.dumps(out), persona
    ofac = "When must a confirmed OFAC match be reported?"
    assert "10 business days" in run(library_engine.ask_as("marcus", "*", ofac))["answer"]
    for persona in ("priya", "dana", "audrey", "kiosk"):
        out = run(library_engine.ask_as(persona, "*", ofac))
        assert "restricted-bsa-aml-escalation.md" not in json.dumps(out), persona


def test_off_topic_questions_are_declined_instead_of_quoting_a_passage(library_engine):
    from demo.engine import NO_ANSWER

    for q in ("What is our CEO's name?", "Who won the Super Bowl?"):
        out = run(library_engine.ask_as("priya", "*", q))
        assert out["status"] == 200 and out["answer"] == NO_ANSWER, q
        assert not any(s["cited"] for s in out["sources"])
