# Corey Mathie, 2026
"""Docs must point at code and tests that exist: every `test_x.py::name` and `gateway/x.py::name` resolves."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = [*ROOT.glob("docs/**/*.md"), ROOT / "README.md"]


def _defs(path: Path) -> set[str]:
    src = path.read_text()
    return set(re.findall(r"^(?:async def|def|class) (\w+)", src, re.M)) | set(re.findall(r"^(\w+) = ", src, re.M))


def test_test_references_resolve():
    missing = []
    for doc in DOCS:
        for line in doc.read_text().splitlines():
            current = None
            for m in re.finditer(r"(test_\w+\.py)(?:::(test_\w+))?|(?<![\w.])::(test_\w+)", line):
                if m.group(1):
                    current = ROOT / "tests" / m.group(1)
                    if not current.exists():
                        missing.append(f"{doc.name}: {m.group(1)}")
                        continue
                name = m.group(2) or m.group(3)
                if name and current and name not in _defs(current):
                    missing.append(f"{doc.name}: {current.name}::{name}")
    assert not missing, missing


def test_code_references_resolve():
    missing = []
    for doc in DOCS:
        for path, name in re.findall(r"`((?:gateway|scripts|demo)/\w+\.py)(?:::(\w+))?`", doc.read_text()):
            f = ROOT / path
            if not f.exists() or (name and name not in _defs(f)):
                missing.append(f"{doc.name}: {path}::{name}")
    assert not missing, missing
