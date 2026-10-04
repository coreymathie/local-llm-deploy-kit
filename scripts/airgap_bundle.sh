#!/usr/bin/env bash
# Corey Mathie, 2026
# Build an offline (air-gapped) install bundle: Python wheels, container images, Ollama model blobs,
# Hugging Face model snapshots, the source, the Helm chart, the model lock file and an ML-BOM, with a
# SHA256SUMS file over everything. Run it on a connected machine, carry the folder across, verify it
# with --verify, then follow INSTALL.txt inside the bundle.
#
#   bash scripts/airgap_bundle.sh --dry-run --image registry.example.internal/local-llm-gateway:0.6.0 \
#       --ollama-model llama3.1:8b --ollama-model nomic-embed-text:latest
#   bash scripts/airgap_bundle.sh --out /media/usb/lldk --platform manylinux2014_x86_64 --python-version 3.12 \
#       --image vllm/vllm-openai:v0.6.6 --hf-model Qwen/Qwen2.5-7B-Instruct
#   bash scripts/airgap_bundle.sh --verify /media/usb/lldk
#
# --dry-run prints every command and file copy without running or writing anything, and reports
# missing tools instead of failing.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="./lldk-airgap-bundle"
DRY_RUN=0
WHEELS=1
PLATFORM=""
PYVER=""
OLLAMA_DIR="${OLLAMA_MODELS:-$HOME/.ollama/models}"
IMAGES=()
OLLAMA_MODELS_LIST=()
HF_MODELS=()

usage() { sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; }

die() { echo "error: $*" >&2; exit 1; }

run() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '+ %s\n' "$*"
  else
    "$@"
  fi
}

need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    if [ "$DRY_RUN" = 1 ]; then
      echo "! missing tool: $1 (needed for: $2)"
    else
      die "missing tool: $1 (needed for: $2)"
    fi
  fi
}

verify() {
  [ -f "$1/SHA256SUMS" ] || die "no SHA256SUMS in $1"
  (cd "$1" && sha256sum --check --quiet SHA256SUMS) || die "checksum mismatch in $1"
  echo "bundle verified: $1"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --no-wheels) WHEELS=0; shift ;;
    --platform) PLATFORM="$2"; shift 2 ;;
    --python-version) PYVER="$2"; shift 2 ;;
    --image) IMAGES+=("$2"); shift 2 ;;
    --ollama-model) OLLAMA_MODELS_LIST+=("$2"); shift 2 ;;
    --ollama-dir) OLLAMA_DIR="$2"; shift 2 ;;
    --hf-model) HF_MODELS+=("$2"); shift 2 ;;
    --verify) verify "$2"; exit 0 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml")"
echo "local-llm-deploy-kit $VERSION -> $OUT$([ "$DRY_RUN" = 1 ] && echo ' (dry run: nothing is written)')"

need sha256sum "checksums"
need python3 "wheels, Ollama manifests, ML-BOM"
[ "${#IMAGES[@]}" -gt 0 ] && need docker "container images"
[ "${#HF_MODELS[@]}" -gt 0 ] && need huggingface-cli "Hugging Face models (pip install huggingface_hub)"
if [ -e "$OUT" ] && [ "$DRY_RUN" = 0 ] && [ -n "$(ls -A "$OUT" 2>/dev/null)" ]; then
  die "$OUT exists and is not empty"
fi
run mkdir -p "$OUT/wheels" "$OUT/images" "$OUT/ollama" "$OUT/hf" "$OUT/source"

# 1. Python wheels (for `pip install --no-index --find-links wheels -r requirements.txt`, or the
#    Dockerfile's OFFLINE=1 build). Cross-platform downloads need --platform and --python-version.
if [ "$WHEELS" = 1 ]; then
  args=(python3 -m pip download -r "$ROOT/requirements.txt" -d "$OUT/wheels")
  if [ -n "$PLATFORM" ] || [ -n "$PYVER" ]; then
    args+=(--only-binary=:all:)
    [ -n "$PLATFORM" ] && args+=(--platform "$PLATFORM")
    [ -n "$PYVER" ] && args+=(--python-version "$PYVER")
  fi
  run "${args[@]}"
fi

# 2. Source, Helm chart, requirements, model lock.
if git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  run git -C "$ROOT" archive --format=tar.gz -o "$OUT/source/local-llm-deploy-kit-$VERSION.tar.gz" HEAD
else
  run tar -czf "$OUT/source/local-llm-deploy-kit-$VERSION.tar.gz" -C "$ROOT" --exclude=.git .
