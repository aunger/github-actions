#!/usr/bin/env python3
"""Reopen issues whose snooze has run out.

The other half of the snooze-issues action, which closes an issue when one of
the caller's SNOOZE_LABELS is added to it. This script runs on a schedule and,
for every issue still carrying a snooze label, checks whether that label's
days have passed since it was applied. Once they have, the issue is reopened,
the snooze label is removed, and so is every label in REMOVE_ON_WAKE: a caller
lists there the process-state labels a woken issue should not keep, so that it
lands back in plain triage rather than resuming mid-cycle in whatever state it
was closed in.

The time is measured from the most recent "labeled" event naming the issue's
snooze label (the issue events API; see find_label_applied_at), not from the
issue's `updated_at`, which any comment or other label change would also bump
and so would silently extend the snooze. If that event cannot be found, the
issue is left alone rather than guessed at: staying snoozed for one more run is
better than acting on evidence the script does not have. Renaming a snooze
label is what makes an event go missing, and it strands every snooze running
under that label; see find_label_applied_at before renaming one.

Before acting on an issue, wake_issue() reads it again and confirms the snooze
label is still there, and removes labels one at a time by name rather than
PATCHing a computed label array. Both guard against a run that takes long
enough (many issues, event pagination, retries) for a label to change under
it, in particular an escalation to a longer snooze, which replaces the shorter
one through mutual-exclusion enforcement and must not be undone by a wake
decided before it.

Usage:
    python3 wake_snoozed_issues.py

Exit code:
    0  always once the tables are valid: API failures are logged but do not
       fail the run, as with archive-stale-test-failures. Also 0, with a
       warning, when the token or repository is missing.
    1  SNOOZE_LABELS or REMOVE_ON_WAKE is invalid.

Required environment variables:
    GITHUB_TOKEN        Token with issues: write
    GITHUB_REPOSITORY   Owner/repo (e.g. "octocat/hello-world")
    SNOOZE_LABELS       JSON object, snooze label: days

Optional environment variables:
    REMOVE_ON_WAKE      JSON array of labels removed from a woken issue too
"""

import datetime
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))

import label_common as lc

# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def fetch_issues_with_label(repo: str, token: str, label: str) -> list[dict]:
    """Return every issue (not pull request) carrying *label*, open or closed.

    Paginates through all pages automatically. Pull requests are excluded:
    GitHub's issues-list endpoint returns them alongside real issues for any
    matching label, identifiable by the presence of a "pull_request" key, and
    snooze labels are scoped to issues (see the snooze-issues action).
    """
    encoded = urllib.parse.quote(label, safe="")
    issues: list[dict] = []
    page = 1
    while True:
        batch = lc.gh_api(
            f"repos/{repo}/issues?labels={encoded}&state=all&per_page=100&page={page}",
            token=token,
        )
        if not batch:
            break
        issues.extend(item for item in batch if "pull_request" not in item)
        page += 1
    return issues


