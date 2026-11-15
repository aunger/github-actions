#!/usr/bin/env bash
# test_run_tests.sh: unit tests for run_tests.sh.
#
# Each case builds a small tree of test files that record their own name into a
# log when they run, runs the real script over it, and checks which ran, in what
# way, and what the script returned.
#
# Exits 0 when every case passes, non-zero otherwise.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_TESTS="$SCRIPT_DIR/run_tests.sh"

PASS=0
FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

TMP="$(mktemp -d "${TMPDIR:-/tmp}/run-tests-test-XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

# Write a shell test at $1 that appends its path to $LOG and exits with $2.
shell_test() {
    mkdir -p "$(dirname "$1")"
    printf '#!/usr/bin/env bash\necho "shell:%s" >> "$LOG"\nexit %s\n' "$1" "${2:-0}" > "$1"
}

# Write a Python test at $1 with one test that appends the path to $LOG, then
# passes or, when $2 is "fail", fails.
python_test() {
    mkdir -p "$(dirname "$1")"
    local body="pass"
    [[ "${2:-}" == "fail" ]] && body="self.fail('on purpose')"
    cat > "$1" << PYTEST
import os
import unittest


class T(unittest.TestCase):
    def test_it(self):
        with open(os.environ["LOG"], "a") as log:
            log.write("python:$1\n")
        $body
PYTEST
}

# Run the script from $1 with the rest as environment assignments; output goes
# to $1/../out.log and the log of tests that ran to $1/../ran.log.
run_in() {
    local dir="$1"
    shift
    : > "$dir/../ran.log"
    (cd "$dir" && env LOG="$dir/../ran.log" "$@" "$RUN_TESTS" > "$dir/../out.log" 2>&1)
}

ran() { sort "$1/../ran.log" | tr '\n' ' '; }

echo "== (a) runs shell and Python tests, and passes"
C="$TMP/a/repo"; mkdir -p "$C"
shell_test "$C/scripts/test_one.sh"
python_test "$C/scripts/test_two.py"
run_in "$C"; rc=$?
[[ $rc -eq 0 ]] && pass "exits 0" || fail "exit code $rc: $(cat "$C/../out.log")"
grep -q "^shell:.*/scripts/test_one.sh$" "$C/../ran.log" && pass "ran the shell test" || fail "ran: $(ran "$C")"
grep -q "python:" "$C/../ran.log" && pass "ran the Python test" || fail "ran: $(ran "$C")"

echo "== (b) a failing shell test fails the run"
C="$TMP/b/repo"; mkdir -p "$C"
shell_test "$C/test_bad.sh" 3
run_in "$C"; rc=$?
[[ $rc -eq 3 ]] && pass "exits with the test's code" || fail "exit code $rc"

echo "== (c) a failing Python test fails the run"
C="$TMP/c/repo"; mkdir -p "$C"
python_test "$C/test_bad.py" fail
run_in "$C"; rc=$?
[[ $rc -ne 0 ]] && pass "exits non-zero" || fail "exited 0 although a test failed"

echo "== (d) a Python test file that collects no tests is tolerated"
C="$TMP/d/repo"; mkdir -p "$C"
printf 'import unittest\n' > "$C/test_empty.py"
run_in "$C"; rc=$?
[[ $rc -eq 0 ]] && pass "exits 0" || fail "exit code $rc: $(cat "$C/../out.log")"

echo "== (e) each directory is discovered on its own, so bare imports resolve"
C="$TMP/e/repo"; mkdir -p "$C/a" "$C/b"
printf 'VALUE = 1\n' > "$C/a/helper.py"
cat > "$C/a/test_uses_helper.py" << 'PYTEST'
import os
import unittest

import helper


class T(unittest.TestCase):
    def test_it(self):
        self.assertEqual(helper.VALUE, 1)
        with open(os.environ["LOG"], "a") as log:
            log.write("python:a\n")
PYTEST
python_test "$C/b/test_other.py"
run_in "$C"; rc=$?
[[ $rc -eq 0 ]] && pass "exits 0" || fail "exit code $rc: $(cat "$C/../out.log")"
grep -q "^python:a$" "$C/../ran.log" && pass "ran the test that imports its sibling" || fail "ran: $(ran "$C")"
[[ "$(grep -c "Discovering tests in" "$C/../out.log")" -eq 2 ]] && pass "discovered two directories" \
    || fail "output: $(cat "$C/../out.log")"

echo "== (f) TEST_KIND narrows the run"
C="$TMP/f/repo"; mkdir -p "$C"
shell_test "$C/test_bad.sh" 1
python_test "$C/test_ok.py"
run_in "$C" TEST_KIND=python; rc=$?
[[ $rc -eq 0 ]] && pass "python only: the failing shell test is not run" || fail "exit code $rc"
run_in "$C" TEST_KIND=shell; rc=$?
[[ $rc -eq 1 ]] && pass "shell only: the failing shell test runs" || fail "exit code $rc"
grep -q "python:" "$C/../ran.log" && fail "shell only ran a Python test" || pass "shell only: no Python test ran"

