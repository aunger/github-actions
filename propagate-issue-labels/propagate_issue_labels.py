#!/usr/bin/env python3
"""Copy a linked issue's labels onto a pull request that closes it.

When a pull request closes an issue through a closing keyword ("Fixes #N",
"Closes #N" and the other variants GitHub recognizes) or a manually linked
issue, this adds each of that issue's labels to the pull request.

The relationship is read from the GraphQL API's `closingIssuesReferences` on
the pull request, which is GitHub's own detection of closing keywords and
links, not a regex over the body, so every variant and every manual link is
covered.

Labels are only ever added, never removed. A label already on the pull
request is left alone. A candidate is also skipped when adding it would let
mutual-exclusion enforcement (the enforce-mutually-exclusive-labels action,
given the same EXCLUSIVE_SETS and EXCLUSIVE_PREFIXES) remove a label the pull
request already carries: an existing label always wins over a conflicting one
copied in from an issue.

EXCLUDED_LABELS are never copied at all, even onto a pull request that carries
none of their conflicting siblings. A caller lists there the labels an issue
and its pull request are meant to carry differently: process state that a
workflow tracks on each separately, which copying would fight, and labels the
pull request's own changed files decide (label-by-files' any-file rules), for
which what the issue is about is no evidence. See labels_to_propagate().

GitHub sends no webhook event when a pull request is linked to an issue from
the Development sidebar after it is open, so a caller runs this on the events
a pull request commonly sees again (opened, reopened, a body edit), and the
reconcile-issue-labels action applies the same rule to every open pull request
on demand for one that sees none of them before it merges.

Usage:
    python3 propagate_issue_labels.py

Exit code:
    0  the pull request closes no labeled issue, or every eligible label was
       added, or every candidate was skipped or excluded (none of which is a
       failure).
    1  required configuration is missing or invalid, or reading or adding
       labels failed.

Required environment variables:
    GITHUB_TOKEN        Token with issues: write and pull-requests: write
    GITHUB_REPOSITORY   Owner/repo (e.g. "octocat/hello-world")
    PR_NUMBER           Number of the pull request

Optional environment variables:
    EXCLUDED_LABELS     JSON array of labels never copied
    EXCLUSIVE_SETS      JSON array of arrays of mutually exclusive labels
    EXCLUSIVE_PREFIXES  JSON array of mutually exclusive prefixes
"""

import json
import os
import sys
import urllib.error
from dataclasses import dataclass, field

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))

import label_common as lc
from github_api import TIMEOUT_SECONDS
from github_headers import github_headers
from https_only import urlopen_https

# ---------------------------------------------------------------------------
# GraphQL
# ---------------------------------------------------------------------------

GRAPHQL_URL = "https://api.github.com/graphql"

CLOSING_ISSUES_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      closingIssuesReferences(first: 50) {
        nodes {
          number
          repository {
            nameWithOwner
          }
          labels(first: 100) {
            nodes {
              name
            }
          }
        }
      }
    }
  }
}
"""


def graphql_query(query: str, variables: dict, token: str) -> dict:
    """Execute a GitHub GraphQL query and return its "data" object.

    Raises ``RuntimeError`` if the response carries a top-level "errors"
    list (GraphQL reports errors this way even on an HTTP 200), or
    ``urllib.error.HTTPError``/``URLError`` on request failure.
    """
    body = json.dumps({"query": query, "variables": variables}).encode()
    headers = github_headers(token)
    headers["Content-Type"] = "application/json"
    with urlopen_https(
        GRAPHQL_URL, body, headers=headers, method="POST", timeout=TIMEOUT_SECONDS
    ) as r:
        payload = json.loads(r.read())
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")
    return payload.get("data") or {}


def fetch_closing_issue_labels(
    owner: str,
    name: str,
    pr_number: int,
    token: str,
) -> list[str]:
    """Return the labels of every same-repo issue that *pr_number* closes.

    Cross-repository closing references (a pull request closing an issue in
    another repository) are skipped: the label actions work within one
    repository throughout.

    Labels are returned in the order GitHub lists the closing issues and,
    within each issue, the order it lists that issue's labels. Duplicates
    across multiple closing issues are not removed here; that happens in
    labels_to_propagate().
    """
    data = graphql_query(
        CLOSING_ISSUES_QUERY,
        {"owner": owner, "name": name, "number": pr_number},
        token,
    )
    pull_request = (data.get("repository") or {}).get("pullRequest")
    if not pull_request:
        return []
    nodes = (pull_request.get("closingIssuesReferences") or {}).get("nodes") or []
    repo_full_name = f"{owner}/{name}"

    labels: list[str] = []
    for node in nodes:
        if not node:
            continue
        node_repo = (node.get("repository") or {}).get("nameWithOwner")
        if node_repo != repo_full_name:
            continue
        labels.extend(lbl["name"] for lbl in (node.get("labels") or {}).get("nodes") or [])
    return labels


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def labels_to_propagate(
    current_pr_labels: list[str],
    issue_labels: list[str],
    excluded_labels: frozenset[str] = frozenset(),
    exclusive: lc.ExclusiveRules | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Partition *issue_labels* into what to add, skip, and exclude.

    Returns (to_add, skipped, excluded):
        to_add    issue labels, not already on the PR, that are safe to add
        skipped   issue labels left out because adding them would let
                  mutual-exclusion enforcement under *exclusive* remove a
                  label already staged on the PR (a current PR label, or one
                  accepted earlier in this same call)
        excluded  issue labels in *excluded_labels* (lowercased), never
                  considered at all

    A label already on the PR (case-insensitively) is dropped from every list:
    there is nothing to do for it. A label repeated across several closing
    issues is considered once.

    *issue_labels* is processed in order, staging each accepted label into a
    working copy of the PR's labels, so later candidates in the same call see
    earlier additions. That keeps one run consistent when several linked
    issues supply conflicting labels, while a label the PR already had always
    wins, since it seeds the working set before any candidate is considered.
    """
    staged = list(current_pr_labels)
    already_lower = {lbl.lower() for lbl in current_pr_labels}
    to_add: list[str] = []
    skipped: list[str] = []
    excluded: list[str] = []
    seen_lower: set[str] = set()

    for label in issue_labels:
        lower = label.lower()
        if lower in seen_lower:
            continue
        seen_lower.add(lower)

        if lower in excluded_labels:
            excluded.append(label)
            continue
        if lower in already_lower:
            continue

        if exclusive is not None and exclusive.conflicts(label, staged):
            skipped.append(label)
            continue

        to_add.append(label)
        staged.append(label)

    return to_add, skipped, excluded


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """The caller's tables: labels never copied, and the exclusive rules."""

    excluded_labels: frozenset[str] = frozenset()
    exclusive: lc.ExclusiveRules = field(default_factory=lc.ExclusiveRules)


