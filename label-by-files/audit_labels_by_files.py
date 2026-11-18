#!/usr/bin/env python3
"""Audit merged pull requests against label_by_files.py's rules.

Applies the rules to the changed-file list of every merged pull request and
compares the verdict with the labels each one carries. One walk does two jobs:
it is the dry run that makes checking a rule change against real history
cheap, and with -f it is the backfill for pull requests that merged before a
rule existed. The label-by-files action sees only pull requests that are open
while it is in place, so history is otherwise never revisited.

The rules come from the caller's workflow, read from the step that uses the
label-by-files action (`--workflow`), or are given directly (`--rules`), in
the action's input format.

Before exiting, prints a JSON report of what happened (or would happen):

    add     labels the rules apply that the pull request lacks
    match   labels the rules apply that the pull request already carries
    remove  labels the pull request carries that the rules forbid

Only any-file labels can appear under "remove": every-file labels are
add-only, since for them "no changed file justifies it" is an absence of
evidence rather than evidence of absence.

Each category maps a label name to the sorted list of pull request numbers it
applies to.

Usage:
    python3 label-by-files/audit_labels_by_files.py (-n | -f)
        (--workflow FILE | --rules JSON) [-q] [--limit N]

    -n, --dry-run   Compute and print the report; apply nothing.
    -f, --force     Apply the "add" and "remove" categories, then print the
                    report.
    -q, --quiet     Suppress the JSON report.
    --limit N       Consider only the N most recently merged pull requests.

    Exactly one of -n or -f is required, as with git clean: without one the
    script refuses to guess and does nothing.

A pull request whose changed-file list may be truncated, or cannot be fetched,
yields no verdict: it is skipped and counted rather than judged on a partial
diff.

Cost is roughly one API call per pull request examined, against a 5000 an hour
authenticated limit; --limit keeps a routine check after a rule change on the
recent tail rather than the whole history.

Exit code:
    0  on success, whether or not any labels were changed
    1  if required configuration is missing or invalid, --limit is not
       positive, the pull request list cannot be fetched, or (-f only)
       writing labels to at least one pull request failed

Required environment variables:
    GITHUB_TOKEN        Token with issues: write and pull-requests: write
    GITHUB_REPOSITORY   Owner/repo (e.g. "octocat/hello-world")
"""

import argparse
import os
import sys
import urllib.error
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))

import label_by_files as lbf
import label_common as lc
import workflow_inputs

ACTION = "label-by-files"
PER_PAGE = 100


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def fetch_merged_pulls(repo: str, token: str, limit: int | None = None) -> list[dict]:
    """Return merged pull requests, most recently merged first.

    Walks the closed pull requests by `updated_at`, newest first, and keeps
    those with a `merged_at`, since a closed pull request may have been closed
    unmerged. Stops once *limit* are collected or at a page shorter than
    PER_PAGE, so a small --limit costs a page or two.

    `updated_at` stands in for the merge time, which the API cannot sort by:
    merging updates a pull request, and a merged one rarely changes again. A
    label changed after the merge (a correction, or a run of this script)
    sorts it later than its merge, an approximation accepted for a check on
    the recent tail.
    """
    pulls: list[dict] = []
    page = 1
    while True:
        batch = lc.gh_api(
            f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc"
            f"&per_page={PER_PAGE}&page={page}",
            token=token,
        )
        if not batch:
            return pulls
        for pull in batch:
            if not pull.get("merged_at"):
                continue
            pulls.append(pull)
            if limit is not None and len(pulls) >= limit:
                return pulls
        if len(batch) < PER_PAGE:
            return pulls
        page += 1


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


class Report(NamedTuple):
    """What the audit found, and the writes -f would make.

    add, match and remove map a label to the pull request numbers it applies
    to (the report); to_add and to_remove map a pull request number to the
    labels to write (the -f payload); skipped lists the pull requests that
    yielded no verdict.
    """

    add: dict[str, list[int]]
    match: dict[str, list[int]]
    remove: dict[str, list[int]]
    to_add: dict[int, list[str]]
    to_remove: dict[int, list[str]]
    skipped: list[int]


