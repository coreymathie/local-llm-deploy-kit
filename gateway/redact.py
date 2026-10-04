# Corey Mathie, 2026
"""
Pattern-based PII redaction for audit entries (GATEWAY_REDACT_PROMPTS=true).

Covers SSNs, Luhn-valid card numbers, DOBs near a DOB marker, emails, and US
phone numbers. Pattern-level defense, not a DLP.
"""

from __future__ import annotations

import re

_RULES: tuple[tuple[str, re.Pattern, str], ...] = (
    ("ssn", re.compile(r"\b\d{3}[- ]?\d{2}[- ]?\d{4}\b"), "[REDACTED_SSN]"),
    ("pan", re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[REDACTED_PAN]"),
    (
        "dob",
        re.compile(r"\b(?:dob|birth[- ]?date|date of birth)\W{0,10}\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}\b", re.I),
        "[REDACTED_DOB]",
    ),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[REDACTED_EMAIL]"),
    ("phone", re.compile(r"\b(?:\+?1[\s.-]?)?\(?[2-9]\d{2}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b"), "[REDACTED_PHONE]"),
)


def _luhn_valid(text: str) -> bool:
    digits = [int(c) for c in text if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
    return total % 10 == 0


def redact(text: str) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    for name, pattern, replacement in _RULES:

        def _sub(m: re.Match, name: str = name, replacement: str = replacement) -> str:
            if name == "pan" and not _luhn_valid(m.group(0)):
                return m.group(0)
            counts[name] = counts.get(name, 0) + 1
            return replacement

        text = pattern.sub(_sub, text)
    return text, counts
