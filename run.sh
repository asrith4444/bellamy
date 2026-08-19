#!/usr/bin/env bash
# Bellamy launcher — uses the shared browser-use virtualenv
set -e
DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
export PYTHONPATH="$DIR:$PYTHONPATH"
PY="$DIR/.venv/bin/python"
[ -f "$PY" ] || PY="/home/asrith/browser-use-agent/.venv/bin/python"
exec "$PY" "$DIR/main.py" "$@"
