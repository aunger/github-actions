# install-pinned-requirements

Installs a hash-pinned pip lock with `--require-hashes` and `--force-reinstall`, into a new virtual environment or into the Python on `PATH`.

```yaml
- uses: actions/checkout@<sha>
- id: deps
  uses: aunger/vcs-automation/install-pinned-requirements@<40-character-sha>
  with:
    lock-file: requirements.txt
    venv: ${{ runner.temp }}/venv
- run: '"$PYTHON" scripts/report.py'
  env:
    PYTHON: ${{ steps.deps.outputs.python }}
```

## Inputs and outputs

| Name | Kind | Default | Meaning |
| --- | --- | --- | --- |
| `lock-file` | input | required | Path to the lock, relative to the workspace. |
| `venv` | input | empty | Directory for a new virtual environment to install into. Empty installs into the Python on `PATH`. |
| `python` | input | `python3` | The interpreter that installs, or that creates the virtual environment. |
| `python` | output | | The interpreter that now has the packages: `<venv>/bin/python` when `venv` is set, otherwise the `python` input. |

`install.sh` is the action's whole body, and runs the same way outside Actions: `install.sh [--venv DIR] LOCK`, with the interpreter in `PYTHON`.
It exits 1 when the lock does not exist and 2 on wrong usage, and otherwise with pip's own status.
The other actions in this repository install their dependencies through it, each into a virtual environment under `$RUNNER_TEMP`.

## The lock

The lock must pin every package, top-level and transitive, to an exact version and at least one SHA-256 hash, since `--require-hashes` refuses an unhashed requirement.
`uv` writes one from a list of top-level pins:

```sh
uv pip compile --universal --python-version 3.11 --generate-hashes requirements.in -o requirements.txt
```

`--universal` makes one lock that covers every interpreter and platform it is installed on, with markers on the entries that apply to only some.
`--python-version` sets the oldest interpreter it covers.

## What the install does, and why

```sh
"$PYTHON" -m pip install --disable-pip-version-check --force-reinstall --require-hashes --quiet -r "$LOCK"
```

- `--require-hashes` makes pip refuse any artifact whose hash is not in the lock, so a substituted or compromised wheel, or an intercepted download, cannot be installed.
- `--force-reinstall` installs what the lock names even when something already satisfies the requirement.
  Without it, a package the runner image ships at the pinned version stays in place, and the job runs bytes the repository never declared.
- `-m pip` runs the pip that belongs to the chosen interpreter, whichever `pip` is first on `PATH`.
- A virtual environment keeps the packages away from the system interpreter, which on some systems refuses `pip install` outright (PEP 668, "externally managed").

## An idempotent installer for other places

The action keeps no record of what it installed, because a runner starts clean every time.
A developer machine, a container's startup hook, or a script run more than once in the same environment wants the opposite: a second run against an unchanged lock should do nothing, and any change to the lock should reinstall.
These rules make that work:

1. **Key the skip on the lock's content.**
   Hash the lock file (SHA-256 of its bytes) and compare it with a hash recorded by the last successful install.
   Equal means skip; anything else, including no record, means install.
   A changed version, an added package or a regenerated hash all change the file, so all of them reinstall.
2. **Record only after success.**
   Write the hash after pip exits 0, never before, so a failed or interrupted install is retried rather than remembered as done.
3. **Keep the record with the packages.**
   Put the marker inside the environment it describes, such as a file in the virtual environment's directory, so that deleting or rebuilding the environment also deletes the marker.
   A marker stored somewhere that outlives the packages (or that the packages outlive) causes a skip over a missing install, or a reinstall that was not needed.
   For a `--user` install, the matching place is under the same `$HOME` as the user site.
4. **One marker per lock.**
   When one environment takes more than one lock, name each marker after the lock's path, not only its file name, so `scripts/requirements.txt` and `tools/requirements.txt` cannot share one.
5. **Same install line.**
   Keep `--require-hashes` and `--force-reinstall`: the skip saves time, but when the install does run, it must leave exactly what the lock names.

A script that follows them, installing into a virtual environment that it creates when missing:

```bash
#!/usr/bin/env bash
# Usage: ensure-venv.sh VENV_DIR LOCK
# Install LOCK into the virtual environment at VENV_DIR, creating it if needed,
# unless that exact lock is already installed there.
set -euo pipefail

VENV="$1"
LOCK="$2"

if [[ ! -x "$VENV/bin/python" ]]; then
    "${PYTHON:-python3}" -m venv "$VENV"
fi

# One marker per lock path, kept inside the venv so it lives and dies with it.
MARKER="$VENV/.installed-$(realpath "$LOCK" | sha256sum | cut -c1-16)"
LOCK_SHA="$(sha256sum "$LOCK" | cut -d' ' -f1)"

if [[ -f "$MARKER" && "$(cat "$MARKER")" == "$LOCK_SHA" ]]; then
    echo "$LOCK is already installed in $VENV"
    exit 0
fi

"$VENV/bin/python" -m pip install --disable-pip-version-check \
    --force-reinstall --require-hashes --quiet -r "$LOCK"
echo "$LOCK_SHA" > "$MARKER"
echo "installed $LOCK into $VENV"
```
