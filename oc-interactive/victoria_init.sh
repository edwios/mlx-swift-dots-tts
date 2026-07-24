#!/usr/bin/env bash
# Run oc-interactive with a CustomVoice speaker (Victoria-style defaults).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
BIN="$ROOT/.venv/bin/oc-interactive"
if [[ ! -x "$BIN" ]]; then
  echo "oc-interactive not installed; run: cd $ROOT && make install" >&2
  exit 1
fi
echo "This will create a new session with default configuration."
echo "If you just want to start a new conversation, or to change the configuration, use 'talk_eileen.sh'."
echo "Press Enter to continue... or Ctrl+C to cancel."
read -r
exec "$BIN" \
  --init --new --config victoria.conf "$@"
