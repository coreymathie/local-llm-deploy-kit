# Corey Mathie, 2026
"""Deployment files: the default compose stack is unchanged, vLLM is opt-in, ports stay on localhost."""

from pathlib import Path

import pytest

from gateway.config import Settings

yaml = pytest.importorskip("yaml")
ROOT = Path(__file__).resolve().parent.parent


def _compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def test_vllm_is_an_opt_in_profile_and_default_stack_is_ollama():
    services = _compose()["services"]
    default = {name for name, svc in services.items() if not svc.get("profiles")}
    assert default == {"ollama", "gateway"}
    assert services["vllm"]["profiles"] == ["vllm"]
    env = services["gateway"]["environment"]
    assert env["BACKEND"] == "${BACKEND:-ollama}"
    assert env["OPENAI_COMPAT_BASE_URL"].endswith("http://vllm:8000/v1}")


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
