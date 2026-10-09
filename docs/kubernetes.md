# Kubernetes (Helm) and air-gapped installs

This document describes the Helm deployment of the gateway (with optional in-cluster vLLM on NVIDIA GPUs) and
the air-gapped installation bundle. It states what the chart deploys, the security posture it enforces, and
exactly what was and was not validated.

Chart: [`deploy/helm/local-llm-gateway`](../deploy/helm/local-llm-gateway). Image: build the repository's
`Dockerfile` and push it to the institution's registry (no public image is published).

```bash
docker build -t registry.example.internal/local-llm-gateway:0.7.0 . && docker push registry.example.internal/local-llm-gateway:0.7.0
helm install gw deploy/helm/local-llm-gateway -n llm --create-namespace \
  --set backend.type=ollama --set backend.ollamaHost=http://ollama.llm.svc.cluster.local:11434
# or with an in-cluster vLLM on one NVIDIA GPU:
helm install gw deploy/helm/local-llm-gateway -n llm -f deploy/helm/local-llm-gateway/values-vllm-gpu.yaml
```

## What the chart deploys

| Object | Notes |
|---|---|
| Deployment (gateway) | **One replica** (`replicaCount` is capped at 1 by `values.schema.json`): state is SQLite on a ReadWriteOnce volume and rate limits are per process. `Recreate` strategy. Non-root UID 10001, read-only root filesystem, all capabilities dropped, `RuntimeDefault` seccomp, no service-account token. Startup, liveness and readiness probes on `/health` |
| PersistentVolumeClaim | `/data` for `gateway.db` and `logs/audit.jsonl` (`persistence.*`, or `existingClaim`) |
| ConfigMap | Every gateway setting the chart manages, as environment variables (checked against the gateway's `Settings` in tests); optional model lock file |
| Secret | `bootstrapAdminKey`, `metricsToken`, `openaiCompatApiKey`, or use `secrets.existingSecret` (recommended: managed outside Helm). Encryption keyring from its own Secret (`gateway.encryption.keyringSecret`), mounted read-only (mode 0400) |
| NetworkPolicy | Default deny for the gateway: ingress from `networkPolicy.ingressFrom` peers (and the release's `helm test` pod, and the `monitoring` namespace when a ServiceMonitor is on); egress to DNS, the chart's vLLM pods, `backendEgressCidrs`, and `oidcEgressCidrs` on 443 for JWKS. vLLM accepts traffic only from the gateway; with `vllm.offline` it has no internet egress |
| ServiceMonitor (optional) | `serviceMonitor.enabled`; scrapes `/metrics` with the metrics token as a bearer credential |
| vLLM (optional) | `vllm.enabled`: Deployment with `nvidia.com/gpu` requests and limits, GPU toleration, `/dev/shm` as memory, a cache PVC for weights, `HF_HUB_OFFLINE` when `vllm.offline`. Image tag pinned (`v0.6.6`); update deliberately |
| `helm test` Pod | Calls `/health` with the gateway image |

The chart fails closed at render time when a security setting is incomplete: encryption without a keyring
Secret, or OIDC without an issuer or audience.

## Validation status

- **Not run in the build environment:** `helm lint`, `helm template` and `helm install`. Helm could not be
  downloaded there (the release host and the Go module proxy were blocked by the egress policy), there was no
  Docker daemon to build the image, and no cluster. CI runs `helm lint --strict` and `helm template` (vLLM GPU
  and air-gapped values) on every push (`.github/workflows/ci.yml`), and those steps pass; `helm install` on a
  cluster has not been run.
- **Run here** (`tests/test_helm_chart.py`): `values.yaml` and both example values files validate against
  `values.schema.json`, and the schema rejects unsafe values (more than one replica, `latest` tags, HMAC
  algorithms, unknown roles). The templates are rendered by `tests/helm_render/render.go`, a stdlib-only Go
  `text/template` harness implementing the Helm/Sprig functions the chart uses (Helm itself uses
  `text/template`; the harness is stricter about missing keys and is not helm). The rendered manifests are
  parsed and checked: hardened security context, probes, volumes, selectors, GPU resources, NetworkPolicy
  rules, offline vLLM, keyring mount, OIDC settings read back through the gateway's own `Settings`, and the
  render failures above.
- `Dockerfile`: checked statically (non-root user, state under `/data`); not built here.

## Air-gapped install

```bash
bash scripts/airgap_bundle.sh --dry-run --image registry.example.internal/local-llm-gateway:0.7.0 \
  --image vllm/vllm-openai:v0.6.6 --hf-model Qwen/Qwen2.5-7B-Instruct --ollama-model nomic-embed-text:latest \
  --platform manylinux2014_x86_64 --python-version 3.12
bash scripts/airgap_bundle.sh --out /media/transfer/lldk ...same options...
bash scripts/airgap_bundle.sh --verify /media/transfer/lldk      # on the air-gapped side, before using anything
```

The bundle holds wheels (`pip download`, cross-platform with `--platform`/`--python-version`), saved images,
Ollama manifests plus every blob they reference, Hugging Face snapshots, the source and chart, the model lock
file (from `GATEWAY_MODEL_LOCK_FILE`) and an ML-BOM, plus `INSTALL.txt` and `SHA256SUMS`.

Tested here: dry-run planning, a real bundle without network steps (Ollama blobs, source, chart, ML-BOM),
checksum verification and tamper detection (`tests/test_airgap_bundle.py`). Not tested here: the `docker save`,
`pip download` and `huggingface-cli` steps (no daemon or network for them in the test).

Checksums detect corruption and tampering in transit, not a malicious source. Model digests are pinned
(`models.lock.json`) and image digests are verified against the institution's registry for that reason.
