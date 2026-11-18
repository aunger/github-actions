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
| `label-by-title` | Adds the labels whose regular expression matches an issue's or pull request's title. |
| `label-by-files` | Adds and removes a pull request's labels by the files it changes. |
| `enforce-mutually-exclusive-labels` | Removes the other labels of a mutually exclusive set or prefix group when one is added. |
| `check-blocking-labels` | Fails, holding a merge, while any open pull request at the head commit carries a blocking label. |
| `propagate-issue-labels` | Copies the labels of the issues a pull request closes onto it. |
| `reconcile-issue-labels` | Applies `propagate-issue-labels` to every open pull request, on demand. |
| `snooze-issues` | Closes an issue as not planned when a snooze label is added. |
| `wake-snoozed-issues` | Reopens each snoozed issue once its snooze label's days have passed. |

Each action's `action.yml` documents its inputs, and the permissions and triggers its caller needs.
`install-pinned-requirements` and `dependency-audit` also have a `README.md`: the first says how to write an idempotent installer for places other than CI, and the second documents the ignore file.

## Layout

- `<action>/action.yml`, the script it runs, and that script's tests.
- `shared/` holds the modules more than one script imports: `github_api.py`, `github_headers.py`, `https_only.py`, `session_bylines.py`, `path_globs.py`, and for the label actions `label_common.py` and `workflow_inputs.py`.
  An action reaches them by relative path, which works because a `uses:` fetches this whole repository at the pinned commit.
- `requirements.in` and `requirements.txt` are the Python dependencies of the scripts, as a hash-pinned lock.
  `dependency-audit/requirements-audit.in` and `.txt` are the lock for `pip-audit` alone.
- `test_action_files.py` checks every `action.yml` for the mistakes the actions' own tests cannot see.

An action that needs a third-party package installs the lock into a virtual environment first, with `install-pinned-requirements`, so no job depends on what its runner image ships.
The scripts of the other actions use only the standard library.

## Label actions

The eight label actions each take the caller's label tables as `with:` inputs, in JSON, so a table lives in the caller's workflow and the action needs no checkout.
Each `action.yml` gives its tables' format; label names match case-insensitively.
A malformed table fails the step, whatever the action does with other errors.

Some tables are read by more than one action, so a caller that uses several gives each the same table and keeps them in step:

- the mutually exclusive sets and prefixes: `enforce-mutually-exclusive-labels`, `propagate-issue-labels` and `reconcile-issue-labels`;
- the snooze labels: `snooze-issues`, `wake-snoozed-issues`, and one of `enforce-mutually-exclusive-labels`' sets, so an issue carries at most one;
- the labels `label-by-files` decides with an `any-file` rule: `label-by-title`'s `skip-on-pull-requests`, and `propagate-issue-labels`' and `reconcile-issue-labels`' `excluded-labels`, so neither a title nor a linked issue re-adds one the files removed.

Two command-line tools apply a label action's rules to history: `label-by-title/backfill_labels_by_title.py` to every issue's and pull request's title, and `label-by-files/audit_labels_by_files.py` to every merged pull request's files.
Each reports what it would change with `-n` and changes it with `-f`, and reads the rules from the caller's workflow with `--workflow`, which needs `requirements.txt` installed:

```sh
GITHUB_TOKEN=... GITHUB_REPOSITORY=owner/repo \
  python3 label-by-files/audit_labels_by_files.py -n --limit 50 --workflow ../repo/.github/workflows/label-by-files.yml
```

### The merge gate and orchestrate's labels

`check-blocking-labels` is the merge gate that the label protocol of the [orchestrate plugin](https://github.com/aunger/dot-and-tilde/tree/main/plugins/orchestrate) relies on.
The Orchestrator puts `orchestrating` on a pull request for the whole cycle, and marks open review and verification work with `changes requested`, `changes done` and `verification needed`; it never merges, and nothing in the cycle opens the gate early.
A project using both pairs them like this:

- One job, on `pull_request` `[opened, reopened, synchronize, labeled, unlabeled, closed]` and `issues` `[labeled]`, runs `enforce-mutually-exclusive-labels` on `labeled` events, then `check-blocking-labels` on pull request events, passing the first step's `removed` output as `removed-labels`.
  The job's name is a required status check.
- `blocking-labels` is `["orchestrating", "verification needed", "changes requested", "changes done"]`.
- `exclusive-sets` holds `["orchestrate", "orchestrating"]`, `["verification needed", "verified"]` and `["changes requested", "changes done"]` as three separate sets, and `exclusive-prefixes` holds `c-a-` and `c-r-`.
  The two review pairs stay apart because they track different things, the before-merging items and the Author and Reviewer round: one set would let `verified` remove `changes requested` and open the gate.
- Enforcement runs first because it can remove a blocking label (adding `orchestrate` removes `orchestrating`), and a removal made with `GITHUB_TOKEN` starts no new run, so the gate must hear about it in the same job.
- The project's `.claude/orchestrate.json` leaves the gate's check off `named_checks` and matches it with `ignored_check_regex`: it is red by design for the whole cycle, so the cycle neither waits for it to pass nor treats it as a failure.
- `propagate-issue-labels` lists the protocol's six labels (`orchestrate`, `orchestrating`, `verification needed`, `verified`, `changes requested` and `changes done`) in `excluded-labels`, since an issue and its pull request carry them separately, and `wake-snoozed-issues` lists them in `remove-on-wake`, so a woken issue does not resume mid-cycle.

The orchestrate plugin's README lists the protocol's labels from its side.

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
