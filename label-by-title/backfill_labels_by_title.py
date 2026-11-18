#!/usr/bin/env python3
"""Backfill title labels across every existing issue and pull request.

Applies label_by_title.py's rules to the title of every issue and pull request
in the repository, and adds any label a title matches that is not already
there. Labels are only ever added, never removed. The label-by-title action
sees only the items whose titles change while it is in place, so this is how
history catches up with a rule, and a dry run is how a rule change is checked
against the titles it would have labeled.

On a pull request, the skip-on-pull-requests labels are neither applied nor
reported, since the changed files decide those (audit_labels_by_files.py in
label-by-files covers them).

The rules come from the caller's workflow, read from the step that uses the
label-by-title action (`--workflow`), or are given directly (`--rules`, and
optionally `--skip-on-pull-requests`), in the action's input format.

Before exiting, prints a JSON report of what happened (or would happen):

    add     labels the rules apply that the item lacks
    match   labels the rules apply that the item already carries
    miss    labels with a rule that the item carries but the rules would not
            apply

Each category maps a label name to the sorted list of issue and pull request
numbers it applies to.

Usage:
    python3 label-by-title/backfill_labels_by_title.py (-n | -f)
        (--workflow FILE | --rules JSON [--skip-on-pull-requests JSON])
        [-q] [--skip-matches[=LABELS]]

    -n, --dry-run   Compute and print the report; apply nothing.
    -f, --force     Apply the "add" category, then print the report.
    -q, --quiet     Suppress the JSON report.
    --skip-matches[=LABELS]
                    Omit LABELS (comma-separated) from the "match" category.
                    With no LABELS, omit the "match" category entirely.

    Exactly one of -n or -f is required, as with git clean: without one the
    script refuses to guess and does nothing.

Exit code:
    0  on success, whether or not any labels were applied
    1  if required configuration is missing or invalid, the issue list cannot
       be fetched, or (-f only) applying labels to at least one item failed

Required environment variables:
    GITHUB_TOKEN        Token with issues: write and pull-requests: write
    GITHUB_REPOSITORY   Owner/repo (e.g. "octocat/hello-world")
"""

import argparse
import os
import sys
import urllib.error
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))

import label_by_title as lbt
import label_common as lc
import workflow_inputs

ACTION = "label-by-title"


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def fetch_all_issues(repo: str, token: str) -> list[dict]:
    """Return every issue and pull request in *repo*, open or closed.

    The /issues endpoint returns both; a pull request carries a `pull_request`
    key and is labeled exactly as an issue is.
    """
    items: list[dict] = []
    page = 1
    while True:
        batch = lc.gh_api(f"repos/{repo}/issues?state=all&per_page=100&page={page}", token=token)
        if not batch:
            break
        items.extend(batch)
        page += 1
    return items


def build_report(
    items: list[dict],
    rules: dict,
    skip_on_pull_requests: frozenset[str] = frozenset(),
) -> tuple[
    dict[str, list[int]],
    dict[str, list[int]],
    dict[str, list[int]],
    dict[int, list[str]],
]:
    """Classify every item's title-matched labels against its current labels.

    Returns (add, match, miss, to_apply):
        add[label]        numbers missing a label the rules would apply
        match[label]      numbers already carrying a label the rules apply
        miss[label]       numbers carrying a label with a rule that the
                          rules would not apply to them
        to_apply[number]  labels to add to that item (the -f payload)
    """
    add: dict[str, list[int]] = defaultdict(list)
    match: dict[str, list[int]] = defaultdict(list)
    miss: dict[str, list[int]] = defaultdict(list)
    to_apply: dict[int, list[str]] = defaultdict(list)

    for item in items:
        number = item["number"]
        current = {lbl["name"].lower() for lbl in item.get("labels", [])}
        is_pr = "pull_request" in item
        matched = lbt.matching_labels(item["title"], rules, is_pr, skip_on_pull_requests)
        matched_lower = {label.lower() for label in matched}
        # Only labels with a rule are tracked: a label no rule can apply would
        # sit in "miss" on every run for every item that carries it. On a pull
        # request the skipped labels are left out as well, or every PR carrying
        # one would land in "miss" for the same reason.
        tracked = [
            label for label in rules if not (is_pr and label.lower() in skip_on_pull_requests)
        ]

        for label in matched:
            if label.lower() in current:
                match[label].append(number)
            else:
                add[label].append(number)
                to_apply[number].append(label)

        for label in tracked:
            if label.lower() in current and label.lower() not in matched_lower:
                miss[label].append(number)

    return add, match, miss, to_apply


