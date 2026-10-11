#!/usr/bin/env bash
# Loomlight setup. Loomlight is standard library only, so this script installs
# no third-party packages: it checks the interpreter, runs pip against the
# (empty) requirements file so the project-level install path is exercised,
# compiles the module, runs the regression suite and EXITS. It never starts a
# server and never needs the network, sudo or a system package manager.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

if ! command -v "$PY" >/dev/null 2>&1; then
  echo "error: $PY was not found. Install Python 3.9 or newer first (see INSTALL.md)." >&2
  exit 1
fi

"$PY" - <<'CHECK'
import sys

if sys.version_info < (3, 9):
    sys.exit(
        "error: Loomlight needs Python 3.9 or newer, found %s" % sys.version.split()[0]
    )
print("interpreter: python %s" % sys.version.split()[0])
CHECK

if "$PY" -m pip --version >/dev/null 2>&1; then
  # requirements.txt lists no third-party packages; this proves the manifest is
  # installable and stays a no-op on a clean machine.
  "$PY" -m pip install --quiet --disable-pip-version-check -r requirements.txt
  # Optional convenience: put the `loomlight` command on PATH. The tool is fully
  # usable without it via `python3 loomlight.py`, so a locked-down environment
  # that refuses the install only gets a note.
  if "$PY" -m pip install --quiet --disable-pip-version-check --no-build-isolation -e . ; then
    echo "console command: loomlight (also runnable as: $PY loomlight.py)"
  else
    echo "note: could not install the optional 'loomlight' command on PATH."
    echo "      Run the tool as: $PY loomlight.py --help"
  fi
else
  echo "note: pip is not available; nothing to install (no third-party dependencies)."
fi

"$PY" -m compileall -q loomlight.py
"$PY" -c "import loomlight"

echo "tests:"
"$PY" -m unittest discover -s tests -q

echo
echo "Loomlight is ready."
echo "  next:  $PY loomlight.py new --out draft.json --shafts 4 --treadles 2 --structure plain"
echo "  demo:  bash demo.sh"
