# Corey Mathie, 2026
"""scripts/airgap_bundle.sh: dry run plans everything and writes nothing; a real run (no network:
wheels, images and Hugging Face skipped) bundles Ollama blobs, source, chart and ML-BOM with checksums
that --verify checks."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "airgap_bundle.sh"


def run(*args, cwd=None):
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, cwd=cwd)


def fake_ollama_store(root: Path, name="llama3.1", tag="8b") -> list[str]:
    """An Ollama model store with one model: manifest + config + two layers, as `ollama pull` leaves it."""
    blobs = {}
    for part in (b"config", b"weights-gguf", b"template"):
        digest = "sha256:" + hashlib.sha256(part).hexdigest()
        (root / "blobs").mkdir(parents=True, exist_ok=True)
        (root / "blobs" / digest.replace(":", "-")).write_bytes(part)
        blobs[part] = digest
    manifest = {
        "schemaVersion": 2,
        "config": {"digest": blobs[b"config"]},
        "layers": [{"digest": blobs[b"weights-gguf"]}, {"digest": blobs[b"template"]}],
    }
    mdir = root / "manifests" / "registry.ollama.ai" / "library" / name
    mdir.mkdir(parents=True)
    (mdir / tag).write_text(json.dumps(manifest))
    return list(blobs.values())


def test_dry_run_plans_every_step_and_writes_nothing(tmp_path):
    r = run(
        "--dry-run", "--out", str(tmp_path / "b"),
        "--image", "registry.example.internal/local-llm-gateway:0.6.0", "--image", "vllm/vllm-openai:v0.6.6",
        "--ollama-model", "llama3.1:8b", "--ollama-dir", str(tmp_path / "none"),
        "--hf-model", "Qwen/Qwen2.5-7B-Instruct", "--platform", "manylinux2014_x86_64", "--python-version", "3.12",
        cwd=tmp_path,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "--only-binary=:all: --platform manylinux2014_x86_64 --python-version 3.12" in out
    assert out.count("+ docker save") == 2 and "vllm_vllm-openai_v0.6.6.tar" in out
    assert "+ huggingface-cli download Qwen/Qwen2.5-7B-Instruct" in out
    assert "no manifest" in out and "ollama pull llama3.1:8b" in out  # reported, not fatal, in a dry run
    assert "scripts/mlbom.py" in out and "SHA256SUMS" in out and "dry run complete" in out
    assert not (tmp_path / "b").exists() and list(tmp_path.iterdir()) == []


def test_real_bundle_has_checksums_that_verify_and_catch_tampering(tmp_path):
    store = tmp_path / "ollama"
    digests = fake_ollama_store(store)
    out = tmp_path / "bundle"
    r = run("--out", str(out), "--no-wheels", "--ollama-model", "llama3.1:8b", "--ollama-dir", str(store))
    assert r.returncode == 0, r.stderr + r.stdout
    for d in digests:
        assert (out / "ollama" / "blobs" / d.replace(":", "-")).is_file()
    assert (out / "ollama" / "manifests" / "registry.ollama.ai" / "library" / "llama3.1" / "8b").is_file()
    sources = sorted(p.name for p in (out / "source").iterdir())
    assert any(n.startswith("private-llm-platform-") for n in sources) and any(
        n.startswith("local-llm-gateway-chart-") for n in sources
    )
    bom = json.loads((out / "mlbom.cdx.json").read_text())
    assert bom["bomFormat"] == "CycloneDX" and any(c["type"] == "library" for c in bom["components"])
    sums = (out / "SHA256SUMS").read_text().splitlines()
    files = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file() and p.name != "SHA256SUMS"}
    assert {line.split("  ", 1)[1].removeprefix("./") for line in sums} == files
    assert "INSTALL.txt" in files

    ok = run("--verify", str(out))
    assert ok.returncode == 0 and "bundle verified" in ok.stdout
    blob = out / "ollama" / "blobs" / digests[1].replace(":", "-")
    blob.write_bytes(b"swapped weights")
    bad = run("--verify", str(out))
    assert bad.returncode != 0 and "checksum mismatch" in bad.stderr


def test_missing_blob_or_non_empty_output_fails(tmp_path):
    store = tmp_path / "ollama"
    digests = fake_ollama_store(store)
    (store / "blobs" / digests[1].replace(":", "-")).unlink()
    r = run("--out", str(tmp_path / "b1"), "--no-wheels", "--ollama-model", "llama3.1:8b", "--ollama-dir", str(store))
    assert r.returncode != 0 and "missing blob" in r.stderr
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "x").write_text("x")
    r = run("--out", str(busy), "--no-wheels")
    assert r.returncode != 0 and "not empty" in r.stderr


def test_script_passes_shellcheck():
    if not shutil.which("shellcheck"):
        pytest.skip("shellcheck not installed (CI runs it)")
    r = subprocess.run(["shellcheck", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
