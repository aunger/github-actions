#!/usr/bin/env python3
"""Apply labels to an issue or pull request whose title matches each label's regex.

The caller's RULES map each label to a regular expression, or to an array of
them that count as alternatives. Matching is a case-insensitive search of the
title, so a rule anchors itself if it needs to; an inline `(?-i:...)` group
opts part of a rule out of case-insensitivity.

On a pull request, SKIP_ON_PULL_REQUESTS labels are never applied: they are
the ones a caller decides from the pull request's changed files instead (the
`any-file` rules of the label-by-files action), so the two signals never race
for the same label. On `opened` both workflows run at once, and whichever
landed last would otherwise decide; a title edit alone would re-add a label
the file signal had removed. Issues have no changed files, so nothing is
skipped there.

Labels are only ever added, never removed.

Usage:
    python3 label_by_title.py

Exit code:
    0  no rule matched, or the matching labels were applied.
    1  required configuration is missing or invalid, or applying labels failed.

Required environment variables:
    GITHUB_TOKEN        Token with issues: write and pull-requests: write
    GITHUB_REPOSITORY   Owner/repo (e.g. "octocat/hello-world")
    ISSUE_NUMBER        Number of the issue or pull request
    ISSUE_TITLE         Its title
    RULES               JSON object, label: regex or array of regexes

Optional environment variables:
    IS_PULL_REQUEST     "true" when the item is a pull request; anything else,
                        unset included, means an issue
    SKIP_ON_PULL_REQUESTS
                        JSON array of labels never applied to a pull request
"""

import os
import re
import sys
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))

import label_common as lc

# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def parse_rules(text: str, name: str = "RULES") -> dict[str, re.Pattern[str]]:
    """Parse *text*, a JSON object of label: regex or array of regexes.

    An array's entries are alternatives, each grouped before they are joined,
    so one alternative's `|` cannot leak into the next. Raises lc.InputError on
    a malformed table or a regex that does not compile. The labels keep the
    spelling the caller gave them, which is the spelling they are applied in.
    """
    data = lc.load_json(name, text)
    if not isinstance(data, dict) or not data:
        raise lc.InputError(f"{name} must be a non-empty JSON object of label: regex")
    rules: dict[str, re.Pattern[str]] = {}
    seen: set[str] = set()
    for label, rule in data.items():
        if not isinstance(label, str) or not label.strip():
            raise lc.InputError(f"{name}: each label must be a non-empty string, not {label!r}")
        if label.strip().lower() in seen:
            raise lc.InputError(f"{name}: {label!r} is listed twice")
        seen.add(label.strip().lower())
        alternatives = rule if isinstance(rule, list) else [rule]
        if not alternatives or not all(isinstance(a, str) and a for a in alternatives):
            raise lc.InputError(
                f"{name}: {label!r} must map to a regex or a non-empty array of regexes"
            )
        pattern = "|".join(f"(?:{a})" for a in alternatives)
        try:
            rules[label.strip()] = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise lc.InputError(f"{name}: {label!r} is not a valid regex: {exc}") from exc
    return rules


def matching_labels(
    title: str,
    rules: dict[str, re.Pattern[str]],
    is_pull_request: bool = False,
    skip_on_pull_requests: frozenset[str] = frozenset(),
) -> list[str]:
    """Return the labels whose rule matches *title*, in the rules' order.

    On a pull request, labels in *skip_on_pull_requests* (lowercased) are left out.
    """
    labels = [label for label, pattern in rules.items() if pattern.search(title)]
    if is_pull_request:
        return [label for label in labels if label.lower() not in skip_on_pull_requests]
    return labels


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    issue_number_str = os.environ.get("ISSUE_NUMBER", "")
    title = os.environ.get("ISSUE_TITLE", "")

    for name, value in (
        ("GITHUB_TOKEN", token),
        ("GITHUB_REPOSITORY", repo),
        ("ISSUE_NUMBER", issue_number_str),
    ):
        if not value:
            print(f"Error: {name} not set.", file=sys.stderr)
            return 1

    try:
        issue_number = int(issue_number_str)
    except ValueError:
        print(f"Error: ISSUE_NUMBER is not a valid integer: {issue_number_str!r}", file=sys.stderr)
        return 1

    try:
        rules = parse_rules(os.environ.get("RULES", ""))
        skip = lc.parse_label_list(
            "SKIP_ON_PULL_REQUESTS", os.environ.get("SKIP_ON_PULL_REQUESTS", "")
        )
    except lc.InputError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # Defaulting to False fails open: a caller that leaves the variable out
    # labels a pull request from its title as before, and the file signal
    # removes any skipped label again on the next push.
    is_pull_request = os.environ.get("IS_PULL_REQUEST", "").strip().lower() == "true"

    labels = matching_labels(title, rules, is_pull_request, skip)
    if not labels:
        print(f"No label rules matched title {title!r} on #{issue_number}--nothing to do.")
        return 0

    try:
        lc.add_labels(repo, issue_number, labels, token)
        print(f"Applied labels {labels} to #{issue_number}.")
    except urllib.error.HTTPError as exc:
        print(f"Error applying labels {labels} to #{issue_number}: {exc}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"Network error applying labels {labels} to #{issue_number}: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
