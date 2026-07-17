#!/usr/bin/env bash
# Run oc-interactive with a CustomVoice speaker (Eileen-style defaults).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
BIN="$ROOT/.venv/bin/oc-interactive"
if [[ ! -x "$BIN" ]]; then
  echo "oc-interactive not installed; run: cd $ROOT && make install" >&2
  exit 1
fi
exec "$BIN" \
  --speaker Ryan \
  --instruct "warm, friendly, and conversational" \
  -c ello.conf \
  "$@"
