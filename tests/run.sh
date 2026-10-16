#!/bin/sh
# Run the full test suite (and shellcheck if installed). Never touches your
# real ~/.cursor: every test uses a temporary HOME.
set -eu
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
PY=${PYTHON:-python3}
if command -v shellcheck >/dev/null 2>&1; then
  shellcheck -s sh "$ROOT/install.sh" "$ROOT/uninstall.sh" "$ROOT/tests/run.sh"
  echo "shellcheck: ok"
elif [ "${SHELLCHECK:-}" = require ]; then
  echo "shellcheck: not installed and SHELLCHECK=require" >&2
  exit 1
else
  echo "shellcheck: not installed, skipped (brew install shellcheck; SHELLCHECK=require to enforce)"
fi
# Always at least a syntax check of every shell script (audit I3).
for f in "$ROOT/install.sh" "$ROOT/uninstall.sh" "$ROOT/tests/run.sh"; do sh -n "$f"; done
echo "sh -n: ok"
"$PY" -m unittest discover -s "$ROOT/tests" -p 'test_*.py' -v
