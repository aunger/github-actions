# dependency-audit

Runs `pip-audit` over every hash-pinned requirements lock in a repository.

```yaml
- uses: actions/checkout@<sha>
- uses: aunger/vcs-automation/dependency-audit@<40-character-sha>
  with:
    lock-glob: scripts/**/requirements*.txt   # default: **/requirements*.txt
    ignore-file: scripts/ci/requirements-audit-ignore.toml   # default: requirements-audit-ignore.toml
```

Run it on pushes and pull requests to the default branch, and on a weekly schedule.
The schedule is what catches an advisory published against a lock nobody has touched, which no pull request causes.

## Why not `pip-audit -r`

The locks are generated with `uv pip compile --universal`, so they cover every interpreter and platform, and some entries carry markers such as `pywin32 ; sys_platform == 'win32'`.
`pip-audit -r <lock>` evaluates those markers against the interpreter it runs under and silently skips the rest.
`audit_requirements.py` re-derives the pin list from the lock text and audits every pin regardless of marker.
A pin it does not recognize is an error, so a change in the lock format shows up as a red run, not as under-coverage.

## The ignore file

A finding can be real, unfixable by regenerating the lock, and unreachable in how the repository uses the package.
Such a finding is recorded in the ignore file rather than suppressed with `--ignore-vuln`, which is global and which would hide it from the stale check below.

```toml
[[ignore."scripts/requirements.txt"]]
id = "PYSEC-0000-1"
package = "somepkg"
reason = "why this finding is tolerated"
remove_when = "what would make this entry stop being needed"
```

- The table name is the lock's path relative to the workspace, with forward slashes.
- `id` is the advisory's primary ID or any alias (a CVE, a GHSA, a PYSEC ID), since which one `pip-audit` reports as primary can change between runs.
  One finding may be matched by one entry only.
- `package` is the distribution the advisory is against; a mismatch fails the run.
- `reason` and `remove_when` carry the argument, so that it can be re-checked later.
- An entry covers its advisory for its package at every version the lock pins that package at.
  What must be unique within a lock is the pair of package and ID.
- An entry is scoped to one lock: a finding that is unreachable in one lock is not automatically unreachable in another.

An entry that no longer matches a reported finding fails the run.
That usually means an upstream cap lifted or the package was dropped, which is when the reasoning deserves re-reading.
Without it, an ignore list only grows and rots into noise.

A file that does not exist is an empty list.

## Exit codes of `audit_requirements.py`

- `0`: every lock is clean, or every finding is ignored and every ignore entry still does work.
- `1`: a finding is not ignored, an ignore entry is stale or mismatched, or a finding is ignored by more than one entry.
- `2`: the ignore file is invalid, a lock could not be parsed, or `pip-audit` could not be run.

## The scanner's own lock

`requirements-audit.txt` here is the hash-pinned lock for `pip-audit` itself.
This repository's CI audits it like any other lock.
