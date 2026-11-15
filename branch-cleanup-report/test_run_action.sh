#!/usr/bin/env bash
# test_run_action.sh: unit tests for run_action.sh.
#
# Each case runs the real script with a stub interpreter that records its
# arguments and prints a line, so the cases check the command line the inputs
# become and where the output goes, without calling GitHub.
#
# Exits 0 when every case passes, non-zero otherwise.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_ACTION="$SCRIPT_DIR/run_action.sh"

PASS=0
FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

TMP="$(mktemp -d "${TMPDIR:-/tmp}/branch-cleanup-action-test-XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

cat > "$TMP/stub" << 'STUB'
#!/usr/bin/env bash
# One argument per line, so a pattern with a space stays one argument.
printf '%s\n' "$@" > "$STUB_ARGS"
echo "# the report"
STUB
chmod +x "$TMP/stub"

# Run the script with the inputs given as VAR=value arguments.
run() {
    local case_dir="$1"
    shift
    mkdir -p "$case_dir"
    : > "$case_dir/summary"
    : > "$case_dir/output"
    env -i PATH="$PATH" PYTHON="$TMP/stub" STUB_ARGS="$case_dir/args" \
        GITHUB_STEP_SUMMARY="$case_dir/summary" GITHUB_OUTPUT="$case_dir/output" \
        "$@" "$RUN_ACTION" > "$case_dir/stdout" 2> "$case_dir/stderr"
}

has_arg() { grep -qxF -- "$2" "$1/args"; }

echo "== (a) report"
C="$TMP/a"
run "$C" MODE=report GRACE_DAYS=14; rc=$?
[[ $rc -eq 0 ]] && pass "exits 0" || fail "exit code $rc: $(cat "$C/stderr")"
[[ "$(sed -n 2p "$C/args")" == "report" ]] && pass "runs the report command" || fail "args: $(cat "$C/args")"
has_arg "$C" "--choices-output" && has_arg "$C" "$C/output" && pass "points --choices-output at GITHUB_OUTPUT" \
    || fail "args: $(cat "$C/args")"
has_arg "$C" "--grace-days" && has_arg "$C" "14" && pass "passes the grace period" || fail "args: $(cat "$C/args")"
[[ "$(cat "$C/summary")" == "# the report" ]] && pass "sends the report to the summary" || fail "summary: $(cat "$C/summary")"
[[ ! -s "$C/stdout" ]] && pass "prints nothing else to stdout" || fail "stdout: $(cat "$C/stdout")"

echo "== (b) delete"
C="$TMP/b"
run "$C" MODE=delete 'CHOICES={"a":1}' ENVIRONMENT=branch-deletion; rc=$?
[[ $rc -eq 0 ]] && pass "exits 0" || fail "exit code $rc: $(cat "$C/stderr")"
[[ "$(sed -n 2p "$C/args")" == "delete" ]] && pass "runs the delete command" || fail "args: $(cat "$C/args")"
has_arg "$C" "--environment" && has_arg "$C" "branch-deletion" && pass "passes the environment" || fail "args: $(cat "$C/args")"
has_arg "$C" "--choices" && has_arg "$C" '{"a":1}' && pass "passes the choices unsplit" || fail "args: $(cat "$C/args")"
has_arg "$C" "--choices-output" && fail "delete passed --choices-output" || pass "does not pass --choices-output"

echo "== (c) each keep line becomes one --keep, trimmed, blank lines dropped"
C="$TMP/c"
# Built with printf so that the whitespace under test is not in this file, where
# an editor could strip it.
run "$C" MODE=report "KEEP=$(printf 'release/*  \n   \n  legacy branch/*\t\n')"; rc=$?
[[ $rc -eq 0 ]] && pass "exits 0" || fail "exit code $rc: $(cat "$C/stderr")"
[[ "$(grep -cx -- '--keep' "$C/args")" -eq 2 ]] && pass "two --keep options" || fail "args: $(cat "$C/args")"
has_arg "$C" "release/*" && pass "trailing spaces dropped" || fail "args: $(cat -A "$C/args")"
has_arg "$C" "legacy branch/*" && pass "leading spaces and a trailing tab dropped, inner space kept" \
    || fail "args: $(cat -A "$C/args")"

echo "== (d) delete without choices or environment is a usage error"
C="$TMP/d"
run "$C" MODE=delete ENVIRONMENT=branch-deletion; rc=$?
[[ $rc -eq 2 ]] && pass "no choices: exits 2" || fail "no choices: exit code $rc"
[[ ! -f "$C/args" ]] && pass "ran nothing" || fail "ran: $(cat "$C/args")"
run "$C" MODE=delete 'CHOICES={"a":1}'; rc=$?
[[ $rc -eq 2 ]] && pass "no environment: exits 2" || fail "no environment: exit code $rc"

echo "== (e) an unknown or missing mode is a usage error"
C="$TMP/e"
run "$C" MODE=bogus; rc=$?
[[ $rc -eq 2 ]] && pass "unknown mode: exits 2" || fail "unknown mode: exit code $rc"
run "$C"; rc=$?
[[ $rc -eq 2 ]] && pass "no mode: exits 2" || fail "no mode: exit code $rc"

echo "== (f) the script's failure is the action's failure"
C="$TMP/f"
printf '#!/usr/bin/env bash\nexit 7\n' > "$TMP/failing"; chmod +x "$TMP/failing"
mkdir -p "$C"
env -i PATH="$PATH" PYTHON="$TMP/failing" MODE=report \
    GITHUB_STEP_SUMMARY="$C/summary" "$RUN_ACTION" > /dev/null 2>&1; rc=$?
[[ $rc -eq 7 ]] && pass "exits with the script's code" || fail "exit code $rc"

echo
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