def load_config(environ: dict[str, str]) -> Config:
    """Read EXCLUDED_LABELS, EXCLUSIVE_SETS and EXCLUSIVE_PREFIXES; raises lc.InputError."""
    return Config(
        lc.parse_label_list("EXCLUDED_LABELS", environ.get("EXCLUDED_LABELS", "")),
        lc.ExclusiveRules.parse(
            "EXCLUSIVE_SETS",
            environ.get("EXCLUSIVE_SETS", ""),
            "EXCLUSIVE_PREFIXES",
            environ.get("EXCLUSIVE_PREFIXES", ""),
        ),
    )


def split_repo(repo: str) -> tuple[str, str] | None:
    """Return (owner, name) for an "owner/name" *repo*, or None if it is not one."""
    owner, slash, name = repo.partition("/")
    if not owner or not slash or not name or "/" in name:
        return None
    return owner, name


# ---------------------------------------------------------------------------
# Single-PR propagation (shared by main() and reconcile-issue-labels)
# ---------------------------------------------------------------------------


def propagate_to_pr(
    owner: str, name: str, repo: str, pr_number: int, token: str, config: Config
) -> bool:
    """Copy *pr_number*'s closing issues' labels onto it, under *config*.

    Returns True on success, including every no-op case (no closing issues,
    no labels on them, or every candidate skipped or excluded). Returns False
    only if reading the closing issues or the PR, or adding labels, failed.
    """
    try:
        issue_labels = fetch_closing_issue_labels(owner, name, pr_number, token)
    except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError) as exc:
        print(f"Error fetching closing issues for PR #{pr_number}: {exc}", file=sys.stderr)
        return False

    if not issue_labels:
        print(f"PR #{pr_number} has no labeled closing issue references--nothing to do.")
        return True

    try:
        current_pr_labels = lc.fetch_label_names(repo, pr_number, token)
    except Exception as exc:  # noqa: BLE001
        print(f"Error fetching PR #{pr_number}: {exc}", file=sys.stderr)
        return False

    to_add, skipped, excluded = labels_to_propagate(
        current_pr_labels, issue_labels, config.excluded_labels, config.exclusive
    )

    if excluded:
        print(
            f"Excluding labels {excluded} on PR #{pr_number}: the caller's excluded-labels "
            "lists them as labels an issue and its pull request carry separately."
        )

    if skipped:
        print(
            f"Skipping labels {skipped} on PR #{pr_number}: applying them would let "
            "mutual-exclusion enforcement remove a label the PR already carries."
        )

    if not to_add:
        print(f"No labels to propagate to PR #{pr_number}--nothing to do.")
        return True

    try:
        lc.add_labels(repo, pr_number, to_add, token)
        print(f"Applied labels {to_add} to PR #{pr_number}.")
    except urllib.error.HTTPError as exc:
        print(f"Error applying labels {to_add} to PR #{pr_number}: {exc}", file=sys.stderr)
        return False
    except urllib.error.URLError as exc:
        print(f"Network error applying labels {to_add} to PR #{pr_number}: {exc}", file=sys.stderr)
        return False

    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    pr_number_str = os.environ.get("PR_NUMBER", "")

    for variable, value in (
        ("GITHUB_TOKEN", token),
        ("GITHUB_REPOSITORY", repo),
        ("PR_NUMBER", pr_number_str),
    ):
        if not value:
            print(f"Error: {variable} not set.", file=sys.stderr)
            return 1
    owner_name = split_repo(repo)
    if owner_name is None:
        print(f"Error: GITHUB_REPOSITORY is not owner/repo: {repo!r}", file=sys.stderr)
        return 1

    try:
        pr_number = int(pr_number_str)
    except ValueError:
        print(f"Error: PR_NUMBER is not a valid integer: {pr_number_str!r}", file=sys.stderr)
        return 1

    try:
        config = load_config(dict(os.environ))
    except lc.InputError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    owner, name = owner_name
    return 0 if propagate_to_pr(owner, name, repo, pr_number, token, config) else 1


if __name__ == "__main__":
    sys.exit(main())
