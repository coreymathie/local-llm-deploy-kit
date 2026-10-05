# Corey Mathie, 2026
"""Deployment files: the default compose stack runs without a model, Ollama and vLLM are opt-in, ports stay local."""

from pathlib import Path

import pytest

from gateway.config import Settings

yaml = pytest.importorskip("yaml")
ROOT = Path(__file__).resolve().parent.parent


def _compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def test_default_stack_is_gateway_and_simulated_backend_and_real_backends_are_opt_in():
    """v0.7: `docker compose up` needs no model (mock-llm, simulated); Ollama and vLLM are profiles."""
    services = _compose()["services"]
    default = {name for name, svc in services.items() if not svc.get("profiles")}
    assert default == {"mock-llm", "gateway"}
    assert services["vllm"]["profiles"] == ["vllm"] and services["ollama"]["profiles"] == ["ollama"]
    assert services["mock-llm"]["environment"]["MOCK_MODE"] == "simulated"
    assert "scripts.mock_openai_server:app" in services["mock-llm"]["command"]
    assert "ports" not in services["mock-llm"]  # only reachable from the gateway
    env = services["gateway"]["environment"]
    assert env["BACKEND"] == "${BACKEND:-openai_compatible}"
    assert env["OPENAI_COMPAT_BASE_URL"] == "${OPENAI_COMPAT_BASE_URL:-http://mock-llm:8001/v1}"
    assert env["OLLAMA_HOST"] == "http://ollama:11434"
    # The gateway itself still defaults to Ollama; the compose env file switches the stack to it.
    assert Settings.model_fields["BACKEND"].default == "ollama"
    ollama = dict(
        line.split("=", 1)
        for line in (ROOT / "profiles" / "compose-ollama.env").read_text().splitlines()
        if line and not line.startswith("#")
    )
    assert ollama == {
        "BACKEND": "ollama",
        "GATEWAY_DEFAULT_MODEL": "llama3.1:8b",
        "GATEWAY_EMBED_MODEL": "nomic-embed-text",
    }


def test_compose_image_ships_the_console_and_the_mock_backend():
    text = (ROOT / "Dockerfile").read_text()
    for path in ("gateway", "demo", "scripts"):
        assert f"COPY {path} /app/{path}" in text
    assert _compose()["services"]["gateway"]["build"]["dockerfile"] == "Dockerfile"


def test_every_published_port_is_bound_to_localhost():
    for name, svc in _compose()["services"].items():
        for port in svc.get("ports", []):
            assert port.startswith("127.0.0.1:"), (name, port)


def test_env_example_and_profiles_only_use_known_settings():
    known = set(Settings.model_fields)
    for path in (ROOT / ".env.example", ROOT / "profiles" / "healthcare.env", ROOT / "profiles" / "finance.env"):
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                assert line.split("=", 1)[0] in known, (path.name, line)


def test_env_example_parses_without_comments_leaking_into_values():
    """python-dotenv reads `KEY=   # note` as the value "# note"; empty settings keep comments on their own line."""
    parsed = Settings(_env_file=str(ROOT / ".env.example"))
    defaults = Settings(_env_file=None)
    for name in Settings.model_fields:
        value = getattr(parsed, name)
        assert not (isinstance(value, str) and value.startswith("#")), name
    assert parsed.GATEWAY_OIDC_ISSUER == defaults.GATEWAY_OIDC_ISSUER == ""
    assert parsed.GATEWAY_OIDC_GROUP_ROLES == {"llm-admins": ["admin"], "staff": ["user"]}


def test_versions_agree_across_package_gateway_chart_and_changelog():
    import re as _re

    from gateway.main import __version__

    pyproject = _re.search(r'^version = "(.+)"', (ROOT / "pyproject.toml").read_text(), _re.M).group(1)
    chart = yaml.safe_load((ROOT / "deploy" / "helm" / "local-llm-gateway" / "Chart.yaml").read_text())
    changelog = _re.search(r"^## \[(.+?)\]", (ROOT / "CHANGELOG.md").read_text(), _re.M).group(1)
    assert pyproject == __version__ == chart["appVersion"] == chart["version"] == changelog
    values = yaml.safe_load((ROOT / "deploy" / "helm" / "local-llm-gateway" / "values.yaml").read_text())
    assert values["image"]["tag"] == __version__
