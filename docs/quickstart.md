# Quickstart

The one-liner installers in the README set everything up. This doc covers the parts people usually ask about afterwards.

## Try it with an OpenAI client

Nothing needs to change on the client side — this gateway is OpenAI-compatible.

**Python:**
```python
from openai import OpenAI

client = OpenAI(api_key="sk-local-xxxx", base_url="http://localhost:8080/v1")
r = client.chat.completions.create(
    model="llama3.1:8b",
    messages=[{"role": "user", "content": "Three bullet-point summary of serverless architecture."}],
)
print(r.choices[0].message.content)
```

**curl:**
```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-local-xxxx" \
  -H "Content-Type: application/json" \
  -d '{"model": "llama3.1:8b", "messages": [{"role": "user", "content": "hi"}]}'
```

## Issue a key for someone else

1. Open `/admin` and paste your admin key
2. In the API keys card, enter a label ("mobile app"), click Create
3. Copy the new `sk-local-…` key and send it to them
4. They use it as a drop-in OpenAI key

## Pull a different model

Ollama's library: https://ollama.com/library. From the admin page, type the name (e.g., `qwen2.5:14b`) and click Pull. The download runs in the background; the model shows up in the list when it finishes, and both events are recorded in the audit log. Then set `GATEWAY_DEFAULT_MODEL` in `.env` or pass `"model"` in each request. Pulls work with the default Ollama backend; with `BACKEND=openai_compatible` (vLLM and similar) the inference server is started with its model and `/v1/pull` returns 501.

## Rate limits

Default is 60 req/min per key. Change with `GATEWAY_RATE_LIMIT_PER_MIN`.

## Prompt logging for audit

Set `GATEWAY_LOG_PROMPTS=true` (and `GATEWAY_REDACT_PROMPTS=true` to scrub PII). Prompts and responses are added to the hash-chained `./logs/audit.jsonl` as `completion` events. The admin page shows whether the chain is intact. Watch the file size on a busy deployment and rotate it as described in `compliance.md`.
