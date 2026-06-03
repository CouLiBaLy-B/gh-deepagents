#!/usr/bin/env bash
# Installs Ollama and pulls the recommended coding model.
set -euo pipefail

MODEL="${1:-qwen2.5-coder:14b}"

if ! command -v ollama >/dev/null 2>&1; then
  echo "==> Installing Ollama"
  curl -fsSL https://ollama.com/install.sh | sh
fi

echo "==> Ensuring Ollama daemon is running"
if ! pgrep -x ollama >/dev/null; then
  nohup ollama serve >/tmp/ollama.log 2>&1 &
  sleep 3
fi

echo "==> Pulling $MODEL"
ollama pull "$MODEL"

echo "==> Quick smoke test"
ollama run "$MODEL" "print 'ok' in python" || true

echo "Done. Set DEEPAGENT_MODEL=ollama:$MODEL"