fi
run tar -czf "$OUT/source/local-llm-gateway-chart-$VERSION.tgz" -C "$ROOT/deploy/helm" local-llm-gateway
run cp "$ROOT/requirements.txt" "$OUT/source/requirements.txt"
LOCK="${GATEWAY_MODEL_LOCK_FILE:-}"
if [ -n "$LOCK" ] && [ -f "$LOCK" ]; then
  run cp "$LOCK" "$OUT/models.lock.json"
fi

# 3. Container images (docker load -i on the other side).
for ref in "${IMAGES[@]+"${IMAGES[@]}"}"; do
  file="$OUT/images/$(echo "$ref" | tr '/:@' '___').tar"
  if ! docker image inspect "$ref" >/dev/null 2>&1; then  # e.g. the gateway image you built locally
    run docker pull "$ref"
  fi
  run docker save -o "$file" "$ref"
done

# 4. Ollama models: copy the manifest and every blob it references from the local model store, so the
#    target's ~/.ollama/models (or OLLAMA_MODELS) can be seeded without a registry.
for model in "${OLLAMA_MODELS_LIST[@]+"${OLLAMA_MODELS_LIST[@]}"}"; do
  name="${model%%:*}"; tag="${model#*:}"; [ "$tag" = "$model" ] && tag="latest"
  case "$name" in */*) repo="$name" ;; *) repo="library/$name" ;; esac
  manifest="$OLLAMA_DIR/manifests/registry.ollama.ai/$repo/$tag"
  if [ ! -f "$manifest" ]; then
    if [ "$DRY_RUN" = 1 ]; then
      echo "! $model: no manifest at $manifest (run 'ollama pull $model' first)"
      continue
    fi
    die "$model: no manifest at $manifest (run 'ollama pull $model' first)"
  fi
  run mkdir -p "$OUT/ollama/manifests/registry.ollama.ai/$repo" "$OUT/ollama/blobs"
  run cp "$manifest" "$OUT/ollama/manifests/registry.ollama.ai/$repo/$tag"
  while IFS= read -r digest; do
    blob="$OLLAMA_DIR/blobs/${digest/:/-}"
    [ -f "$blob" ] || die "$model: missing blob $blob"
    run cp "$blob" "$OUT/ollama/blobs/"
  done < <(python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); print("\n".join([m["config"]["digest"]] + [l["digest"] for l in m["layers"]]))' "$manifest")
done

# 5. Hugging Face snapshots for vLLM (mount as the vLLM cache volume; set vllm.offline=true).
for repo in "${HF_MODELS[@]+"${HF_MODELS[@]}"}"; do
  run huggingface-cli download "$repo" --local-dir "$OUT/hf/$repo"
done

# 6. ML-BOM of the pinned models and Python packages.
if [ -n "$LOCK" ] && [ -f "$LOCK" ]; then
  run python3 "$ROOT/scripts/mlbom.py" --lock "$LOCK" --out "$OUT/mlbom.cdx.json"
else
  run python3 "$ROOT/scripts/mlbom.py" --out "$OUT/mlbom.cdx.json"
fi

# 7. Instructions and checksums.
if [ "$DRY_RUN" = 1 ]; then
  echo "+ write $OUT/INSTALL.txt"
  echo "+ (cd $OUT && sha256sum over every file > SHA256SUMS)"
  echo "dry run complete"
  exit 0
fi
cat > "$OUT/INSTALL.txt" <<TXT
local-llm-deploy-kit $VERSION offline bundle

1. Verify:            bash scripts/airgap_bundle.sh --verify <this folder>   (or: sha256sum -c SHA256SUMS)
2. Images:            for f in images/*.tar; do docker load -i "\$f"; done   (then push to your registry)
3. Python (no Docker): pip install --no-index --find-links wheels -r source/requirements.txt
4. Ollama models:     copy ollama/manifests and ollama/blobs into the target's OLLAMA_MODELS folder
5. vLLM models:       copy hf/<org>/<model> to the vLLM cache volume; Helm: vllm.offline=true
6. Kubernetes:        helm install gw source/local-llm-gateway-chart-$VERSION.tgz -f values-airgapped.yaml
7. Model pinning:     models.lock.json (if present) -> GATEWAY_MODEL_LOCK_FILE, GATEWAY_MODEL_POLICY=enforce
TXT
sums="$(mktemp)"
(cd "$OUT" && find . -type f -print0 | sort -z | xargs -0 sha256sum) > "$sums"
mv "$sums" "$OUT/SHA256SUMS"
echo "bundle written: $OUT ($(wc -l < "$OUT/SHA256SUMS") files, SHA256SUMS)"
