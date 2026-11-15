#!/usr/bin/env bash
# test_install.sh: unit tests for install.sh.
#
# Each case runs the real script with a stub interpreter first on PATH, so the
# cases check the flags the script passes, how it builds a virtual environment,
# and how it fails, without touching PyPI. They run with no network.
#
# Exits 0 when every case passes, non-zero otherwise.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_SH="$SCRIPT_DIR/install.sh"

PASS=0
FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

TMP="$(mktemp -d "${TMPDIR:-/tmp}/install-pinned-req-test-XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

# A case directory holds a stub python3 that records every invocation, and a lock
# to install. The stub handles `-m venv DIR` by creating a second stub at
# DIR/bin/python that records into the same log, so the log shows which
# interpreter ran each call: the log line starts with the name of the directory
# above the stub's own bin/ ("venv" for one made by `-m venv`). Its `-m pip` exit
# status is $2, which makes the "pip failed" case possible.
make_case() {
    local case_dir="$1" pip_rc="${2:-0}"
    mkdir -p "$case_dir/bin"
    cat > "$case_dir/stub.sh" << STUB
#!/usr/bin/env bash
printf '%s: %s\n' "\$(basename "\$(dirname "\$(dirname "\$0")")")" "\$*" >> "$case_dir/calls.log"
if [[ "\$1" == "-m" && "\$2" == "venv" ]]; then
    mkdir -p "\$3/bin"
    cp "\$0" "\$3/bin/python"
    exit 0
fi
if [[ "\$1" == "-m" && "\$2" == "pip" ]]; then
    exit $pip_rc
fi
exit 0
STUB
    chmod +x "$case_dir/stub.sh"
    cp "$case_dir/stub.sh" "$case_dir/bin/python3"
    printf 'defusedxml==0.7.1 --hash=sha256:%064d\n' 1 > "$case_dir/requirements-fake.txt"
}

run_installer() {
    local case_dir="$1"
    shift
    (cd "$case_dir" && PATH="$case_dir/bin:$PATH" GITHUB_OUTPUT="$case_dir/output" \
        "$INSTALL_SH" "$@" >> "$case_dir/out.log" 2>&1)
}

echo "== (a) installs with --require-hashes and --force-reinstall"
C="$TMP/a"; make_case "$C"
if run_installer "$C" requirements-fake.txt; then
    line="$(grep -- '-m pip install' "$C/calls.log" || true)"
    [[ "$line" == *"--require-hashes"* ]] && pass "passes --require-hashes" || fail "no --require-hashes: $line"
    [[ "$line" == *"--force-reinstall"* ]] && pass "passes --force-reinstall" || fail "no --force-reinstall: $line"
    [[ "$line" == *"-r requirements-fake.txt"* ]] && pass "names the lock" || fail "lock not named: $line"
else
    fail "installer exited non-zero: $(cat "$C/out.log")"
fi

echo "== (b) reports the interpreter it installed into"
if [[ "$(cat "$C/output")" == "python=python3" ]]; then
    pass "writes python=python3 to GITHUB_OUTPUT"
else
    fail "GITHUB_OUTPUT was: $(cat "$C/output")"
fi

echo "== (c) --venv creates the environment and installs into it"
C="$TMP/c"; make_case "$C"
if run_installer "$C" --venv "$C/venv" requirements-fake.txt; then
    grep -q -- "-m venv $C/venv" "$C/calls.log" && pass "creates the venv" || fail "no venv call: $(cat "$C/calls.log")"
    grep -q "^venv: -m pip install" "$C/calls.log" && pass "installs with the venv's interpreter" \
        || fail "pip did not run from the venv: $(cat "$C/calls.log")"
    [[ "$(cat "$C/output")" == "python=$C/venv/bin/python" ]] && pass "outputs the venv's interpreter" \
        || fail "GITHUB_OUTPUT was: $(cat "$C/output")"
else
    fail "installer exited non-zero: $(cat "$C/out.log")"
fi

echo "== (d) PYTHON picks the interpreter"
C="$TMP/d"; make_case "$C"
# A second stub with a log of its own, so a call to the python3 stub cannot
# pass for a call to this one.
printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >> "%s/other.log"\n' "$C" > "$C/bin/other-python"
chmod +x "$C/bin/other-python"
if (cd "$C" && PYTHON=other-python PATH="$C/bin:$PATH" GITHUB_OUTPUT="$C/output" \
    "$INSTALL_SH" requirements-fake.txt > "$C/out.log" 2>&1); then
    grep -q -- "-m pip install" "$C/other.log" 2>/dev/null && pass "ran pip with the interpreter PYTHON names" \
        || fail "other-python did not run pip"
    [[ ! -f "$C/calls.log" ]] && pass "did not run python3" || fail "python3 ran: $(cat "$C/calls.log")"
    [[ "$(cat "$C/output")" == "python=other-python" ]] && pass "outputs the interpreter PYTHON names" \
        || fail "GITHUB_OUTPUT was: $(cat "$C/output")"
else
    fail "installer exited non-zero: $(cat "$C/out.log")"
fi

echo "== (e) a missing lock exits 1 and runs nothing"
C="$TMP/e"; make_case "$C"
run_installer "$C" no-such-lock.txt; rc=$?
[[ $rc -eq 1 ]] && pass "exits 1" || fail "exit code $rc"
[[ ! -f "$C/calls.log" ]] && pass "ran no interpreter" || fail "ran: $(cat "$C/calls.log")"
grep -q "no such lock file" "$C/out.log" && pass "names the problem" || fail "message: $(cat "$C/out.log")"

echo "== (f) wrong usage exits 2"
C="$TMP/f"; make_case "$C"
run_installer "$C"; rc=$?
[[ $rc -eq 2 ]] && pass "no lock: exits 2" || fail "no lock: exit code $rc"
run_installer "$C" --venv; rc=$?
[[ $rc -eq 2 ]] && pass "--venv without a directory: exits 2" || fail "--venv alone: exit code $rc"
run_installer "$C" --bogus requirements-fake.txt; rc=$?
[[ $rc -eq 2 ]] && pass "unknown option: exits 2" || fail "unknown option: exit code $rc"
run_installer "$C" requirements-fake.txt requirements-fake.txt; rc=$?
[[ $rc -eq 2 ]] && pass "two locks: exits 2" || fail "two locks: exit code $rc"

echo "== (g) a failing pip fails the install and writes no output"
C="$TMP/g"; make_case "$C" 1
run_installer "$C" requirements-fake.txt; rc=$?
[[ $rc -ne 0 ]] && pass "exits non-zero" || fail "exited 0 although pip failed"
[[ ! -f "$C/output" ]] && pass "reports no interpreter" || fail "GITHUB_OUTPUT: $(cat "$C/output")"

echo
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
