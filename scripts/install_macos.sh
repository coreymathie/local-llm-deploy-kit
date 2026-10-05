#!/usr/bin/env bash
# private-llm-platform: macOS installer
#   curl -fsSL https://raw.githubusercontent.com/coreymathie/private-llm-platform/main/scripts/install_macos.sh | bash
# Corey Mathie, 2026
set -euo pipefail

MODEL="${LLDK_MODEL:-llama3.1:8b}"
EMBED_MODEL="${LLDK_EMBED_MODEL:-nomic-embed-text}"
INSTALL_DIR="${LLDK_DIR:-$HOME/.private-llm-platform}"
REPO="${LLDK_REPO:-https://github.com/coreymathie/private-llm-platform.git}"

need() { command -v "$1" >/dev/null 2>&1; }

echo "==> Checking prerequisites"
need git || { echo "git is required. Install Xcode Command Line Tools: xcode-select --install"; exit 1; }
need python3 || { echo "python3 is required. Install from python.org or: brew install python@3.12"; exit 1; }

if ! need ollama; then
  if need brew; then
    echo "==> Installing Ollama with Homebrew"
    brew install ollama
  else
    echo "Ollama is not installed and Homebrew isn't available."
    echo "Download the macOS app from https://ollama.com/download, open it once, then re-run this script."
    exit 1
  fi
fi

echo "==> Starting Ollama (if it isn't already running)"
if ! curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  if need brew && brew services list 2>/dev/null | grep -q '^ollama'; then
    brew services start ollama
  else
    mkdir -p "$INSTALL_DIR"
    nohup ollama serve >"$INSTALL_DIR/ollama.log" 2>&1 &
  fi
  for _ in $(seq 1 20); do
    curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break
    sleep 1
  done
fi

echo "==> Pulling model: $MODEL (first download can take several minutes)"
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
open http://localhost:8080/admin 2>/dev/null || true
