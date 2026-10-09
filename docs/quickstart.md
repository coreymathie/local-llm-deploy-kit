# Quickstart

This document covers every installation path (one-line installers, a manual development setup, Docker
Compose, a GPU inference server, Kubernetes) and the first tasks after installation: calling the API with an
OpenAI client, issuing keys, pulling models, and turning on prompt logging.

To evaluate the console without installing a model, run `docker compose up` and open
`http://localhost:8080/console/` (simulated backend; the admin key is in `docker compose logs gateway`). See
[console.md](console.md).

## One-line installers

**macOS**
```bash
curl -fsSL https://raw.githubusercontent.com/coreymathie/private-llm-platform/main/scripts/install_macos.sh | bash
```

**Linux**
```bash
curl -fsSL https://raw.githubusercontent.com/coreymathie/private-llm-platform/main/scripts/install_linux.sh | bash
```

**Windows** (elevated PowerShell; see [windows-notes.md](windows-notes.md))
```powershell
iwr -useb https://raw.githubusercontent.com/coreymathie/private-llm-platform/main/scripts/install_windows.ps1 | iex
```

Each installer checks prerequisites, installs Ollama if it is missing, pulls `llama3.1:8b` and
`nomic-embed-text`, starts the gateway on `http://127.0.0.1:8080`, prints a one-time admin key, and opens the
admin page.

## Manual / development setup

```bash
git clone https://github.com/coreymathie/private-llm-platform.git
cd private-llm-platform
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                       # or: cp profiles/healthcare.env .env
uvicorn gateway.main:app --host 127.0.0.1 --port 8080
```

## Docker Compose

`docker compose up -d` starts the gateway and console on `http://localhost:8080/console/` with a simulated
backend (no model). For real answers from Ollama:
`docker compose --env-file profiles/compose-ollama.env --profile ollama up -d`. Ports bind to localhost.

## GPU inference server (vLLM and other OpenAI-compatible servers)

vLLM on an NVIDIA GPU runs as an opt-in compose profile:

```bash
OPENAI_COMPAT_BASE_URL=http://vllm:8000/v1 VLLM_MODEL=Qwen/Qwen2.5-7B-Instruct \
  GATEWAY_DEFAULT_MODEL=Qwen/Qwen2.5-7B-Instruct docker compose --profile vllm up -d
```

An existing OpenAI-compatible server is configured with
`BACKEND=openai_compatible OPENAI_COMPAT_BASE_URL=http://gpu-host:8000/v1 OPENAI_COMPAT_API_KEY=...`.
Document Q&A also needs an embedding model at that URL; see
[ADR 0001](adr/0001-inference-backend.md).

## Kubernetes

The Helm chart in [`deploy/helm/local-llm-gateway`](../deploy/helm/local-llm-gateway) deploys the gateway,
optional vLLM on NVIDIA GPUs, a PVC, Secrets, probes, a NetworkPolicy and a ServiceMonitor;
`scripts/airgap_bundle.sh` packages offline installs. See [kubernetes.md](kubernetes.md), including what was and
was not validated (`helm lint` could not be run in the build environment; CI runs it).

## Calling the API with an OpenAI client

No client-side change is needed beyond the base URL and key: the gateway is OpenAI-compatible.

**Python:**
```python
from openai import OpenAI

client = OpenAI(api_key="sk-local-...", base_url="http://localhost:8080/v1")
r = client.chat.completions.create(
    model="llama3.1:8b",
    messages=[{"role": "user", "content": "Summarize this intake note in three bullets: ..."}],
)
print(r.choices[0].message.content)
```

Streaming works the same way; `stream_options={"include_usage": True}` adds a final usage chunk.

**curl:**
```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-local-xxxx" \
  -H "Content-Type: application/json" \
  -d '{"model": "llama3.1:8b", "messages": [{"role": "user", "content": "hi"}]}'
```

Document Q&A (ingestion, cited answers, access lists) is described in [document-qa.md](document-qa.md).

## Issuing a key to an application

1. Open `/admin` and paste the admin key.
2. In the API keys card, enter a label ("mobile app") and click Create.
3. Copy the new `sk-local-…` key and hand it to the application owner.
4. The application uses it as a drop-in OpenAI key.

## Pulling a different model

Ollama's library: https://ollama.com/library. On the admin page, type the name (e.g. `qwen2.5:14b`) and click
Pull. The download runs in the background; the model appears in the list when it finishes, and both events are
recorded in the audit log. Then set `GATEWAY_DEFAULT_MODEL` in `.env` or pass `"model"` in each request. Pulls
work with the default Ollama backend; with `BACKEND=openai_compatible` (vLLM and similar) the inference server
is started with its model and `/v1/pull` returns 501. Models in regulated deployments are pinned afterwards; see
[operations.md](operations.md#model-supply-chain).

## Rate limits

The default is 60 requests/min per key, set with `GATEWAY_RATE_LIMIT_PER_MIN`.

## Prompt logging for audit

`GATEWAY_LOG_PROMPTS=true` (with `GATEWAY_REDACT_PROMPTS=true` to scrub PII) adds prompts and responses to the
hash-chained `./logs/audit.jsonl` as `completion` events. The admin page shows whether the chain is intact. On a
busy deployment the file grows quickly; rotate it as described in
[compliance.md](compliance.md#worm--immutable-log-retention).
