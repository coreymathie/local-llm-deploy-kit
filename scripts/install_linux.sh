#!/usr/bin/env bash
# private-llm-platform: Linux installer
#   curl -fsSL https://raw.githubusercontent.com/coreymathie/private-llm-platform/main/scripts/install_linux.sh | bash
# Corey Mathie, 2026
set -euo pipefail

MODEL="${LLDK_MODEL:-llama3.1:8b}"
EMBED_MODEL="${LLDK_EMBED_MODEL:-nomic-embed-text}"
INSTALL_DIR="${LLDK_DIR:-$HOME/.private-llm-platform}"
REPO="${LLDK_REPO:-https://github.com/coreymathie/private-llm-platform.git}"

need() { command -v "$1" >/dev/null 2>&1; }

echo "==> Checking prerequisites"
need git || { echo "git is required (e.g. sudo apt install git)"; exit 1; }
need python3 || { echo "python3 is required (e.g. sudo apt install python3 python3-venv)"; exit 1; }
python3 -c "import venv" 2>/dev/null || { echo "python3-venv is required (e.g. sudo apt install python3-venv)"; exit 1; }

if ! need ollama; then
  echo "==> Installing Ollama (official installer; sets up a systemd service)"
  curl -fsSL https://ollama.com/install.sh | sh
fi

echo "==> Making sure Ollama is running"
if ! curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  if need systemctl && systemctl list-unit-files 2>/dev/null | grep -q '^ollama'; then
    sudo systemctl enable --now ollama
  else
    mkdir -p "$INSTALL_DIR"
    nohup ollama serve >"$INSTALL_DIR/ollama.log" 2>&1 &
  fi
  for _ in $(seq 1 20); do
    curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break
    sleep 1
  done
fi

echo "==> Pulling model: $MODEL"
ollama pull "$MODEL"
echo "==> Pulling embedding model for document Q&A: $EMBED_MODEL"
ollama pull "$EMBED_MODEL"

echo "==> Installing the gateway to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
[ -d "$INSTALL_DIR/repo" ] || git clone --depth 1 "$REPO" "$INSTALL_DIR/repo"
cd "$INSTALL_DIR/repo"
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
[ -f .env ] || { cp .env.example .env; { echo "GATEWAY_DEFAULT_MODEL=$MODEL"; echo "GATEWAY_EMBED_MODEL=$EMBED_MODEL"; } >> .env; }

echo "==> Starting the gateway on http://127.0.0.1:8080"
nohup .venv/bin/uvicorn gateway.main:app --host 127.0.0.1 --port 8080 >"$INSTALL_DIR/gateway.log" 2>&1 &
for _ in $(seq 1 20); do
  curl -fsS http://127.0.0.1:8080/health >/dev/null 2>&1 && break
  sleep 1
done

echo ""
echo "==> Bootstrap admin key (printed once; store it in your password manager):"
grep "Bootstrap admin API key" "$INSTALL_DIR/gateway.log" | tail -1 || echo "   (no new key: an admin key already exists)"
echo "==> Admin page: http://localhost:8080/admin"
echo "==> To run as a service, see docs/enterprise-notes.md (systemd unit)."
