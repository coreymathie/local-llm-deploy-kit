# Corey Mathie, 2026
"""The folder ingest script, run against the in-process gateway."""

from contextlib import nullcontext

import pytest

from scripts import ingest_folder

from .conftest import ADMIN


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "handbook"
    (root / "hr").mkdir(parents=True)
    (root / "hr" / "pto.md").write_text("Paid time off.\n\nFull-time employees receive 20 vacation days per year.")
    (root / "expenses.txt").write_text("Submit receipts within 30 days.")
    (root / "setup.exe").write_bytes(b"MZ")
    (root / ".git").mkdir()
    (root / ".git" / "notes.md").write_text("should be ignored")
    return root


def _run(client, monkeypatch, folder, *extra):
    # The script opens `with httpx.Client(...)`; hand it the already-running test client instead.
    monkeypatch.setattr(ingest_folder.httpx, "Client", lambda **kw: nullcontext(client))
    client.headers.update(ADMIN)
    return ingest_folder.main([str(folder), "--collection", "handbook", "--key", "sk-local-admin", *extra])


def test_ingest_adds_supported_files_and_skips_what_is_already_there(client, monkeypatch, folder, capsys):
    assert _run(client, monkeypatch, folder) == 0
    titles = sorted(d["title"] for d in client.get("/v1/collections/handbook/documents").json())
    assert titles == ["expenses.txt", "hr/pto.md"]

    assert _run(client, monkeypatch, folder) == 0
    assert "0 added, 2 already present" in capsys.readouterr().out

    assert _run(client, monkeypatch, folder, "--replace") == 0
    assert len(client.get("/v1/collections/handbook/documents").json()) == 2


def test_dry_run_lists_files_without_uploading(client, monkeypatch, folder, capsys):
    assert _run(client, monkeypatch, folder, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "would upload hr/pto.md" in out and "setup.exe" not in out and ".git" not in out
    assert client.get("/v1/collections/handbook/documents").json() == []
