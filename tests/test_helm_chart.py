# Corey Mathie, 2026
"""
Helm chart checks without helm (it couldn't be installed in the build environment; CI runs `helm lint`
and `helm template` too). values.yaml and the example values files are validated against
values.schema.json, and the templates are rendered by tests/helm_render/render.go, a stdlib-only Go
text/template harness implementing the Helm/Sprig functions the chart uses (not helm itself), then
the rendered manifests are parsed and checked.
"""

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from gateway.config import Settings

yaml = pytest.importorskip("yaml")
jsonschema = pytest.importorskip("jsonschema")

ROOT = Path(__file__).resolve().parent.parent
CHART = ROOT / "deploy" / "helm" / "local-llm-gateway"
FULL = "gw-local-llm-gateway"


def values() -> dict:
    return yaml.safe_load((CHART / "values.yaml").read_text())


def merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def settings_from_env(data: dict, monkeypatch) -> Settings:
    """Read a rendered ConfigMap the way the container would: as environment variables."""
    for k, v in data.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


def schema_errors(v: dict) -> list[str]:
    schema = json.loads((CHART / "values.schema.json").read_text())
    return [e.message for e in jsonschema.Draft7Validator(schema).iter_errors(v)]


@pytest.fixture(scope="session")
def renderer(tmp_path_factory):
    if not shutil.which("go"):
        pytest.skip("go toolchain not installed")
    binary = tmp_path_factory.mktemp("render") / "render"
    subprocess.run(["go", "build", "-o", str(binary), "render.go"], cwd=ROOT / "tests" / "helm_render", check=True)
    chart_meta = {k[0].upper() + k[1:]: v for k, v in yaml.safe_load((CHART / "Chart.yaml").read_text()).items()}

    def render(override: dict | None = None) -> tuple[dict, str]:
        """Returns ({"Kind/name": manifest}, error message or "")."""
        data = {
            "Values": merge(values(), override or {}),
            "Chart": chart_meta,
            "Release": {"Name": "gw", "Namespace": "llm", "Service": "Helm"},
        }
        inp = tmp_path_factory.mktemp("in") / "in.json"
        inp.write_text(json.dumps(data))
        p = subprocess.run([str(binary), str(CHART), str(inp)], capture_output=True, text=True)
        if p.returncode:
            return {}, p.stderr
        docs = {}
        for name, text in json.loads(p.stdout).items():
            if not name.endswith(".yaml"):
                continue
            for doc in yaml.safe_load_all(text):
                if doc:
                    docs[f"{doc['kind']}/{doc['metadata']['name']}"] = doc
        return docs, ""

    return render


def test_chart_metadata_and_values_files_are_valid():
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
    assert (
        chart["apiVersion"] == "v2" and chart["name"] == "local-llm-gateway" and chart["version"] == chart["appVersion"]
    )
    jsonschema.Draft7Validator.check_schema(json.loads((CHART / "values.schema.json").read_text()))
    assert schema_errors(values()) == []
    for example in sorted(CHART.glob("values-*.yaml")):
        assert schema_errors(merge(values(), yaml.safe_load(example.read_text()))) == [], example.name


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"replicaCount": 2}, "maximum"),
        ({"image": {"tag": "latest"}}, "should not be valid"),
        ({"vllm": {"image": {"tag": "latest"}}}, "should not be valid"),
        ({"backend": {"type": "tgi"}}, "is not one of"),
        ({"gateway": {"oidc": {"algorithms": "HS256"}}}, "does not match"),
        ({"gateway": {"oidc": {"groupRoles": {"x": ["superuser"]}}}}, "does not match"),
        ({"gateway": {"modelPolicy": "strict"}}, "is not one of"),
        ({"persistence": {"size": "ten gigs"}}, "does not match"),
        ({"unknownKey": 1}, "Additional properties"),
    ],
)
def test_schema_rejects_unsafe_or_invalid_values(override, fragment):
    errors = schema_errors(merge(values(), override))
    assert any(fragment in e for e in errors), errors


