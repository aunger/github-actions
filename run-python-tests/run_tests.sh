#!/usr/bin/env bash
# run_tests.sh -- run a repository's shell and Python unit tests.
#
# Shell tests are every file named test_*.sh under the search paths, each run
# with bash.
# Python tests are every directory under them that holds a test_*.py file, each
# discovered with `python3 -m unittest discover` on its own.
#
# Environment:
#   TEST_PATHS  directories to search, one per line (default: .)
#   TEST_KIND   shell, python or all (default: all)
#   PYTHON      the interpreter that runs the Python tests (default: python3)
#   TEST_GITHUB_TOKEN
#               when set and not empty, exported to the tests as GITHUB_TOKEN,
#               for tests that call the GitHub API; otherwise GITHUB_TOKEN is
#               left as the caller's environment has it
#
# A single recursive `unittest discover` cannot stand in for the per-directory
# loop.
# Discover does not descend into a directory with no __init__.py (namespace
# packages are not auto-discovered), and adding __init__.py so that it would
# breaks the bare intra-directory imports many scripts rely on: a test that does
# `import helper` resolves it only because its own directory is on sys.path.
# Discovering each directory that holds test_*.py files separately keeps every
# directory's own sys.path behavior exactly as it is when a person runs the
# tests there.
#
# Python 3.12 and later exit 5 for "no tests were collected" instead of 0.
# That is tolerated; any other nonzero code is a real failure.
#
# Directories whose names start with a dot (.git, .venv) are not searched: they
# hold other projects' files, and the default search path is the whole
# repository.
#
# The first failure ends the run.
# A search path that is not a directory is an error, so that a misspelled path
# cannot report success having run nothing.
#
# Exit codes:
#   0  every test that ran passed, or none was found
#   2  wrong usage: an unknown TEST_KIND, or a search path that is not a directory
#   any other nonzero code is the exit code of the test that failed

set -euo pipefail

KIND="${TEST_KIND:-all}"
PY="${PYTHON:-python3}"
PATHS="${TEST_PATHS:-.}"
if [[ -n "${TEST_GITHUB_TOKEN:-}" ]]; then
    export GITHUB_TOKEN="$TEST_GITHUB_TOKEN"
fi
unset TEST_GITHUB_TOKEN

case "$KIND" in
    shell | python | all) ;;
    *)
        echo "[run-python-tests] ERROR: kind must be shell, python or all, not '$KIND'" >&2
        exit 2
        ;;
esac

roots=()
while IFS= read -r root; do
    [[ -n "${root//[[:space:]]/}" ]] || continue
    if [[ ! -d "$root" ]]; then
        echo "[run-python-tests] ERROR: not a directory: $root" >&2
        exit 2
    fi
    roots+=("$root")
done <<< "$PATHS"
if [[ ${#roots[@]} -eq 0 ]]; then
    roots=(.)
fi

# Print every file under the roots whose name matches $1, or with $2 = dirs,
# the directory of each, skipping dot-directories.
find_tests() {
    local pattern="$1" format="$2" root
    for root in "${roots[@]}"; do
        find "$root" -mindepth 1 \( -type d -name '.?*' -prune \) -o \
            -type f -name "$pattern" -printf "$format"
    done
}

ran=0

if [[ "$KIND" == "shell" || "$KIND" == "all" ]]; then
    while IFS= read -r test_script; do
        echo "==> Running $test_script"
        bash "$test_script"
        ran=$((ran + 1))
    done < <(find_tests 'test_*.sh' '%p\n' | sort -u)
fi

if [[ "$KIND" == "python" || "$KIND" == "all" ]]; then
    while IFS= read -r dir; do
        echo "==> Discovering tests in $dir"
        "$PY" -m unittest discover -s "$dir" -p "test_*.py" -v && rc=0 || rc=$?
        if [[ $rc -ne 0 && $rc -ne 5 ]]; then
            exit "$rc"
        fi
        ran=$((ran + 1))
    done < <(find_tests 'test_*.py' '%h\n' | sort -u)
fi

if [[ $ran -eq 0 ]]; then
    echo "[run-python-tests] no tests found (kind: $KIND) under: ${roots[*]}"
fi
