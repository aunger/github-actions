#!/usr/bin/env bash
# install.sh -- install a hash-pinned pip lock, into a new virtual environment
# or into the Python on PATH.
#
# Usage: install.sh [--venv DIR] LOCK
#
#   LOCK       a fully resolved requirements lock: every package, top-level and
#              transitive, pinned to an exact version and a SHA-256 hash.
#   --venv DIR create a virtual environment at DIR and install into it, rather
#              than into the Python on PATH.
#
# Environment:
#   PYTHON         the interpreter to use, or to create the virtual environment
#                  with (default: python3)
#   GITHUB_OUTPUT  when set, `python=<interpreter>` is appended to it, naming the
#                  interpreter that now has the packages
#
# --require-hashes makes pip refuse to install anything whose artifact does not
# match a hash in the lock, so a compromised or substituted wheel on PyPI (or an
# intercepted download) cannot slip in.
# It also forces every dependency to be hash-pinned, which is why these locks
# list the full transitive closure rather than just the top-level packages.
#
# --force-reinstall installs what the lock names even when something already
# satisfies the requirement.
# That is what makes the lock authoritative: without it, a package the runner
# image happens to ship at the pinned version is left in place and the job runs
# bytes the repository never declared.
#
# This is the installer for CI jobs.
# It keeps no marker of what is installed, since a runner starts clean.

set -euo pipefail

usage() {
    echo "[install-pinned-requirements] usage: $(basename "$0") [--venv DIR] LOCK" >&2
    exit 2
}

VENV=""
LOCK=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --venv)
            [[ $# -ge 2 ]] || usage
            VENV="$2"
            shift 2
            ;;
        -*)
            usage
            ;;
        *)
            [[ -z "$LOCK" ]] || usage
            LOCK="$1"
            shift
            ;;
    esac
done
[[ -n "$LOCK" ]] || usage

if [[ ! -f "$LOCK" ]]; then
    echo "[install-pinned-requirements] ERROR: no such lock file: $LOCK" >&2
    exit 1
fi

PY="${PYTHON:-python3}"
if [[ -n "$VENV" ]]; then
    "$PY" -m venv "$VENV"
    PY="$VENV/bin/python"
fi

echo "[install-pinned-requirements] installing $(basename "$LOCK")..."
"$PY" -m pip install --disable-pip-version-check --force-reinstall --require-hashes --quiet -r "$LOCK"
echo "[install-pinned-requirements] installed $(basename "$LOCK")"

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "python=$PY" >> "$GITHUB_OUTPUT"
fi
