#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${1:-http://127.0.0.1:8766}"
ARTIFACT_DIR="${2:-work/qa-agent-$(date +%Y%m%d-%H%M%S)}"

exec uv run --no-sync python docs/qa/scripts/test-editor-agent.py \
  --base-url "$BASE_URL" \
  --artifact-dir "$ARTIFACT_DIR"
