#!/usr/bin/env python3
"""Apply labels to a pull request from the set of files it changed.

The caller's RULES give each label a list of path globs and one of two modes:

every-file
    Added when every changed file matches one of the label's globs, and never
    removed. "Not every file" is an absence of evidence, not evidence of
    absence: a pull request the label fits may also touch something else, so
    a title rule or a person may rightly have applied it.

any-file
    Added when any changed file matches one of the label's globs, and removed
    when none does. This is sound only because the globs are the label's whole
    definition, so a caller gives this mode only to such a label, and keeps its
    title rule off pull requests (label-by-title's skip-on-pull-requests).

Two properties keep the add-only mode safe:

- A file no glob matches justifies no label, so it stops every every-file
  label from applying: adding a glob can only ever make a label fire more,
  and a gap in the globs only ever makes it fire less.
- An empty file list applies nothing, rather than vacuously matching every
  label.

The any-file mode stays honest about missing evidence too: an empty file list,
or one that may be truncated, yields no verdict at all, neither an add nor a
removal.

Globs are matched against each changed path from the repository root, case-
sensitively: `**/` matches any number of directories, none included, a
trailing `**` matches everything below a directory, and `*`, `?` and `[...]`
match within one path component. `**/test/**` therefore matches a file under
any directory named `test`, but not a file named `test`.

Usage:
    python3 label_by_files.py

Exit code:
    0  nothing to do, the changed-file list may be truncated, or every label
       was added and removed (a 404 on removal means the label was already
       gone and is not a failure).
    1  required configuration is missing or invalid, or a GitHub API call
       failed.

Required environment variables:
    GITHUB_TOKEN        Token with issues: write and pull-requests: write
    GITHUB_REPOSITORY   Owner/repo (e.g. "octocat/hello-world")
    PR_NUMBER           Number of the pull request
    RULES               JSON object, label: {"mode": ..., "paths": [glob, ...]}
"""

import os
import re
import sys
import urllib.error
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))

import label_common as lc
from path_globs import glob_regex

MODES = ("every-file", "any-file")

# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """One label's globs, compiled, and its mode."""

    label: str
    mode: str
    globs: tuple[str, ...]
    patterns: tuple[re.Pattern[str], ...]

    def matches(self, path: str) -> bool:
        return any(pattern.match(path) for pattern in self.patterns)


def parse_rules(text: str, name: str = "RULES") -> list[Rule]:
    """Parse *text*, a JSON object of label: {"mode": ..., "paths": [glob, ...]}.

    Raises lc.InputError on a malformed table. The labels keep the spelling the
    caller gave them, which is the spelling they are applied in.
    """
    data = lc.load_json(name, text)
    if not isinstance(data, dict) or not data:
        raise lc.InputError(f"{name} must be a non-empty JSON object of label: rule")
    rules = []
    seen: set[str] = set()
    for label, rule in data.items():
        if not isinstance(label, str) or not label.strip():
            raise lc.InputError(f"{name}: each label must be a non-empty string, not {label!r}")
        if label.strip().lower() in seen:
            raise lc.InputError(f"{name}: {label!r} is listed twice")
        seen.add(label.strip().lower())
        if not isinstance(rule, dict) or set(rule) != {"mode", "paths"}:
            raise lc.InputError(f'{name}: {label!r} must map to {{"mode": ..., "paths": [...]}}')
        if rule["mode"] not in MODES:
            raise lc.InputError(f"{name}: {label!r} mode must be one of {list(MODES)}")
        globs = rule["paths"]
        if (
            not isinstance(globs, list)
            or not globs
            or not all(isinstance(g, str) and g for g in globs)
        ):
            raise lc.InputError(f"{name}: {label!r} paths must be a non-empty array of globs")
        try:
            patterns = tuple(glob_regex(g) for g in globs)
        except re.error as exc:
            raise lc.InputError(f"{name}: {label!r} has an invalid glob: {exc}") from exc
        rules.append(Rule(label.strip(), rule["mode"], tuple(globs), patterns))
    return rules


def labels_for_path(path: str, rules: list[Rule]) -> frozenset[str]:
    """Return the labels *path* alone justifies (empty when it justifies none)."""
    return frozenset(rule.label for rule in rules if rule.matches(path))


def matching_labels(paths: list[str], rules: list[Rule]) -> list[str]:
    """Return the labels to add to a pull request changing *paths*; [] for no paths.

    Taking the intersection over every-file labels only, and the union over
    any-file labels only, keeps the two modes from leaking into each other.
    """
    if not paths:
        return []
    per_path = [labels_for_path(p, rules) for p in paths]
    every = {r.label for r in rules if r.mode == "every-file"}
    anyf = {r.label for r in rules if r.mode == "any-file"}
    unanimous = frozenset.intersection(*per_path) & every
    existential = frozenset().union(*per_path) & anyf
    return sorted(unanimous | existential)


