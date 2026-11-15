#!/usr/bin/env python3
"""Archive stale test-failure GitHub issues.

Finds all issues (open or closed) labelled ``test-failure`` whose
``updated_at`` timestamp is older than STALE_DAYS (default 21).  For
each such issue the ``test-failure`` label is removed and
``test-failure-archive`` is added, so that a reporter which finds an
existing issue for a failing test by the ``test-failure`` label no
longer finds it, and files a fresh one.  Closed issues are included
because a closed issue that still carries ``test-failure`` would still
be found, and could be reopened by a later failure of the same test.

Usage:
    python3 archive_stale_test_failures.py

Exit code is always 0; API failures are logged but do not fail the
CI run.

Required environment variables:
    GITHUB_TOKEN        Personal access token or Actions secret with
                        issues: write
    GITHUB_REPOSITORY   Owner/repo (e.g. "octocat/hello-world")

Optional environment variables:
    STALE_DAYS          Number of inactivity days before an issue is
                        archived (default: 21)
"""

import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))

from github_api import gh_api

# ---------------------------------------------------------------------------
# Label name constants
# ---------------------------------------------------------------------------

LABEL_TEST_FAILURE: str = "test-failure"
LABEL_TEST_FAILURE_ARCHIVE: str = "test-failure-archive"


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def fetch_stale_issues(repo: str, token: str, cutoff: datetime.datetime) -> list[dict]:
    """Return all test-failure issues, open or closed, last updated before *cutoff*.

    Paginates through all pages automatically.
    """
    issues: list[dict] = []
    page = 1
    while True:
        batch = gh_api(
            # The endpoint lists only open issues unless asked for state=all.
            f"repos/{repo}/issues?labels={LABEL_TEST_FAILURE}&state=all&per_page=100&page={page}",
            token=token,
        )
        if not batch:
            break
        issues.extend(batch)
        page += 1

    stale = []
    for issue in issues:
        updated_at = datetime.datetime.fromisoformat(issue["updated_at"])
        if updated_at < cutoff:
            stale.append(issue)
    return stale


def archive_issue(issue: dict, repo: str, token: str) -> None:
    """Swap labels on *issue*: remove test-failure, add test-failure-archive."""
    n = issue["number"]
    current_labels = [
        label["name"] for label in issue["labels"] if label["name"] != LABEL_TEST_FAILURE
    ]
    current_labels.append(LABEL_TEST_FAILURE_ARCHIVE)
    gh_api(
        f"repos/{repo}/issues/{n}",
        token=token,
        method="PATCH",
        body={"labels": current_labels},
    )
    print(f"Archived issue #{n}: {issue['title']}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    stale_days = int(os.environ.get("STALE_DAYS", "21"))

    if not token:
        print("Warning: GITHUB_TOKEN not set; skipping archival.", file=sys.stderr)
        return 0
    if not repo:
        print("Warning: GITHUB_REPOSITORY not set; skipping archival.", file=sys.stderr)
        return 0

    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=stale_days)

    try:
        stale = fetch_stale_issues(repo, token, cutoff)
    except Exception as exc:  # noqa: BLE001
        print(f"Error fetching issues: {exc}", file=sys.stderr)
        return 0

    archived = 0
    for issue in stale:
        try:
            archive_issue(issue, repo, token)
            archived += 1
        except Exception as exc:  # noqa: BLE001
            print(
                f"Error archiving issue #{issue['number']}: {exc}",
                file=sys.stderr,
            )

    print(f"Done. Archived {archived} issue(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
