#!/usr/bin/env bash
# Bellamy launcher — uses the shared browser-use virtualenv
set -e
DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
export PYTHONPATH="$DIR:$PYTHONPATH"
PY="$DIR/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "Bellamy virtual environment not found at $DIR/.venv" >&2
  echo "Run: uv venv $DIR/.venv && uv pip install --python $PY -r $DIR/requirements.txt" >&2
  exit 1
fi
exec "$PY" "$DIR/main.py" "$@"
