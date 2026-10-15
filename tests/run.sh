#!/bin/sh
# Run the full test suite (and shellcheck if installed). Never touches your
# real ~/.cursor: every test uses a temporary HOME.
set -eu
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
PY=${PYTHON:-python3}
if command -v shellcheck >/dev/null 2>&1; then
  shellcheck -s sh "$ROOT/install.sh" "$ROOT/uninstall.sh" "$ROOT/tests/run.sh"
  echo "shellcheck: ok"
else
  echo "shellcheck: not installed, skipped"
fi
"$PY" -m unittest discover -s "$ROOT/tests" -p 'test_*.py' -v