echo "== (g) an unknown TEST_KIND is a usage error"
C="$TMP/g/repo"; mkdir -p "$C"
shell_test "$C/test_ok.sh"
run_in "$C" TEST_KIND=bogus; rc=$?
[[ $rc -eq 2 ]] && pass "exits 2" || fail "exit code $rc"
[[ ! -s "$C/../ran.log" ]] && pass "ran nothing" || fail "ran: $(ran "$C")"

echo "== (h) TEST_PATHS lists the directories to search, one per line"
C="$TMP/h/repo"; mkdir -p "$C"
shell_test "$C/one/test_a.sh"
shell_test "$C/two words/test_b.sh"
shell_test "$C/three/test_c.sh"
run_in "$C" "TEST_PATHS=one
two words"; rc=$?
[[ $rc -eq 0 ]] && pass "exits 0" || fail "exit code $rc: $(cat "$C/../out.log")"
grep -q "test_a.sh" "$C/../ran.log" && grep -q "test_b.sh" "$C/../ran.log" && pass "ran both named directories" \
    || fail "ran: $(ran "$C")"
grep -q "test_c.sh" "$C/../ran.log" && fail "ran a directory that was not named" || pass "left the other directory alone"

echo "== (i) a path that is not a directory is a usage error"
C="$TMP/i/repo"; mkdir -p "$C"
shell_test "$C/test_ok.sh"
run_in "$C" TEST_PATHS=nope; rc=$?
[[ $rc -eq 2 ]] && pass "exits 2" || fail "exit code $rc"
grep -q "not a directory: nope" "$C/../out.log" && pass "names the path" || fail "output: $(cat "$C/../out.log")"

echo "== (j) dot-directories are not searched"
C="$TMP/j/repo"; mkdir -p "$C"
shell_test "$C/.venv/test_bad.sh" 1
shell_test "$C/.git/hooks/test_bad.sh" 1
python_test "$C/.venv/test_bad.py" fail
shell_test "$C/test_ok.sh"
run_in "$C"; rc=$?
[[ $rc -eq 0 ]] && pass "exits 0" || fail "exit code $rc: $(cat "$C/../out.log")"

echo "== (k) a search path that is itself under a dot-directory is searched"
C="$TMP/k/repo"; mkdir -p "$C"
shell_test "$C/.claude/plugins/test_ok.sh"
run_in "$C" TEST_PATHS=.claude; rc=$?
[[ $rc -eq 0 ]] && pass "exits 0" || fail "exit code $rc"
grep -q "test_ok.sh" "$C/../ran.log" && pass "ran the test" || fail "ran: $(ran "$C")"

echo "== (l) finding no tests is a success that says so"
C="$TMP/l/repo"; mkdir -p "$C/docs"
run_in "$C"; rc=$?
[[ $rc -eq 0 ]] && pass "exits 0" || fail "exit code $rc"
grep -q "no tests found (kind: all)" "$C/../out.log" && pass "says so" || fail "output: $(cat "$C/../out.log")"

echo "== (m) PYTHON picks the interpreter for the Python tests"
C="$TMP/m/repo"; mkdir -p "$C"
python_test "$C/test_ok.py"
cat > "$C/../fake-python" << 'FAKE'
#!/usr/bin/env bash
echo "fake-python:$*" >> "$LOG"
exit 0
FAKE
chmod +x "$C/../fake-python"
run_in "$C" "PYTHON=$C/../fake-python"; rc=$?
[[ $rc -eq 0 ]] && pass "exits 0" || fail "exit code $rc: $(cat "$C/../out.log")"
grep -q "^fake-python:-m unittest discover" "$C/../ran.log" && pass "used the named interpreter" || fail "ran: $(ran "$C")"

echo "== (n) TEST_GITHUB_TOKEN reaches the tests as GITHUB_TOKEN, and only when set"
C="$TMP/n/repo"; mkdir -p "$C"
printf '#!/usr/bin/env bash\necho "token:${GITHUB_TOKEN:-unset} raw:${TEST_GITHUB_TOKEN:-unset}" >> "$LOG"\n' > "$C/test_env.sh"
run_in "$C" TEST_GITHUB_TOKEN=secret-value; rc=$?
[[ $rc -eq 0 ]] && pass "exits 0" || fail "exit code $rc"
grep -qx "token:secret-value raw:unset" "$C/../ran.log" && pass "exported as GITHUB_TOKEN, input variable unset" \
    || fail "ran: $(cat "$C/../ran.log")"
run_in "$C" TEST_GITHUB_TOKEN= GITHUB_TOKEN=from-caller; rc=$?
grep -qx "token:from-caller raw:unset" "$C/../ran.log" && pass "empty input keeps the caller's GITHUB_TOKEN" \
    || fail "ran: $(cat "$C/../ran.log")"

echo
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