def find_label_applied_at(
    issue_number: int,
    label: str,
    repo: str,
    token: str,
) -> datetime.datetime | None:
    """Return when *label* was last applied to *issue_number*, or None.

    Reads the issue events API for "labeled" events naming *label*
    (case-insensitively) and returns the latest one's timestamp. Returns None
    if no such event is found.

    Applying a label always records an event, but under whatever name the
    label carried at the time, and GitHub does not rewrite those events when
    the label is later renamed. Renaming a snooze label therefore orphans
    every snooze running under it: the issue comes out of the rename wearing
    the new name while its event still names the old one, so nothing here can
    date it, and main() leaves an issue it cannot date snoozed--closed
    indefinitely, announced only by one stderr line per daily run.

    So do not rename a snooze label while any issue is snoozed under it. If
    one must be renamed, then afterwards re-apply the new label to every issue
    still snoozed under it (remove it, then add it back), which records a
    fresh event under the current name and re-dates that snooze from the day
    of the re-application.
    """
    wanted = label.lower()
    latest: datetime.datetime | None = None
    page = 1
    while True:
        batch = lc.gh_api(
            f"repos/{repo}/issues/{issue_number}/events?per_page=100&page={page}",
            token=token,
        )
        if not batch:
            break
        for event in batch:
            if event.get("event") != "labeled":
                continue
            event_label = (event.get("label") or {}).get("name", "")
            if event_label.lower() != wanted:
                continue
            created_at = datetime.datetime.fromisoformat(event["created_at"])
            if latest is None or created_at > latest:
                latest = created_at
        if len(batch) < 100:
            break
        page += 1
    return latest


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def wake_issue(
    issue_number: int,
    snooze_label: str,
    repo: str,
    token: str,
    remove_on_wake: frozenset[str] = frozenset(),
) -> bool:
    """Reopen *issue_number* if it is still snoozed under *snooze_label*.

    Re-fetches the issue's current labels immediately before acting, rather
    than reusing the list-call snapshot main() saw when it decided this
    issue's snooze had elapsed, and removes only the specific labels this
    function targets (lc.remove_labels(), one DELETE per label, the snooze
    label and any in *remove_on_wake*, lowercased) instead of PATCHing a
    computed full label array.
    Together these mean a label change made after main()'s snapshot--most
    importantly someone escalating to a longer snooze, which replaces
    *snooze_label* via mutual-exclusion enforcement--is never silently
    reverted: an escalation is caught by the live re-fetch below (the
    now-current label list would no longer contain *snooze_label*, so this
    returns without touching anything), and even a label added in the
    narrower window between this fetch and the DELETE calls survives, since
    remove_labels() only ever removes the labels it is explicitly told to.

    An issue that is no longer closed as not planned (reopened by hand, or
    closed as completed) is not woken: only its spent snooze label is removed.

    Returns True if the issue was woken (reopened and stripped), False if it
    turned out to no longer be snoozed under *snooze_label*. Raises
    RuntimeError if the snooze label could not be removed, so the caller logs
    a failure, and no comment is posted.
    """
    issue = lc.gh_api(f"repos/{repo}/issues/{issue_number}", token=token)
    if not isinstance(issue, dict):
        raise RuntimeError(f"unexpected response fetching issue #{issue_number}: {issue!r}")

    snooze_lower = snooze_label.lower()
    current_labels = [lbl["name"] for lbl in issue.get("labels", [])]
    if snooze_lower not in {lbl.lower() for lbl in current_labels}:
        print(
            f"#{issue_number} no longer carries '{snooze_label}' (label changed since "
            "this run started)--leaving it alone."
        )
        return False

    snooze_name = next(lbl for lbl in current_labels if lbl.lower() == snooze_lower)

    # Someone reopened the issue by hand, or closed it another way, with the
    # label still on: it is no longer waiting out the snooze, so only the
    # spent label comes off, and its live state and labels are left alone.
    if issue.get("state") != "closed" or issue.get("state_reason") != "not_planned":
        print(
            f"#{issue_number} is {issue.get('state')} "
            f"({issue.get('state_reason') or 'no reason'}), not snoozed--removing only "
            f"its spent '{snooze_label}' label."
        )
        if not lc.remove_labels(issue_number, [snooze_name], repo, token, reason="expired-snooze"):
            raise RuntimeError(f"could not remove '{snooze_name}' from #{issue_number}")
        return False

    others = [lbl for lbl in current_labels if lbl.lower() in remove_on_wake]

    # Reopening the issue's state never touches its labels at all--no PATCH
    # body key for labels is sent here, so there is nothing for this call to
    # clobber even if the label list changes again immediately afterward.
    lc.gh_api(
        f"repos/{repo}/issues/{issue_number}",
        token=token,
        method="PATCH",
        body={"state": "open"},
        read_response=False,
    )
    # The snooze label goes first and alone: while it stays, every run would
    # wake the issue again and post another comment. Left on an open issue,
    # the next run removes it by the branch above, with no comment.
    if not lc.remove_labels(issue_number, [snooze_name], repo, token, reason="expired-snooze"):
        raise RuntimeError(f"reopened #{issue_number} but could not remove '{snooze_name}'")
    if others and not lc.remove_labels(issue_number, others, repo, token, reason="expired-snooze"):
        print(f"#{issue_number}: some of {others} could not be removed.", file=sys.stderr)

    other_stripped = sorted(others)
    stripped_note = ""
    if other_stripped:
        stripped_note = f" Also removed label(s): {', '.join(other_stripped)}."

    try:
        lc.gh_api(
            f"repos/{repo}/issues/{issue_number}/comments",
            token=token,
            method="POST",
            body={
                "body": (
                    f"The `{snooze_label}` snooze has elapsed. Reopening and returning this "
                    f"issue to triage.{stripped_note}"
                )
            },
        )
    except Exception as exc:  # noqa: BLE001
        # The reopen and label strip above already succeeded and are not
        # undone by a failed notification comment; report this narrowly so
        # the Actions log does not read this issue as untouched.
        print(
            f"#{issue_number} was reopened and stripped of its snooze, but posting the "
            f"wake comment failed: {exc}",
            file=sys.stderr,
        )

    print(f"Woke #{issue_number} from its '{snooze_label}' snooze.")
    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")

    if not token:
        print("Warning: GITHUB_TOKEN not set; skipping wake check.", file=sys.stderr)
        return 0
    if not repo:
        print("Warning: GITHUB_REPOSITORY not set; skipping wake check.", file=sys.stderr)
        return 0

    try:
        snooze_labels = lc.parse_snooze_labels("SNOOZE_LABELS", os.environ.get("SNOOZE_LABELS", ""))
        remove_on_wake = lc.parse_label_list("REMOVE_ON_WAKE", os.environ.get("REMOVE_ON_WAKE", ""))
    except lc.InputError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    now = datetime.datetime.now(datetime.UTC)
    woken = 0

    # Shortest snooze first, then by name, so the log reads in a stable order.
    for snooze_label, snooze_days in sorted(snooze_labels.items(), key=lambda kv: (kv[1], kv[0])):
        # A label that cannot be listed is skipped rather than allowed to fail
        # the run, so a blip on one label does not cost the others their turn.
        try:
            snoozed = fetch_issues_with_label(repo, token, snooze_label)
        except Exception as exc:  # noqa: BLE001
            print(f"Error fetching issues snoozed with '{snooze_label}': {exc}", file=sys.stderr)
            continue

        cutoff = now - datetime.timedelta(days=snooze_days)

        for issue_number in sorted(issue["number"] for issue in snoozed):
            try:
                applied_at = find_label_applied_at(issue_number, snooze_label, repo, token)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"Error reading label history for #{issue_number}: {exc}",
                    file=sys.stderr,
                )
                continue

            if applied_at is None:
                print(
                    f"Could not find when '{snooze_label}' was applied to #{issue_number}; "
                    "leaving it snoozed.",
                    file=sys.stderr,
                )
                continue

            if applied_at > cutoff:
                continue  # still within the snooze period

            try:
                if wake_issue(issue_number, snooze_label, repo, token, remove_on_wake):
                    woken += 1
            except Exception as exc:  # noqa: BLE001
                print(f"Error waking #{issue_number}: {exc}", file=sys.stderr)

    print(f"Done. Woke {woken} issue(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
