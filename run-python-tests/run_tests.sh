#!/usr/bin/env bash
# run_tests.sh -- run a repository's shell and Python unit tests.
#
# Shell tests are every file named test_*.sh under the search paths, each run
# with bash.
# Python tests are every directory under them that holds a test_*.py file, each
# discovered with `python3 -m unittest discover` on its own, except a package
# directory that another one's discovery already reaches (see below).
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
#   2  wrong usage (an unknown TEST_KIND, or a search path that is not a
#      directory), or a search for the tests that failed
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

# Print every file under the roots whose name matches $1, one per line, sorted,
# skipping dot-directories. -print rather than GNU find's -printf, so BSD find
# works too. A find that fails makes this fail, which a caller cannot miss: the
# callers run it in a command substitution, not a process substitution, whose
# status set -e never sees.
find_tests() {
    local pattern="$1" root
    for root in "${roots[@]}"; do
        find "$root" -mindepth 1 \( -type d -name '.?*' -prune \) -o \
            -type f -name "$pattern" -print || return 1
    done | sort -u
}

list_tests() {
    local listed
    if ! listed=$(find_tests "$1"); then
        echo "[run-python-tests] ERROR: could not list $1 under: ${roots[*]}" >&2
        exit 2
    fi
    printf '%s' "$listed"
}

ran=0

# Each test's stdin is /dev/null: the loops read their list on stdin, and a test
# that read it would consume the rest of the list, which would then never run.

if [[ "$KIND" == "shell" || "$KIND" == "all" ]]; then
    shell_tests=$(list_tests 'test_*.sh')
    while IFS= read -r test_script; do
        [[ -n "$test_script" ]] || continue
        echo "==> Running $test_script"
        bash "$test_script" < /dev/null
        ran=$((ran + 1))
    done <<< "$shell_tests"
fi

# unittest's discovery recurses into subdirectories that are packages (have an
# __init__.py), so a package directory whose parent chain of packages reaches
# another directory in the list is already covered by that directory's run, and
# is skipped. Any other package directory is discovered with its top level (-t)
# at its first ancestor that is not a package, so its tests import as members of
# their package and relative imports resolve.
if [[ "$KIND" == "python" || "$KIND" == "all" ]]; then
    python_dirs=$(list_tests 'test_*.py' | sed 's|/[^/]*$||' | sort -u)
    while IFS= read -r dir; do
        [[ -n "$dir" ]] || continue
        top="$dir"
        covered=no
        while [[ -f "$top/__init__.py" ]]; do
            top=$(dirname "$top")
            if grep -Fqx -- "$top" <<< "$python_dirs"; then
                covered=yes
                break
            fi
        done
        if [[ $covered == yes ]]; then
            echo "==> Skipping $dir: a package discovered from $top"
            continue
        fi
        echo "==> Discovering tests in $dir"
        "$PY" -m unittest discover -s "$dir" -t "$top" -p "test_*.py" -v < /dev/null && rc=0 || rc=$?
        if [[ $rc -ne 0 && $rc -ne 5 ]]; then
            exit "$rc"
        fi
        ran=$((ran + 1))
    done <<< "$python_dirs"
fi

if [[ $ran -eq 0 ]]; then
    echo "[run-python-tests] no tests found (kind: $KIND) under: ${roots[*]}"
fi
