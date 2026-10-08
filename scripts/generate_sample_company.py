# Corey Mathie, 2026
"""
Write demo/data/sample_company.json: 90 days of usage of the private assistant at a fictional
mid-size credit union, so the console's Business impact view shows the platform at a realistic
scale.

Cypress Harbor Credit Union does not exist. Every number in the file is generated here from a
fixed seed and the assumptions below; none of it is a measurement of this repo or of any real
institution. The console labels it "Sample company data" wherever it appears, and keeps it apart
from the measured eval results (Evals) and the requests made in the browser (This session).

    python scripts/generate_sample_company.py            # write the file
    python scripts/generate_sample_company.py --check    # exit 1 if the committed file differs

The model, in short:
- 340 employees, all signed in through SSO. Weekly active users grow after launch (a typical
  adoption curve), and questions follow the working week.
- Most questions are answered from documents with citations; some find nothing in the documents
  the asker may read, and a small share touch documents the asker isn't cleared for, which the
  retrieval layer filters out before scoring.
- Time saved uses a stated minutes-per-answer assumption and a loaded hourly cost; the
  infrastructure cost is a stated monthly figure for one on-prem GPU server.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "demo" / "data" / "sample_company.json"
SEED = 20261008
END = date(2026, 10, 7)
DAYS = 90

COMPANY = {
    "name": "Cypress Harbor Credit Union",
    "short": "Cypress Harbor CU",
    "fictional": True,
    "industry": "Credit union (financial services)",
    "headquarters": "Fort Lauderdale, Florida",
    "members": 92400,
    "assets_usd": 1_400_000_000,
    "employees": 340,
    "branches": 11,
    "deployment": "One on-prem GPU server (2 x NVIDIA L40S) running vLLM; SSO through the credit union's IdP",
    "regulators": ["NCUA", "CFPB", "Florida OFR"],
}

# Department: headcount, questions per active user per workday, share of questions answered
DEPARTMENTS = [
    ("Member services and branches", 158, 3.1),
    ("Lending", 42, 3.6),
    ("IT and digital banking", 31, 2.4),
    ("Finance and accounting", 16, 2.2),
    ("Fraud and risk", 14, 4.1),
    ("Marketing", 10, 1.6),
    ("Compliance and BSA", 9, 4.6),
    ("Executive", 9, 1.2),
    ("Human resources", 8, 2.9),
    ("Facilities and security", 43, 0.9),
]

TOPICS = [
    ("Card disputes and provisional credit", 0.16),
    ("Lending limits and exceptions", 0.13),
    ("Identity verification and contact changes", 0.12),
    ("Wire transfers and callbacks", 0.09),
    ("Complaint handling and escalation", 0.08),
    ("Benefits, leave and pay", 0.08),
    ("Records retention", 0.06),
    ("Branch security procedures", 0.06),
    ("IT and security policy", 0.07),
    ("Travel and expenses", 0.05),
    ("BSA/AML procedures (compliance only)", 0.04),
    ("Other", 0.06),
]

LIBRARY = ROOT / "demo" / "data" / "library.json"  # written by scripts/sample_library.py
ACCESS = {
    "policies": "All staff; compensation: HR only; payments runbook: engineering only; BSA procedure: compliance only",
    "member-services": "All staff",
    "lending": "All staff",
    "compliance": "All staff; OFAC screening: compliance only",
    "branch-operations": "All staff, and the lobby kiosk",
}

ASSUMPTIONS = {
    "minutes_saved_per_answer": 6,
    "loaded_cost_per_hour_usd": 52,
    "infrastructure_per_month_usd": 4200,
    "note": "Minutes saved per cited answer (versus searching shared drives or asking a colleague), "
    "the loaded hourly cost of an employee, and the monthly cost of the on-prem GPU server, power and "
    "support. Illustrative assumptions for the sample company, not measurements.",
}


def _day(rng: random.Random, d: date, i: int) -> dict:
    progress = i / (DAYS - 1)
    adoption = 0.52 + 0.30 * (1 - math.exp(-2.6 * progress))  # share of staff active on a workday
    weekday = d.weekday()
    workday = weekday < 5 and d != date(2026, 9, 7)  # Labor Day
    active = int(sum(n for _, n, _ in DEPARTMENTS) * adoption * (1 if workday else 0.07) * rng.uniform(0.94, 1.05))
    per_user = sum(n * q for _, n, q in DEPARTMENTS) / sum(n for _, n, _ in DEPARTMENTS)
    questions = int(active * per_user * rng.uniform(0.9, 1.1) * ([1.12, 1.0, 1.0, 0.98, 0.86, 1, 1][weekday]))
    no_answer_rate = 0.11 - 0.04 * progress + rng.uniform(-0.01, 0.01)  # the library fills its gaps
    withheld_rate = rng.uniform(0.018, 0.03)
    no_answer = int(questions * no_answer_rate)
    withheld = int(questions * withheld_rate)
    answered = questions - no_answer
    return {
        "date": d.isoformat(),
        "active_users": active,
        "questions": questions,
        "answered": answered,
        "no_answer": no_answer,
        "restricted_withheld": withheld,
        "pii_redacted": int(questions * rng.uniform(0.012, 0.02)),
        "injection_flagged": rng.randint(0, 2) if workday else 0,
        "thumbs_up": int(answered * rng.uniform(0.18, 0.24) * (0.86 + 0.06 * progress)),
        "thumbs_down": int(answered * rng.uniform(0.18, 0.24) * (0.14 - 0.06 * progress)),
        "p50_ms": int(rng.uniform(1650, 2050) - 220 * progress),
        "p95_ms": int(rng.uniform(4100, 4900) - 500 * progress),
    }


def build() -> dict:
    rng = random.Random(SEED)
    start = END - timedelta(days=DAYS - 1)
    days = [_day(rng, start + timedelta(days=i), i) for i in range(DAYS)]
    last30 = days[-30:]
    q30 = sum(d["questions"] for d in last30)
    weights = [n * q for _, n, q in DEPARTMENTS]
    departments = []
    for (name, headcount, _), w in zip(DEPARTMENTS, weights, strict=True):
        share = w / sum(weights)
        adoption = min(0.97, 0.62 + 0.3 * rng.random() if headcount > 12 else 0.7 + 0.25 * rng.random())
        departments.append(
            {
                "department": name,
                "headcount": headcount,
                "active_users_30d": int(headcount * adoption),
                "questions_30d": int(q30 * share * rng.uniform(0.95, 1.05)),
                "answer_rate": round(rng.uniform(0.86, 0.95), 3),
            }
        )
    topics = [{"topic": t, "questions_30d": int(q30 * s * rng.uniform(0.94, 1.06))} for t, s in TOPICS]
    library = json.loads(LIBRARY.read_text())
    collections = [
        {
            "collection": c["label"],
            "name": c["name"],
            "documents": c["documents"],
            "passages": c["passages"],
            "access": ACCESS[c["name"]],
        }
        for c in library["collections"]
    ]
    notable = [
        {
            "date": "2026-10-05",
            "kind": "access",
            "title": "Restricted procedure stayed restricted",
            "detail": "A branch employee asked how BSA referrals are decided. The BSA procedure was filtered out "
            "before retrieval; the answer cited the all-staff referral policy instead.",
        },
        {
            "date": "2026-10-01",
            "kind": "ops",
            "title": "October auto loan rate sheet published and re-indexed",
            "detail": "The new rate sheet replaced September's overnight; questions on auto loan rates now cite the "
            "October version.",
        },
        {
            "date": "2026-09-22",
            "kind": "compliance",
            "title": "Model pin verified after a server patch",
            "detail": "The served model's digest matched the lock file after OS patching; the ML-BOM was attached "
            "to the change ticket for the IT examination file.",
        },
        {
            "date": "2026-09-08",
            "kind": "access",
            "title": "Quarterly access review completed",
            "detail": "HR and compliance reviewed every SSO group-to-role mapping and document access list; two "
            "departed contractors' API keys were revoked.",
        },
        {
            "date": "2026-08-18",
            "kind": "ops",
            "title": "Prompt-injection text found in an uploaded vendor PDF",
            "detail": "The answer kept the vendor's terms but ignored the embedded instructions; the document was "
            "flagged for the vendor-management team.",
        },
    ]
    return {
        "generated_by": "scripts/generate_sample_company.py",
        "seed": SEED,
        "disclaimer": "Fictional sample company. Generated data for demonstration, not measurements.",
        "period": {"start": start.isoformat(), "end": END.isoformat(), "days": DAYS},
        "company": COMPANY,
        "assumptions": ASSUMPTIONS,
        "days": days,
        "departments": sorted(departments, key=lambda x: -x["questions_30d"]),
        "topics": sorted(topics, key=lambda x: -x["questions_30d"]),
        "collections": collections,
        "compliance": {
            "data_sent_to_external_ai": 0,
            "sso_coverage": 1.0,
            "audit_chain_verified_rate": 1.0,
            "model_pin_verified_rate": 1.0,
            "encryption_at_rest": True,
            "access_reviews_on_schedule": True,
        },
        "notable": notable,
    }


def render(data: dict) -> str:
    return json.dumps(data, indent=1) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if the committed file is stale")
    args = ap.parse_args(argv)
    text = render(build())
    if args.check:
        if not OUT.exists() or OUT.read_text() != text:
            print(f"{OUT.relative_to(ROOT)} is stale: run python scripts/generate_sample_company.py")
            return 1
        print(f"{OUT.relative_to(ROOT)} is current")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text)
    d = json.loads(text)["days"][-30:]
    print(f"wrote {OUT.relative_to(ROOT)}: {sum(x['questions'] for x in d):,} questions in the last 30 days")
    return 0


if __name__ == "__main__":
    sys.exit(main())
