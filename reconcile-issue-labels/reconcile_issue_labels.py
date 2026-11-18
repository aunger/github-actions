#!/usr/bin/env python3
"""Apply propagate-issue-labels' rule to every open pull request.

The propagate-issue-labels action runs from a `pull_request` event and looks
only at the pull request that fired it. GitHub sends no event when a pull
request is linked to an issue from the Development sidebar after it is open,
so a pull request linked that way that then sees no further event its caller
listens for before it merges would never be revisited, and its issue's labels
would never be copied.

This closes that gap by walking every open pull request and applying the same
rule, the mutual-exclusion skip and the excluded labels included, to each. It
is meant to be run on demand (`workflow_dispatch`) rather than on a schedule:
the gap is narrow once the propagating workflow listens for a pull request's
ordinary activity, and polling every open pull request on a timer costs API
calls for it indefinitely.

Usage:
    python3 reconcile_issue_labels.py

Exit code:
    0  every open pull request was reconciled without error, whether or not
       any labels were added.
    1  required configuration is missing or invalid, the open pull requests
       could not be listed, or reconciling at least one failed.

Required environment variables:
    GITHUB_TOKEN        Token with issues: write and pull-requests: write
    GITHUB_REPOSITORY   Owner/repo (e.g. "octocat/hello-world")

Optional environment variables:
    EXCLUDED_LABELS, EXCLUSIVE_SETS, EXCLUSIVE_PREFIXES
                        As for propagate_issue_labels.py.
"""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "propagate-issue-labels")
)

import propagate_issue_labels as pil

lc = pil.lc


def fetch_open_pr_numbers(repo: str, token: str) -> list[int]:
    """Return the number of every open pull request in *repo*."""
    return [pr["number"] for pr in lc.fetch_open_pull_requests(repo, token)]


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")

    if not token:
        print("Error: GITHUB_TOKEN not set.", file=sys.stderr)
        return 1
    if not repo:
        print("Error: GITHUB_REPOSITORY not set.", file=sys.stderr)
        return 1
    owner_name = pil.split_repo(repo)
    if owner_name is None:
        print(f"Error: GITHUB_REPOSITORY is not owner/repo: {repo!r}", file=sys.stderr)
        return 1

    try:
        config = pil.load_config(dict(os.environ))
    except lc.InputError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    owner, name = owner_name

    try:
        pr_numbers = fetch_open_pr_numbers(repo, token)
    except Exception as exc:  # noqa: BLE001
        print(f"Error fetching open pull requests: {exc}", file=sys.stderr)
        return 1

    if not pr_numbers:
        print("No open pull requests--nothing to reconcile.")
        return 0

    all_succeeded = True
    for pr_number in pr_numbers:
        # One pull request's unexpected failure must not cost the rest their turn.
        try:
            succeeded = pil.propagate_to_pr(owner, name, repo, pr_number, token, config)
        except Exception as exc:  # noqa: BLE001
            print(f"Error reconciling PR #{pr_number}: {exc}", file=sys.stderr)
            succeeded = False
        if not succeeded:
            all_succeeded = False

    return 0 if all_succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
