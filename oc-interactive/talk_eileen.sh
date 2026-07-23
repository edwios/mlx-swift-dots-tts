#!/usr/bin/env bash
# Run oc-interactive from the repo without activating the venv or adding to PATH.
#
# Usage:
#   talk_eileen.sh [options] [-t "text input"]
#   talk_eileen.sh text input from user
#
# If none of the arguments look like an option (i.e. none start with "-"),
# everything after the script name is treated as the text input, e.g.
# `talk_eileen.sh text input from user` is equivalent to
# `talk_eileen.sh -t "text input from user"`.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
BIN="$ROOT/.venv/bin/oc-interactive"
if [[ ! -x "$BIN" ]]; then
  echo "oc-interactive not installed; run: cd $ROOT && make install" >&2
  exit 1
fi

has_option=false
for arg in "$@"; do
  if [[ "$arg" == -* ]]; then
    has_option=true
    break
  fi
done

if [[ $# -gt 0 && "$has_option" == false ]]; then
  exec "$BIN" -t "$*"
else
  exec "$BIN" "$@"
fi

