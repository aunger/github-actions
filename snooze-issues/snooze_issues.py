#!/usr/bin/env python3
"""Close an issue when a snooze label is added to it.

For an issue whose revisit condition is elapsed time with no observable signal
to wait on. Adding one of the caller's SNOOZE_LABELS is how a person snoozes
such an issue: this script closes it, so it stops reading as open work, and
the wake-snoozed-issues action reopens it once the label's days have passed.

The issue is closed with state_reason "not_planned" rather than GitHub's
default, "completed": a snoozed issue is deferred, not done, and "completed"
would show it as resolved in the issue list and in search until it wakes.

Keeping at most one snooze label on an issue is the enforce-mutually-exclusive-
labels action's job, given the snooze labels as one of its sets, so this
script only reacts to the label just added.

An issue that is already closed is left alone, which also covers swapping a
snooze label for a longer one on an issue already snoozed.

Usage:
    python3 snooze_issues.py

Exit code:
    0  ADDED_LABEL is not a snooze label, the issue was already closed, or it
       was closed. Also 0, with a warning, when the token, repository, number
       or label is missing, so an event without a label is a skip.
    1  SNOOZE_LABELS or ISSUE_NUMBER is invalid, or reading or closing the
       issue failed.

Required environment variables:
    GITHUB_TOKEN        Token with issues: write
    GITHUB_REPOSITORY   Owner/repo (e.g. "octocat/hello-world")
    ISSUE_NUMBER        Number of the issue
    ADDED_LABEL         Name of the label just added
    SNOOZE_LABELS       JSON object, snooze label: days

Optional environment variables:
    EVENT_ACTION        github.event.action; any value but "labeled" is a skip,
                        since an `unlabeled` event names the label removed
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))

import label_common as lc


def snooze_issue(issue_number: int, snooze_label: str, repo: str, token: str) -> bool:
    """Close *issue_number* for its *snooze_label* snooze.

    Returns True on success. Raises on a failed read or PATCH, so main() can
    exit nonzero: neither failing is benign here.
    """
    issue = lc.gh_api(f"repos/{repo}/issues/{issue_number}", token=token)
    if not isinstance(issue, dict):
        raise RuntimeError(f"unexpected response fetching issue #{issue_number}: {issue!r}")

    if issue.get("state") == "closed":
        print(f"#{issue_number} is already closed--nothing to do.")
        return True

    lc.gh_api(
        f"repos/{repo}/issues/{issue_number}",
        token=token,
        method="PATCH",
        body={"state": "closed", "state_reason": "not_planned"},
        read_response=False,
    )
    print(f"Closed #{issue_number} for its '{snooze_label}' snooze.")
    return True


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    issue_number_str = os.environ.get("ISSUE_NUMBER", "")
    added_label = os.environ.get("ADDED_LABEL", "")

    event_action = os.environ.get("EVENT_ACTION", "")
    if event_action and event_action != "labeled":
        print(f"Event action '{event_action}' is not 'labeled'--skipping snooze.")
        return 0

    for name, value in (
        ("GITHUB_TOKEN", token),
        ("GITHUB_REPOSITORY", repo),
        ("ISSUE_NUMBER", issue_number_str),
        ("ADDED_LABEL", added_label),
    ):
        if not value:
            print(f"Warning: {name} not set--skipping snooze.", file=sys.stderr)
            return 0

    try:
        snooze_labels = lc.parse_snooze_labels("SNOOZE_LABELS", os.environ.get("SNOOZE_LABELS", ""))
    except lc.InputError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if added_label.lower() not in {label.lower() for label in snooze_labels}:
        print(f"Label '{added_label}' is not a snooze label--nothing to do.")
        return 0

    try:
        issue_number = int(issue_number_str)
    except ValueError:
        print(f"Error: ISSUE_NUMBER is not a valid integer: {issue_number_str!r}", file=sys.stderr)
        return 1

    try:
        snooze_issue(issue_number, added_label, repo, token)
    except Exception as exc:  # noqa: BLE001
        print(f"Error snoozing #{issue_number}: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