def apply_labels(to_apply: dict[int, list[str]], repo: str, token: str) -> bool:
    """Add each item's missing labels, logging a failure without stopping.

    Returns True if every addition succeeded.
    """
    all_succeeded = True
    for number, labels in to_apply.items():
        try:
            lc.add_labels(repo, number, labels, token)
            print(f"Applied labels {labels} to #{number}.")
        except urllib.error.HTTPError as exc:
            print(f"Error applying labels {labels} to #{number}: {exc}", file=sys.stderr)
            all_succeeded = False
        except urllib.error.URLError as exc:
            print(f"Network error applying labels {labels} to #{number}: {exc}", file=sys.stderr)
            all_succeeded = False
    return all_succeeded


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def format_report(
    add: dict[str, list[int]],
    match: dict[str, list[int]],
    miss: dict[str, list[int]],
    include_match: bool = True,
) -> str:
    """Render the add, match and miss categories; *include_match* False drops "match"."""
    categories = [("add", add)]
    if include_match:
        categories.append(("match", match))
    categories.append(("miss", miss))
    return lc.format_categories(categories)


def parse_skip_matches(value: str | None) -> tuple[bool, frozenset[str]]:
    """Interpret the --skip-matches value.

    Returns (include_match, labels_to_skip):
        value is None   flag not given: include "match" in full
        value is ""     flag given with no LABELS: omit "match" entirely
        value is a CSV  flag given with LABELS: include "match" minus those

    Each comma-separated entry is stripped of surrounding whitespace and of one
    more layer of literal quotes, since a shell strips only one
    (--skip-matches='"ci","docs"').
    """
    if value is None:
        return True, frozenset()
    if value == "":
        return False, frozenset()
    labels = frozenset(
        stripped for label in value.split(",") if (stripped := label.strip().strip("'\""))
    )
    return True, labels


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("-n", "--dry-run", action="store_true", help="Report only; apply nothing.")
    mode.add_argument("-f", "--force", action="store_true", help="Apply the missing labels.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--workflow",
        type=Path,
        metavar="FILE",
        help=f"Read the rules from the step in FILE that uses the {ACTION} action.",
    )
    source.add_argument("--rules", metavar="JSON", help="The rules, as the action's rules input.")
    parser.add_argument(
        "--skip-on-pull-requests",
        metavar="JSON",
        default="",
        help="With --rules: the action's skip-on-pull-requests input.",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="Suppress the JSON report.")
    parser.add_argument(
        "--skip-matches",
        nargs="?",
        const="",
        default=None,
        metavar="LABELS",
        help='Omit LABELS (comma-separated) from "match"; with no LABELS, omit "match".',
    )
    return parser.parse_args(argv)


def load_rules(args: argparse.Namespace) -> tuple[dict, frozenset[str]]:
    """Return the rules and skip list *args* name; raises lc.InputError or WorkflowInputError."""
    if args.workflow is not None:
        if args.skip_on_pull_requests:
            raise lc.InputError("--skip-on-pull-requests goes with --rules, not --workflow")
        inputs = workflow_inputs.action_inputs(args.workflow, ACTION)
        rules_text = inputs.get("rules", "")
        skip_text = inputs.get("skip-on-pull-requests", "")
    else:
        rules_text, skip_text = args.rules, args.skip_on_pull_requests
    rules = lbt.parse_rules(rules_text, name="rules")
    skip = lc.parse_label_list("skip-on-pull-requests", skip_text)
    return rules, skip


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

    try:
        rules, skip = load_rules(args)
    except (lc.InputError, workflow_inputs.WorkflowInputError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        items = fetch_all_issues(repo, token)
    except Exception as exc:  # noqa: BLE001
        print(f"Error fetching issues: {exc}", file=sys.stderr)
        return 1

    add, match, miss, to_apply = build_report(items, rules, skip)

    exit_code = 0
    if args.force and not apply_labels(to_apply, repo, token):
        exit_code = 1

    if not args.quiet:
        include_match, labels_to_skip = parse_skip_matches(args.skip_matches)
        if labels_to_skip:
            skipped = {label.lower() for label in labels_to_skip}
            match = {label: nums for label, nums in match.items() if label.lower() not in skipped}
        print(format_report(add, match, miss, include_match=include_match))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