def test_default_render_is_a_hardened_single_replica_gateway(renderer):
    docs, err = renderer()
    assert err == ""
    assert set(docs) == {
        f"ConfigMap/{FULL}",
        f"Secret/{FULL}-secrets",
        f"PersistentVolumeClaim/{FULL}-data",
        f"Service/{FULL}",
        f"Deployment/{FULL}",
        f"NetworkPolicy/{FULL}",
        f"Pod/{FULL}-test-connection",
    }
    dep = docs[f"Deployment/{FULL}"]
    spec, pod = dep["spec"], dep["spec"]["template"]["spec"]
    assert spec["replicas"] == 1 and spec["strategy"]["type"] == "Recreate"
    assert pod["automountServiceAccountToken"] is False
    assert (
        pod["securityContext"]["runAsNonRoot"] is True
        and pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    )
    (c,) = pod["containers"]
    assert c["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    assert c["image"] == "registry.example.internal/local-llm-gateway:0.6.0"
    for probe in ("startupProbe", "livenessProbe", "readinessProbe"):
        assert c[probe]["httpGet"] == {"path": "/health", "port": "http"}
    assert {m["mountPath"] for m in c["volumeMounts"]} == {"/data", "/tmp"}
    assert pod["volumes"][0]["persistentVolumeClaim"]["claimName"] == f"{FULL}-data"
    secret_env = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in c["env"]}
    assert set(secret_env) == {"GATEWAY_ADMIN_BOOTSTRAP_KEY", "GATEWAY_METRICS_TOKEN", "OPENAI_COMPAT_API_KEY"}
    assert all(ref["optional"] and ref["name"] == f"{FULL}-secrets" for ref in secret_env.values())
    svc = docs[f"Service/{FULL}"]
    labels = dep["spec"]["template"]["metadata"]["labels"]
    assert svc["spec"]["selector"].items() <= labels.items() and dep["spec"]["selector"]["matchLabels"] == labels


def test_configmap_only_sets_settings_the_gateway_knows(renderer, monkeypatch):
    docs, _ = renderer({"gateway": {"modelLock": "{}", "extraEnv": {"GATEWAY_RRF_K": "60"}}})
    data = docs[f"ConfigMap/{FULL}"]["data"]
    assert set(data) <= set(Settings.model_fields), set(data) - set(Settings.model_fields)
    assert (
        data["GATEWAY_DB_PATH"] == "/data/gateway.db"
        and data["GATEWAY_MODEL_LOCK_FILE"] == "/etc/lldk/lock/models.lock.json"
    )
    parsed = settings_from_env(data, monkeypatch)
    assert parsed.GATEWAY_COLLECTION_DEFAULT_ACCESS == "restricted" and parsed.GATEWAY_RATE_LIMIT_PER_MIN == 60


