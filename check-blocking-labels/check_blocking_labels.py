#!/usr/bin/env python3
"""Fail the merge gate when any open pull request at this head commit carries a blocking label.

Backs a required status check that holds a merge while process state is
outstanding: a caller lists the labels that mean "not yet" as BLOCKING_LABELS,
and this check is red while any open pull request headed by the commit
carries one. The orchestrate plugin of aunger/dot-and-tilde is one such
caller: its Orchestrator applies `orchestrating` for the length of a cycle,
and its label protocol relies on a check like this one to keep the merge box
closed until the label comes off. The README says how the two pair up.

Why this asks about a commit rather than about one pull request: a check run is
stored against a head commit, not against a pull request, and GitHub evaluates
a required check by taking the most recent run of that check name on the
commit, with no notion of which pull request produced it. A check that
answered only "does the pull request that fired this event carry a blocking
label?" therefore let two open pull requests sharing a head commit answer for
each other: the one that fired most recently overwrote the other's verdict.
That is not hypothetical: a test pull request opened from the head of a pull
request under review lands on exactly the commit the gate is holding.

So the question asked here is the one the storage can answer: does *any* open
pull request whose head is this commit carry a blocking label? The answer is
then true for every pull request on the commit, whichever one triggered the
run.

Two consequences of that scoping are deliberate:

  - A pull request can be blocked by a sibling's label. The hold output names
    the offending pull request and label, so the log says whose label it is.
    Blocking is the safe direction: the alternative is a gate that silently
    opens.
  - The base branch is not consulted. A pull request onto another base sharing
    this head commit counts too, because its check run lands on the same
    commit and would otherwise overwrite this verdict.

Only open pull requests count. A closed one cannot be merged, so its labels
must not block anyone. That is a statement about this script's verdict, not
about the gate: what gates a merge is the stored check run, so a failure a
since-closed pull request earned outlives it until something re-runs the
check. That is why the caller also triggers on `closed`. That run excludes the
closing pull request from both of this script's sources by construction (see
main()), rather than trusting the open listing to have caught up with the
close, so it is what retires the verdict that pull request earned.

The triggering pull request's own labels are read from the event payload as
well as from the API listing (see main()). Both are snapshots that go stale in
opposite directions, so unioning them keeps either staleness blocking rather
than opening, and makes this check never weaker about the triggering pull
request than a payload-only check.

One exception narrows the payload, and only by what is known to be gone. A
caller may run the enforce-mutually-exclusive-labels action first in the same
job, and enforcement can remove a blocking label the payload still names
(adding `verified` can remove `verification needed`). Enforcement removes with
GITHUB_TOKEN, which starts no new run, so a verdict that kept counting the
removed label would stay red until some unrelated event. The caller passes the
labels enforcement removed as REMOVED_LABELS, and only those are dropped from
the payload. Every other payload label still counts.

The price of unioning the two sources is a verdict that can name a label the
pull request no longer carries, since a run whose event has been superseded,
or whose listing lagged a label removal, reports the labels of the moment it
read. A new event (a label change, a push) reads both sources afresh and
clears it. Re-running the stale run clears only the listing case: a re-run
replays the original event, so PR_LABELS still holds the payload it was first
given. Over-blocking is the safe direction, so this is the trade taken
deliberately.

Usage:
    python3 check_blocking_labels.py

Exit code:
    0  no open pull request at HEAD_SHA carries a blocking label.
    1  at least one does, or the check could not be evaluated (missing
       configuration, an invalid BLOCKING_LABELS, PR_LABELS or REMOVED_LABELS
       value, or a GitHub API failure). Only a 429, a 5xx or a network error
       is retried, so a single 403, which is how GitHub answers some
       rate-limit and abuse-detection cases, fails the gate on the first
       attempt. Failing closed is the point: a gate that cannot prove the
       merge is safe must not open it.

Required environment variables:
    GITHUB_TOKEN        Token with pull-requests: read
    GITHUB_REPOSITORY   Owner/repo (e.g. "octocat/hello-world")
    HEAD_SHA            github.event.pull_request.head.sha, the commit this
                        check run is stored against
    PR_NUMBER           github.event.pull_request.number
    PR_STATE            github.event.pull_request.state ("open" or "closed")
    PR_LABELS           toJson(github.event.pull_request.labels.*.name)
    BLOCKING_LABELS     JSON array of the labels that hold a merge

Optional environment variables:
    REMOVED_LABELS      JSON array of labels an earlier step removed from the
                        triggering pull request in this run; dropped from
                        PR_LABELS. Unset or empty means none.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))

import label_common as lc

# ---------------------------------------------------------------------------
# Blocking labels
# ---------------------------------------------------------------------------


def blocking_labels_in(labels: list[str], blocking: frozenset[str]) -> list[str]:
    """Return the *blocking* labels among *labels*, lowercased, deduplicated, sorted.

    *blocking* is lowercased, as lc.parse_label_list returns it.
    """
    return sorted({lbl.lower() for lbl in labels} & blocking)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def fetch_open_prs_at_head(repo: str, token: str, head_sha: str) -> list[dict]:
    """Return every open pull request in *repo* whose head commit is *head_sha*.

    Filters the full open-PR listing locally, because no endpoint answers this
    question server-side. The list endpoint's `head` filter takes a branch
    name, and a branch is not what the check run is keyed on. The nearest
    alternative, GET /repos/{owner}/{repo}/commits/{sha}/pulls, matches every
    PR whose branch *contains* the commit rather than the PRs headed by it, so
    it would over-match every descendant PR. In a repository with fewer than
    100 open pull requests the listing is one API call.
    """
    return [
        pr
        for pr in lc.fetch_open_pull_requests(repo, token)
        if (pr.get("head") or {}).get("sha") == head_sha
    ]


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def label_names(pull_request: dict) -> list[str]:
    """Return the label names carried by an API pull request object."""
    return [lbl["name"] for lbl in pull_request.get("labels", [])]


def blocked_prs(
    candidates: dict[int, list[str]], blocking: frozenset[str]
) -> list[tuple[int, list[str]]]:
    """Return (pr number, blocking labels) for each candidate that carries one.

    Ordered by PR number so the hold output is stable.
    """
    blocked = []
    for number in sorted(candidates):
        found = blocking_labels_in(candidates[number], blocking)
        if found:
            blocked.append((number, found))
    return blocked


def json_array(text: str) -> list:
    """Parse *text* as a JSON array; raises ValueError for anything else."""
    value = json.loads(text)
    if not isinstance(value, list):
        raise ValueError(f"expected an array, got {type(value).__name__}")
    return value


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    head_sha = os.environ.get("HEAD_SHA", "")
    pr_number_raw = os.environ.get("PR_NUMBER", "")
    pr_state = os.environ.get("PR_STATE", "")
    pr_labels_raw = os.environ.get("PR_LABELS", "")
    removed_labels_raw = os.environ.get("REMOVED_LABELS", "") or "[]"

    # Unlike the other label actions, missing configuration is fatal rather
    # than a skip: those apply a convenience label, this decides whether a
    # merge may proceed.
    missing = [
        name
        for name, value in (
            ("GITHUB_TOKEN", token),
            ("GITHUB_REPOSITORY", repo),
            ("HEAD_SHA", head_sha),
            ("PR_NUMBER", pr_number_raw),
            ("PR_STATE", pr_state),
            ("PR_LABELS", pr_labels_raw),
        )
        if not value
    ]
    if missing:
        print(
            f"Error: required environment variable(s) not set: {', '.join(missing)}",
            file=sys.stderr,
        )
        return 1

    try:
        blocking = lc.parse_label_list(
            "BLOCKING_LABELS", os.environ.get("BLOCKING_LABELS", ""), required=True
        )
    except lc.InputError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        pr_number = int(pr_number_raw)
    except ValueError:
        print(f"Error: PR_NUMBER is not a valid integer: {pr_number_raw!r}", file=sys.stderr)
        return 1

    try:
        event_labels = [str(name) for name in json_array(pr_labels_raw)]
    except (ValueError, TypeError) as exc:
        print(
            f"Error: PR_LABELS is not a JSON array of names ({exc}): {pr_labels_raw!r}",
            file=sys.stderr,
        )
        return 1

    try:
        removed = {str(name).lower() for name in json_array(removed_labels_raw)}
    except (ValueError, TypeError) as exc:
        print(
            f"Error: REMOVED_LABELS is not a JSON array of names ({exc}): {removed_labels_raw!r}",
            file=sys.stderr,
        )
        return 1
    # Labels enforcement removed earlier in this job are gone, although the
    # payload, captured before enforcement ran, still names them.
    event_labels = [name for name in event_labels if name.lower() not in removed]

    try:
        open_prs = fetch_open_prs_at_head(repo, token, head_sha)
    except Exception as exc:  # noqa: BLE001
        print(f"Error listing open pull requests at {head_sha}: {exc}", file=sys.stderr)
        print(
            "Cannot prove this commit is unblocked, so it is treated as blocked.", file=sys.stderr
        )
        return 1

    # The triggering PR's labels are taken from both sources and unioned rather
    # than either replacing the other, because each source is stale in the
    # direction the other covers. The payload is fixed at event time, so the
    # listing is what catches a label applied after this run's event fired,
    # which a superseded run would otherwise report as a clean commit. The
    # listing is a separate read, so the payload is what catches a listing that
    # has not yet caught up with a PR just opened or just labeled. Blocking on
    # either is also why the verdict is never weaker about the triggering PR
    # than the payload alone would have made it.
    #
    # The union can therefore name a label that is no longer applied: a
    # superseded run landing last, or a listing lagging a removal. That is
    # over-blocking, it is cleared by a new event (a re-run replays the stale
    # payload, so it clears only the listing case), and it is the safe
    # direction; the alternative is a gate that opens on a stale reading.
    #
    # A closed triggering PR is the mirror case, and gets the opposite
    # treatment: it is dropped from both sources, the payload by its state and
    # the listing by its number. It cannot be merged, so its label must not
    # block a sibling that can. Dropping it from the listing too is what makes
    # the workflow's `closed` trigger a reliable recovery rather than a race:
    # that run is the last event the commit will see, so a copy of the listing
    # that has not yet caught up with the close would otherwise leave the
    # failing verdict the closing PR earned as the newest run on the commit,
    # with a sibling blocked by a label nothing carries and no automatic exit.
    # It cannot wrongly unblock, because a payload whose state is stale in the
    # other direction (closed here, reopened since) is followed by a `reopened`
    # event that re-runs this check.
    candidates: dict[int, list[str]] = {}
    if pr_state == "open":
        candidates[pr_number] = list(event_labels)
    for pull_request in open_prs:
        number = pull_request["number"]
        if number == pr_number and pr_state != "open":
            continue
        # REMOVED_LABELS narrows the payload only. A removed label the listing
        # still shows may have been added back since, and the run its event
        # started can finish before this one; landing last, this run must not
        # open the gate on a label that is there. A listing that merely lags
        # over-blocks until the next event, the safe direction.
        candidates.setdefault(number, []).extend(label_names(pull_request))

    listed = ", ".join(f"#{number}" for number in sorted(candidates)) or "none"
    print(f"Open pull request(s) whose head is {head_sha}: {listed}.")

    blocked = blocked_prs(candidates, blocking)
    if not blocked:
        print("OK: No blocking labels are present.")
        return 0

    # Each PR is named by number, the triggering one included: the run is
    # stored on the commit, so a sibling's merge box can show this log, and
    # "this PR" would read as that sibling.
    for number, labels in blocked:
        relation = (
            "whose event started this run"
            if number == pr_number
            else "which shares this head commit"
        )
        blamed = f"PR #{number}, {relation},"
        carried = "a blocking label" if len(labels) == 1 else "blocking labels"
        print(
            f"On hold: {blamed} carried {carried} when this run read its labels:"
            f" {', '.join(labels)}"
        )
    print("Remove each label once its blocking condition is resolved.")
    print(
        "If a label named above is already gone, the reading this run used predates"
        " that label's removal. A re-run replays this run's event payload, so it can name the"
        " same label again; a new event, such as a label change or a push, reads the"
        " labels afresh."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
