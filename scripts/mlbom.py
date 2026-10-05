# Corey Mathie, 2026
"""
Generate a CycloneDX 1.6 ML-BOM (JSON) for this gateway: the pinned models from the lock file and the
Python packages it runs on.

    python scripts/mlbom.py --lock models.lock.json --out mlbom.cdx.json
    python scripts/mlbom.py --lock models.lock.json --verify     # also record live verification status

Models become `machine-learning-model` components with their pinned SHA-256 (Ollama manifest digest, or
nested `file` components with per-file hashes for weight files), source, license and purpose, plus a
`lldk:verification` property when --verify asks the backend. Python packages are `library` components
with purl, version and license as declared in their installed metadata: the direct requirements in
requirements.txt and everything they require, resolved from what is installed in this environment.

The BOM records what was pinned and observed; it is not a signature and doesn't prove where a model
came from. Validated against the CycloneDX 1.6 JSON schema in tests (tests/test_supply_chain.py).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import uuid
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway import supply_chain  # noqa: E402

GATEWAY_REF = "private-llm-platform"


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_names(path: Path) -> list[str]:
    names = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.append(re.split(r"[\s\[<>=!~;]", line, maxsplit=1)[0])
    return names


def _license(dist: metadata.Distribution) -> list[dict]:
    meta = dist.metadata
    expr = meta.get("License-Expression")
    if expr:
        return [{"license": {"name": expr}}]
    classifiers = [c.split(" :: ")[-1] for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    if classifiers:
        return [{"license": {"name": c}} for c in dict.fromkeys(classifiers)]
    text = (meta.get("License") or "").strip()
    if text and len(text) <= 80 and "\n" not in text:  # some packages put the whole license text here
        return [{"license": {"name": text}}]
    return []


def python_components(requirements: Path) -> tuple[list[dict], list[dict], list[str]]:
    """Library components, their dependency edges, and the refs of the direct requirements."""
    try:
        from packaging.requirements import Requirement
    except ImportError:  # pragma: no cover - packaging ships with pip/setuptools environments
        Requirement = None

    components: dict[str, dict] = {}
    edges: dict[str, list[str]] = {}
    direct: list[str] = []

    def visit(name: str) -> str | None:
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            return None
        pname, version = dist.metadata["Name"], dist.version
        ref = f"pkg:pypi/{_norm(pname)}@{version}"
        if ref in components:
            return ref
        components[ref] = {
            "type": "library",
            "bom-ref": ref,
            "name": pname,
            "version": version,
            "purl": ref,
            **({"licenses": lic} if (lic := _license(dist)) else {}),
        }
        children = []
        for raw in dist.requires or []:
            if Requirement is None:
                continue
            req = Requirement(raw)
            if req.marker and not req.marker.evaluate({"extra": ""}):
                continue  # optional extras and other-platform dependencies are not installed requirements
            child = visit(req.name)
            if child:
                children.append(child)
        edges[ref] = sorted(set(children))
        return ref

    for name in _requirement_names(requirements):
        ref = visit(name)
        if ref:
            direct.append(ref)
    deps = [{"ref": ref, "dependsOn": kids} for ref, kids in sorted(edges.items())]
    return sorted(components.values(), key=lambda c: c["bom-ref"]), deps, sorted(set(direct))


def model_components(pins: list[supply_chain.Pin], verification: dict | None) -> list[dict]:
    out = []
    statuses = (verification or {}).get("models", {})
    for pin in pins:
        ref = f"model:{pin.backend}:{pin.name}"
        comp: dict = {"type": "machine-learning-model", "bom-ref": ref, "name": pin.name}
        if pin.digest:
            comp["hashes"] = [{"alg": "SHA-256", "content": pin.digest}]
        if pin.license:
            comp["licenses"] = [{"license": {"name": pin.license}}]
        if pin.source:
            comp["externalReferences"] = [{"type": "distribution", "url": pin.source}]
        if pin.purpose:
            comp["modelCard"] = {"modelParameters": {"task": pin.purpose}}
        if pin.files:
            comp["components"] = [
                {
                    "type": "file",
                    "bom-ref": f"{ref}:file:{i}",
                    "name": Path(f["path"]).name,
                    "hashes": [{"alg": "SHA-256", "content": supply_chain._norm_digest(f["sha256"])}],
                    "properties": [{"name": "lldk:path", "value": f["path"]}],
                }
                for i, f in enumerate(pin.files)
            ]
        props = [{"name": "lldk:backend", "value": pin.backend}]
        if pin.backend == "ollama" and pin.digest:
            props.append({"name": "lldk:digest-kind", "value": "ollama-manifest-sha256"})
        if pin.name in statuses:
            props.append({"name": "lldk:verification", "value": statuses[pin.name]["status"]})
            for k, v in sorted((statuses[pin.name].get("info") or {}).items()):
                if isinstance(v, str | int | float) and v != "":
                    props.append({"name": f"lldk:ollama:{k}", "value": str(v)})
        comp["properties"] = props
        out.append(comp)
    return out


def build(lock: Path | None, requirements: Path, verification: dict | None = None, version: str = "") -> dict:
    pins = supply_chain.load_lock(lock) if lock else []
    models = model_components(pins, verification)
    libs, lib_deps, direct = python_components(requirements)
    if not version:
        from gateway.main import __version__ as version
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "tools": {
                "components": [
                    {"type": "application", "name": "private-llm-platform scripts/mlbom.py", "version": version}
                ]
            },
            "component": {
                "type": "application",
                "bom-ref": GATEWAY_REF,
                "name": "private-llm-platform",
                "version": version,
            },
        },
        "components": models + libs,
        "dependencies": [{"ref": GATEWAY_REF, "dependsOn": sorted([m["bom-ref"] for m in models] + direct)}, *lib_deps],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lock", type=Path, default=None, help="model lock file (default: GATEWAY_MODEL_LOCK_FILE)")
    ap.add_argument("--requirements", type=Path, default=ROOT / "requirements.txt")
    ap.add_argument("--verify", action="store_true", help="ask the configured backend and record verification status")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    from gateway.config import settings

    lock = args.lock or (Path(settings.GATEWAY_MODEL_LOCK_FILE) if settings.GATEWAY_MODEL_LOCK_FILE else None)
    verification = None
    if args.verify:
        from gateway import backends

        async def _run():
            try:
                return await supply_chain.verify(backends.get_backend(), supply_chain.load_lock(lock) if lock else [])
            finally:
                await backends.aclose_clients()

        verification = asyncio.run(_run())
    bom = json.dumps(build(lock, args.requirements, verification), indent=2) + "\n"
    if args.out:
        args.out.write_text(bom)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(bom)
    return 0


if __name__ == "__main__":
    sys.exit(main())
