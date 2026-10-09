# Corey Mathie, 2026
"""The console's sample-company data: reproducible, internally consistent, and labelled fictional."""

import json
from pathlib import Path

import pytest

from scripts import generate_sample_company as gen

ROOT = Path(__file__).resolve().parents[1]
DATA = json.loads((ROOT / "demo" / "data" / "sample_company.json").read_text())


def test_committed_file_matches_the_generator():
    assert gen.main(["--check"]) == 0


def test_generation_is_deterministic():
    assert gen.render(gen.build()) == gen.render(gen.build())


def test_daily_numbers_add_up():
    assert len(DATA["days"]) == gen.DAYS
    staff = DATA["company"]["employees"]
    for d in DATA["days"]:
        assert d["answered"] + d["no_answer"] == d["questions"]
        assert 0 <= d["active_users"] <= staff
        assert d["restricted_withheld"] <= d["questions"]
        assert d["p50_ms"] < d["p95_ms"]


def test_headcounts_sum_to_employees_and_departments_match_the_catalog():
    catalog = json.loads((ROOT / "demo" / "data" / "library.json").read_text())
    assert sum(x["headcount"] for x in DATA["departments"]) == DATA["company"]["employees"] == 340
    assert sorted(x["department"] for x in DATA["departments"]) == sorted(catalog["departments"])
    heads = {x["department"]: x["headcount"] for x in DATA["departments"]}
    assert heads["Facilities and security"] < heads["IT and digital banking"]  # a small team, not 13% of staff
    for x in DATA["departments"]:
        assert 0 < x["active_users_30d"] <= x["headcount"]


def test_thirty_day_actives_are_at_least_the_busiest_day():
    monthly = sum(x["active_users_30d"] for x in DATA["departments"])
    assert max(d["active_users"] for d in DATA["days"]) <= monthly


@pytest.mark.parametrize("days", [7, 30, 90])
def test_topics_for_any_range_add_up_to_the_questions_asked(days):
    rows = DATA["days"][-days:]
    asked = sum(d["questions"] for d in rows)
    assert sum(sum(d["topics"].values()) for d in rows) == asked
    if days == 30:
        assert sum(t["questions_30d"] for t in DATA["topics"]) == asked
        assert sum(x["questions_30d"] for x in DATA["departments"]) == asked


def test_topics_are_asked_only_by_departments_that_can_read_them():
    by_dept = {x["department"]: x["questions_30d"] for x in DATA["departments"]}
    restricted = [t for t in DATA["topics"] if t["asked_by"] != "All staff"]
    assert restricted
    for t in restricted:
        assert t["questions_30d"] <= sum(by_dept[d] for d in t["asked_by"]), t["topic"]
    bsa = next(t for t in DATA["topics"] if t["topic"].startswith("BSA/AML"))
    assert bsa["asked_by"] == ["Compliance and BSA"] and 0 < bsa["questions_30d"] < by_dept["Compliance and BSA"]


def test_it_is_labelled_fictional_and_assumptions_are_stated():
    assert DATA["company"]["fictional"] is True
    assert "not measurements" in DATA["disclaimer"]
    assert DATA["assumptions"]["minutes_saved_per_answer"] > 0


def test_recent_activity_names_documents_that_exist():
    catalog = json.loads((ROOT / "demo" / "data" / "library.json").read_text())
    titles = {d["title"] for d in catalog["documents"]}
    bsa = next(n for n in DATA["notable"] if "BSA" in n["detail"])
    assert "Unusual Activity Referral Policy" in bsa["detail"] and "Unusual Activity Referral Policy" in titles