def labels_to_remove(paths: list[str], rules: list[Rule]) -> list[str]:
    """Return the any-file labels no path in *paths* justifies; [] for no paths.

    This says which labels the diff forbids, not which the pull request
    carries; the caller filters to the ones present.
    """
    if not paths:
        return []
    justified = frozenset().union(*(labels_for_path(p, rules) for p in paths))
    return sorted({r.label for r in rules if r.mode == "any-file"} - justified)


# ---------------------------------------------------------------------------
# GitHub access
# ---------------------------------------------------------------------------

# 10 pages of 100 files. GitHub lists at most 3000 files for a pull request,
# but one with more than 1000 is not worth optimizing for, so
# fetch_changed_files() returns None rather than risk a verdict on a partial
# list.
MAX_PAGES = 10
PER_PAGE = 100


def fetch_changed_files(repo: str, pr_number: int, token: str) -> list[str] | None:
    """Return every path the pull request changed, or None if possibly truncated.

    Stops at a page shorter than PER_PAGE (the normal end of the list) or an
    empty one. Having read MAX_PAGES full pages, the list may go on, so this
    returns None rather than a partial list.
    """
    paths: list[str] = []
    page = 1
    while page <= MAX_PAGES:
        batch = lc.gh_api(
            f"repos/{repo}/pulls/{pr_number}/files?per_page={PER_PAGE}&page={page}",
            token=token,
        )
        if not batch:
            return paths
        paths.extend(item["filename"] for item in batch)
        if len(batch) < PER_PAGE:
            return paths
        page += 1
    return None


# ---------------------------------------------------------------------------
# Label writes
# ---------------------------------------------------------------------------


def add_labels(repo: str, pr_number: int, labels: list[str], token: str) -> bool:
    """Add *labels* to the pull request. Returns True on success."""
    try:
        lc.add_labels(repo, pr_number, labels, token)
        print(f"Applied labels {labels} to #{pr_number}.")
    except urllib.error.HTTPError as exc:
        print(f"Error applying labels {labels} to #{pr_number}: {exc}", file=sys.stderr)
        return False
    except urllib.error.URLError as exc:
        print(f"Network error applying labels {labels} to #{pr_number}: {exc}", file=sys.stderr)
        return False
    return True


def remove_unjustified_labels(repo: str, pr_number: int, labels: list[str], token: str) -> bool:
    """Remove whichever of *labels* the pull request carries.

    The current labels are read first so a DELETE is sent only for a label
    that is there; most pull requests carry none of them, and a DELETE for
    each would 404 and log a spurious "already removed?" line on every run.

    Returns True on success, including when the pull request carries none.
    """
    try:
        current = lc.fetch_label_names(repo, pr_number, token)
    except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError) as exc:
        print(f"Error fetching current labels for #{pr_number}: {exc}", file=sys.stderr)
        return False

    unjustified = {label.lower() for label in labels}
    # Delete by the name GitHub returned, not the rule's spelling.
    carried = [name for name in current if name.lower() in unjustified]
    if not carried:
        return True

    print(
        f"Removing labels {carried} from #{pr_number}: no changed file matches their "
        "any-file rules."
    )
    return lc.remove_labels(pr_number, carried, repo, token, reason="unjustified")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    pr_number_str = os.environ.get("PR_NUMBER", "")

    for name, value in (
        ("GITHUB_TOKEN", token),
        ("GITHUB_REPOSITORY", repo),
        ("PR_NUMBER", pr_number_str),
    ):
        if not value:
            print(f"Error: {name} not set.", file=sys.stderr)
            return 1

    try:
        pr_number = int(pr_number_str)
    except ValueError:
        print(f"Error: PR_NUMBER is not a valid integer: {pr_number_str!r}", file=sys.stderr)
        return 1

    try:
        rules = parse_rules(os.environ.get("RULES", ""))
    except lc.InputError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        paths = fetch_changed_files(repo, pr_number, token)
    except urllib.error.HTTPError as exc:
        print(f"Error fetching changed files for #{pr_number}: {exc}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"Network error fetching changed files for #{pr_number}: {exc}", file=sys.stderr)
        return 1

    if paths is None:
        print(
            f"Changed-file list for #{pr_number} may be truncated "
            f"(hit {MAX_PAGES} pages)--nothing to do."
        )
        return 0

    to_add = matching_labels(paths, rules)
    to_remove = labels_to_remove(paths, rules)

    if not to_add and not to_remove:
        print(f"No label rules matched the changed files on #{pr_number}--nothing to do.")
        return 0

    exit_code = 0
    if to_add and not add_labels(repo, pr_number, to_add, token):
        exit_code = 1
    if to_remove and not remove_unjustified_labels(repo, pr_number, to_remove, token):
        exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
