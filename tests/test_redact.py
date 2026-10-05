# Corey Mathie, 2026
"""PII redaction patterns (gateway/redact.py)."""

import pytest

from gateway.redact import redact


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("call (415) 555-0134 today", "call [REDACTED_PHONE] today"),  # the "(" used to be left behind
        ("call 415-555-0134 today", "call [REDACTED_PHONE] today"),
        ("call +1 415.555.0134 today", "call [REDACTED_PHONE] today"),
        ("SSN 123-45-6789", "SSN [REDACTED_SSN]"),
        ("card 4111 1111 1111 1111", "card [REDACTED_PAN]"),
        ("order 1234 5678 9012 3456", "order 1234 5678 9012 3456"),  # fails the Luhn check: not a card
        ("DOB: 04/12/1986", "[REDACTED_DOB]"),
        ("mail jordan.lee@example.org", "mail [REDACTED_EMAIL]"),
    ],
)
def test_patterns(text, expected):
    assert redact(text)[0] == expected


def test_counts_by_type():
    _, counts = redact("a@b.co, c@d.co and 123-45-6789")
    assert counts == {"email": 2, "ssn": 1}