def build_report(
    pulls: list[dict],
    fetch_paths: Callable[[int], list[str] | None],
    rules: list[lbf.Rule],
) -> Report:
    """Classify each pull request in *pulls* against its own changed files.

    *fetch_paths* maps a pull request number to its changed paths, or to None
    when no reliable list is available; that pull request is skipped rather
    than judged on a partial diff.
    """
    add: dict[str, list[int]] = defaultdict(list)
    match: dict[str, list[int]] = defaultdict(list)
    remove: dict[str, list[int]] = defaultdict(list)
    to_add: dict[int, list[str]] = defaultdict(list)
    to_remove: dict[int, list[str]] = defaultdict(list)
    skipped: list[int] = []

    for pull in pulls:
        number = pull["number"]
        paths = fetch_paths(number)
        if paths is None:
            skipped.append(number)
            continue

        # Keyed by lowercase name, valued by GitHub's spelling, so a removal
        # deletes the label by the name the API knows.
        carried = {lbl["name"].lower(): lbl["name"] for lbl in pull.get("labels", [])}

        for label in lbf.matching_labels(paths, rules):
            if label.lower() in carried:
                match[label].append(number)
            else:
                add[label].append(number)
                to_add[number].append(label)

        for label in lbf.labels_to_remove(paths, rules):
            name = carried.get(label.lower())
            if name is not None:
                remove[label].append(number)
                to_remove[number].append(name)

    return Report(dict(add), dict(match), dict(remove), dict(to_add), dict(to_remove), skipped)


def apply_changes(
    to_add: dict[int, list[str]],
    to_remove: dict[int, list[str]],
    repo: str,
    token: str,
) -> bool:
    """Make every pending addition and removal, logging failures without stopping.

    Returns True if every write succeeded.
    """
    all_succeeded = True
    for number, labels in to_add.items():
        if not lbf.add_labels(repo, number, labels, token):
            all_succeeded = False
    for number, labels in to_remove.items():
        if not lc.remove_labels(number, labels, repo, token, reason="unjustified"):
            all_succeeded = False
    return all_succeeded


def format_report(report: Report) -> str:
    """Render *report*'s three categories in the shape lc.format_categories gives."""
    return lc.format_categories(
        [("add", report.add), ("match", report.match), ("remove", report.remove)]
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("-n", "--dry-run", action="store_true", help="Report only; apply nothing.")
    mode.add_argument("-f", "--force", action="store_true", help="Apply the adds and removals.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--workflow",
        type=Path,
        metavar="FILE",
        help=f"Read the rules from the step in FILE that uses the {ACTION} action.",
    )
    source.add_argument("--rules", metavar="JSON", help="The rules, as the action's rules input.")
    parser.add_argument("-q", "--quiet", action="store_true", help="Suppress the JSON report.")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Consider only the N most recently merged pull requests.",
    )
    return parser.parse_args(argv)


def load_rules(args: argparse.Namespace) -> list[lbf.Rule]:
    """Return the rules *args* name; raises lc.InputError or WorkflowInputError."""
    if args.workflow is not None:
        text = workflow_inputs.action_inputs(args.workflow, ACTION).get("rules", "")
    else:
        text = args.rules
    return lbf.parse_rules(text, name="rules")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")

    if not token:
        print("Error: GITHUB_TOKEN not set.", file=sys.stderr)
        return 1
    if not repo:
        print("Error: GITHUB_REPOSITORY not set.", file=sys.stderr)
        return 1
    if args.limit is not None and args.limit < 1:
        print(f"Error: --limit must be at least 1: {args.limit}", file=sys.stderr)
        return 1

    try:
        rules = load_rules(args)
    except (lc.InputError, workflow_inputs.WorkflowInputError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        pulls = fetch_merged_pulls(repo, token, args.limit)
    except Exception as exc:  # noqa: BLE001
        print(f"Error fetching pull requests: {exc}", file=sys.stderr)
        return 1

    def changed_paths(number: int) -> list[str] | None:
        try:
            return lbf.fetch_changed_files(repo, number, token)
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            print(f"Error fetching changed files for #{number}: {exc}", file=sys.stderr)
            return None

    report = build_report(pulls, changed_paths, rules)

    exit_code = 0
    if args.force and not apply_changes(report.to_add, report.to_remove, repo, token):
        exit_code = 1

    if report.skipped:
        print(
            f"Skipped {len(report.skipped)} of {len(pulls)} pull requests with no reliable "
            f"changed-file list: {sorted(report.skipped)}",
            file=sys.stderr,
        )

    if not args.quiet:
        print(format_report(report))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
