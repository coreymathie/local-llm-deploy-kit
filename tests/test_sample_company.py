# Corey Mathie, 2026
"""The console's sample-company data: reproducible, internally consistent, and labelled fictional."""

import json
from pathlib import Path

from scripts import generate_sample_company as gen

ROOT = Path(__file__).resolve().parents[1]


def test_committed_file_matches_the_generator():
    assert gen.main(["--check"]) == 0


def test_generation_is_deterministic():
    assert gen.render(gen.build()) == gen.render(gen.build())


def test_daily_numbers_add_up():
    data = gen.build()
    assert len(data["days"]) == gen.DAYS
    staff = data["company"]["employees"]
    for d in data["days"]:
        assert d["answered"] + d["no_answer"] == d["questions"]
        assert 0 <= d["active_users"] <= staff
        assert d["restricted_withheld"] <= d["questions"]
        assert d["p50_ms"] < d["p95_ms"]
    assert sum(x["headcount"] for x in data["departments"]) == staff


def test_it_is_labelled_fictional_and_assumptions_are_stated():
    data = json.loads((ROOT / "demo" / "data" / "sample_company.json").read_text())
    assert data["company"]["fictional"] is True
    assert "not measurements" in data["disclaimer"]
    assert data["assumptions"]["minutes_saved_per_answer"] > 0
