# vcs-automation

Composite GitHub Actions for repository upkeep: pull request and issue hygiene, dependency audits and CI test running.
Each action is a directory with its `action.yml`, its script and its tests.

Consumers call an action by path and a full commit SHA:

```yaml
uses: aunger/vcs-automation/<action>@<40-character-sha>
```

## Actions

| Action | What it does |
| --- | --- |
| `run-python-tests` | Runs a repository's `test_*.sh` and `test_*.py` tests, each Python directory discovered on its own, after an optional hash-pinned install. |
| `install-pinned-requirements` | Installs a hash-pinned pip lock with `--require-hashes` and `--force-reinstall`, into a new virtual environment or the Python on `PATH`. |
| `dependency-audit` | Runs `pip-audit` over every requirements lock, with an ignore list whose stale entries fail the run. |
| `privileged-checkout-guard` | Fails when a pull-request-triggered job with write permissions checks out pull request code. |
| `strip-session-bylines` | Removes Claude session-URL bylines from issues, pull requests, comments and reviews as they are written, or from a whole issue or pull request on request. |
| `branch-cleanup-report` | Sorts branches into Delete, Review and Keep in a job summary, then deletes the ones a reviewer names when approving. |
| `archive-stale-test-failures` | Swaps the `test-failure` label for `test-failure-archive` on issues with no recent activity. |

Each action's `action.yml` documents its inputs, and the permissions and triggers its caller needs.
`install-pinned-requirements` and `dependency-audit` also have a `README.md`: the first says how to write an idempotent installer for places other than CI, and the second documents the ignore file.

## Layout

- `<action>/action.yml`, the script it runs, and that script's tests.
- `shared/` holds the modules more than one script imports: `github_api.py`, `github_headers.py`, `https_only.py` and `session_bylines.py`.
  An action reaches them by relative path, which works because a `uses:` fetches this whole repository at the pinned commit.
- `requirements.in` and `requirements.txt` are the Python dependencies of the scripts, as a hash-pinned lock.
  `dependency-audit/requirements-audit.in` and `.txt` are the lock for `pip-audit` alone.
- `test_action_files.py` checks every `action.yml` for the mistakes the actions' own tests cannot see.

An action that needs a third-party package installs the lock into a virtual environment first, with `install-pinned-requirements`, so no job depends on what its runner image ships.
The scripts of the other actions use only the standard library.

## Tests

Run from the repository root:

```sh
run-python-tests/run_tests.sh
```

`.github/workflows/test.yml` does the same through the `run-python-tests` action, and runs `dependency-audit` and `privileged-checkout-guard` over this repository.
The tests need Python 3.11 or later and `bash`.

`.github/workflows/strip-session-bylines.yml` runs `strip-session-bylines` on this repository's own issues, pull requests, comments and reviews, with `issues` and `pull-requests` write access.
Run by hand (`workflow_dispatch`) with a number, it cleans that whole issue or pull request.

## Copies in other repositories

Three modules in `shared/` have intentional copies in [aunger/dot-and-tilde](https://github.com/aunger/dot-and-tilde)'s `github-supplement` plugin, which imports nothing from here.
`session_bylines.py`'s byline pattern must match the one in `verify_github_write.py`, so a change to either is made in both in the same pass.
`shared/test_plugin_copies.py` compares them at a pinned dot-and-tilde commit, so a change lands in dot-and-tilde first, then here with a bump of that pin.
`github_headers.py` and `https_only.py` have copies in `link_gh_issues.py` and `verify_github_write.py` too.