def test_vllm_values_add_a_gpu_deployment_reachable_only_from_the_gateway(renderer):
    docs, err = renderer(yaml.safe_load((CHART / "values-vllm-gpu.yaml").read_text()))
    assert err == ""
    vllm = docs[f"Deployment/{FULL}-vllm"]["spec"]["template"]["spec"]
    (c,) = vllm["containers"]
    assert c["resources"]["limits"]["nvidia.com/gpu"] == 1 and c["resources"]["requests"]["nvidia.com/gpu"] == 1
    assert c["image"] == "vllm/vllm-openai:v0.6.6" and c["args"][:2] == ["--model", "Qwen/Qwen2.5-7B-Instruct"]
    assert {v["name"]: v for v in vllm["volumes"]}["shm"]["emptyDir"]["medium"] == "Memory"
    assert (
        vllm["nodeSelector"] == {"nvidia.com/gpu.present": "true"} and vllm["tolerations"][0]["key"] == "nvidia.com/gpu"
    )
    assert f"PersistentVolumeClaim/{FULL}-vllm-cache" in docs and f"Service/{FULL}-vllm" in docs
    assert docs[f"ConfigMap/{FULL}"]["data"]["OPENAI_COMPAT_BASE_URL"] == f"http://{FULL}-vllm:8000/v1"
    vnp = docs[f"NetworkPolicy/{FULL}-vllm"]["spec"]
    assert vnp["ingress"][0]["from"][0]["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "gateway"
    gnp = docs[f"NetworkPolicy/{FULL}"]["spec"]
    assert any(
        peer.get("podSelector", {}).get("matchLabels", {}).get("app.kubernetes.io/component") == "vllm"
        for rule in gnp["egress"]
        for peer in rule.get("to", [])
    )


def test_airgapped_values_go_offline_mount_the_keyring_and_configure_oidc(renderer, monkeypatch):
    docs, err = renderer(yaml.safe_load((CHART / "values-airgapped.yaml").read_text()))
    assert err == ""
    assert f"Secret/{FULL}-secrets" not in docs  # existingSecret is used instead
    env = {
        e["name"]: e.get("value")
        for e in docs[f"Deployment/{FULL}-vllm"]["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["HF_HUB_OFFLINE"] == "1" and env["TRANSFORMERS_OFFLINE"] == "1"
    vnp = docs[f"NetworkPolicy/{FULL}-vllm"]["spec"]
    assert all(p["port"] != 443 for rule in vnp["egress"] for p in rule["ports"])  # no internet for vLLM
    gw = docs[f"Deployment/{FULL}"]["spec"]["template"]["spec"]
    mounts = {m["name"]: m for m in gw["containers"][0]["volumeMounts"]}
    assert mounts["keyring"]["readOnly"] is True and mounts["keyring"]["mountPath"] == "/etc/lldk/keyring"
    assert {v["name"]: v for v in gw["volumes"]}["keyring"]["secret"] == {
        "secretName": "lldk-keyring",
        "defaultMode": 256,
    }
    data = docs[f"ConfigMap/{FULL}"]["data"]
    assert (
        data["GATEWAY_ENCRYPTION_KEY_FILE"] == "/etc/lldk/keyring/keyring.json"
        and data["GATEWAY_ENCRYPT_AT_REST"] == "true"
    )
    assert json.loads(data["GATEWAY_OIDC_GROUP_ROLES"]) == {"llm-admins": ["admin"], "staff": ["user"]}
    s = settings_from_env(data, monkeypatch)
    assert s.GATEWAY_OIDC_ENABLED is True and s.GATEWAY_OIDC_GROUP_ROLES == {"llm-admins": ["admin"], "staff": ["user"]}
    gnp = docs[f"NetworkPolicy/{FULL}"]["spec"]
    assert {"to": [{"ipBlock": {"cidr": "10.20.0.15/32"}}], "ports": [{"port": 443, "protocol": "TCP"}]} in gnp[
        "egress"
    ]


def test_service_monitor_and_scrape_access(renderer):
    docs, _ = renderer(
        {
            "serviceMonitor": {"enabled": True, "labels": {"release": "kube-prometheus"}},
            "secrets": {"metricsToken": "t"},
        }
    )
    sm = docs[f"ServiceMonitor/{FULL}"]
    assert sm["metadata"]["labels"]["release"] == "kube-prometheus"
    (ep,) = sm["spec"]["endpoints"]
    assert ep["path"] == "/metrics" and ep["authorization"]["credentials"]["key"] == "metricsToken"
    peers = docs[f"NetworkPolicy/{FULL}"]["spec"]["ingress"][0]["from"]
    assert {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "monitoring"}}} in peers


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"gateway": {"encryption": {"enabled": True}}}, "keyringSecret"),
        ({"gateway": {"oidc": {"enabled": True, "audience": "x"}}}, "gateway.oidc.issuer is required"),
    ],
)
def test_incomplete_security_settings_fail_the_render(renderer, override, message):
    docs, err = renderer(override)
    assert docs == {} and message in err


def test_dockerfile_runs_unprivileged_with_state_under_data():
    text = (ROOT / "Dockerfile").read_text()
    assert "USER 10001" in text and "GATEWAY_DB_PATH=/data/gateway.db" in text
    for path in ("gateway", "admin-ui", "scripts"):
        assert f"COPY {path} /app/{path}" in text
    assert "FROM python:3.12-slim" in text  # pinned major/minor, not :latest
