#!/usr/bin/env python3
"""Keep at most one label of each mutually exclusive set on an issue or pull request.

When a label is added that belongs to one of the caller's EXCLUSIVE_SETS, or
starts with one of its EXCLUSIVE_PREFIXES, every other label the item carries
from the same set, or with the same prefix, is removed. Matching is
case-insensitive. The label just added always stays: the newest choice wins.
A run whose label is no longer on the item when it reads the labels does
nothing, so a later choice is not undone by an earlier run.

Usage:
    python3 enforce_mutually_exclusive_labels.py

Exit code:
    0  the added label is in no set and has no listed prefix, nothing
       conflicted, or every conflicting label was removed (a 404 on removal
       means it was already gone, such as through a race with another
       process, and is not a failure). Also 0, with a warning, when the
       token, repository, number or label is missing, so an event without a
       label is a skip.
    1  ISSUE_NUMBER is not an integer, a table is invalid, the item's labels
       could not be read, or a removal failed for a reason other than 404. A
       transient GitHub failure (a 5xx, a 429, a network error) is retried
       with backoff first, so a nonzero exit reflects a persistent failure.

Required environment variables:
    GITHUB_TOKEN        Token with issues: write (and pull-requests: write for
                        a pull request)
    GITHUB_REPOSITORY   Owner/repo (e.g. "octocat/hello-world")
    ISSUE_NUMBER        Number of the issue or pull request
    ADDED_LABEL         Name of the label just added

Optional environment variables:
    EVENT_ACTION        github.event.action; any value but "labeled" is a skip,
                        since an `unlabeled` event names the label removed
    EXCLUSIVE_SETS      JSON array of arrays of labels; at most one member of
                        each array belongs on an item
    EXCLUSIVE_PREFIXES  JSON array of prefixes; at most one label starting
                        with each belongs on an item
    GITHUB_OUTPUT       Set by Actions. Once every removal has succeeded, the
                        removed labels are written there as the step output
                        `removed`, a JSON array (see write_removed_output).
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))

import label_common as lc


def write_removed_output(removed: list[str]) -> None:
    """Record *removed* as this step's `removed` output, a JSON array.

    Written only once every removal has succeeded or found the label already
    gone, so each name in it is known to be off the item. A later step in the
    same job whose event payload predates the removal, such as the
    check-blocking-labels action, drops these names from that payload. A
    removal made with GITHUB_TOKEN starts no new workflow run, so without this
    the later step would count a label that is gone.
    """
    lc.write_output("removed", json.dumps(removed))


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    issue_number_str = os.environ.get("ISSUE_NUMBER", "")
    added_label = os.environ.get("ADDED_LABEL", "")

    # The tables are read first, so a malformed one fails the step on every
    # event, not only on the next one that has work to do.
    try:
        rules = lc.ExclusiveRules.parse(
            "EXCLUSIVE_SETS",
            os.environ.get("EXCLUSIVE_SETS", ""),
            "EXCLUSIVE_PREFIXES",
            os.environ.get("EXCLUSIVE_PREFIXES", ""),
        )
    except lc.InputError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    event_action = os.environ.get("EVENT_ACTION", "")
    if event_action and event_action != "labeled":
        print(f"Event action '{event_action}' is not 'labeled'--skipping enforcement.")
        return 0

    for name, value in (
        ("GITHUB_TOKEN", token),
        ("GITHUB_REPOSITORY", repo),
        ("ISSUE_NUMBER", issue_number_str),
        ("ADDED_LABEL", added_label),
    ):
        if not value:
            print(f"Warning: {name} not set--skipping enforcement.", file=sys.stderr)
            return 0

    try:
        issue_number = int(issue_number_str)
    except ValueError:
        print(f"Error: ISSUE_NUMBER is not a valid integer: {issue_number_str!r}", file=sys.stderr)
        return 1

    if not rules.governs(added_label):
        print(f"Label '{added_label}' is not in any mutually exclusive set--nothing to do.")
        return 0

    try:
        current_labels = lc.fetch_label_names(repo, issue_number, token)
    except Exception as exc:  # noqa: BLE001
        print(f"Error fetching issue #{issue_number}: {exc}", file=sys.stderr)
        return 1

    # Two runs for two labels of one set, started close together, would each
    # remove the other's. A run whose label is already gone stands aside, so
    # the later choice wins; runs that read the labels at the same moment can
    # still race.
    if added_label.lower() not in {label.lower() for label in current_labels}:
        print(f"Label '{added_label}' is no longer on #{issue_number}--nothing to do.")
        return 0

    to_remove = rules.conflicts(added_label, current_labels)
    if not to_remove:
        print(f"No conflicting labels found for '{added_label}' on #{issue_number}--nothing to do.")
        return 0

    if not lc.remove_labels(issue_number, to_remove, repo, token):
        return 1
    write_removed_output(to_remove)
    return 0


if __name__ == "__main__":
    sys.exit(main())
