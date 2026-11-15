#!/usr/bin/env python3
"""check_privileged_checkouts.py: A privileged pull-request job must not check out pull request code.

Reads every workflow file under a directory (default `.github/workflows`),
prints one line for each violation of the rule below, and exits 1 if there is
any.

The case this came from, in aunger/gallery-button-for-pixel-camera#882: five
label and byline workflows checked out the pull request's own merge ref (the
`actions/checkout` default on a `pull_request` event) and then ran a script out
of that checkout, in a job declaring `issues: write` and `pull-requests: write`.
Each was fixed by pinning `ref:` to the base branch, or by dropping the checkout.
This check keeps that from being undone by a later edit that quietly restores
the default checkout, which would break nothing visible and would pass every
other check in the tree.

What this achieves, and what it does not
----------------------------------------

Read this before concluding that these jobs are now safe against the setting
below, because they are not.

The exposure the case above describes is real but latent.
GitHub hands a fork's `pull_request` run a read-only `GITHUB_TOKEN` no matter
what the workflow's `permissions:` block asks for, so the declared write scopes
never materialize for untrusted code.
Turning on the repository or organization setting "Send write tokens to
workflows from fork pull requests" is what would make them materialize.

Pinning the checkout narrows that exposure.
It does not close it.
On a `pull_request` event the workflow file itself is read from the pull
request's merge ref, not from the base branch, which is exactly why these pins
and this check took effect on the pull request that introduced them.
A fork pull request after that setting was enabled would therefore not need to
touch a script at all.
It could edit the workflow file directly: add a `run:`
step, restore the default `ref:`, or delete the pinning outright.
It could edit the workflow that runs this check too, so that the check reports a clean tree.

What the pins buy is that such an attack has to appear in a workflow diff,
where it gets read, rather than in a script buried somewhere in the tree,
and that the rule is written down and enforced rather than assumed.
That is worth having.
It is not a security boundary, and nobody should enable that setting on the
strength of it.

The real boundary is a workflow triggered by `workflow_run`: a workflow file
must live on the base branch to run at all, so what executes there is always
the reviewed file on the default branch, never anything from a pull request
branch.
No `pull_request`-triggered job has that property, which is what makes that
split a boundary and this one a speed bump.

The rule enforced here
----------------------

    A job that a `pull_request` (or `pull_request_target`) event can trigger,
    and whose effective permissions grant any write scope other than
    `security-events`, must pin the `ref:` of every `actions/checkout` step to
    a ref the pull request does not control.

`security-events: write` is the one exemption, because a CodeQL workflow exists
precisely to analyze the pull request's code and has to check out the merge
ref to do it.
A job that checks out `pull_request.head.sha` on purpose needs no exemption if
it holds `contents: read` only, which is the correct shape for running
untrusted code.

Every other write scope counts, not only the `issues` and `pull-requests` that
the case above happened to involve.
`contents: write` over pull-request-controlled code is a full compromise, and
`actions: write`, `packages: write`, `deployments: write` and `id-token: write`
are each at least as serious as `issues: write`.
The point of the wider rule is that adding such a job later trips this
check instead of passing it in silence.

A job that declares no permissions at all, at either the workflow or the job
level, is treated as privileged.
Its token then carries whatever the repository's default workflow permissions
happen to be, which this check cannot read and which can include write scopes,
so an explicit declaration is required before the job is judged.

The accepted refs are listed exactly, in ACCEPTED_REFS, rather than matched by
a pattern.
A pattern would have to decide what "not controlled by the pull request" means
for an arbitrary expression, and the subtle cases are the ones that matter:
`github.sha` alone is the merge ref on a `pull_request` event and so is
pull-request-controlled, while `... || github.sha` is safe, because the
fallback is only reached on the events that carry no pull request context.
Adding a genuinely new form should be a deliberate edit to that list.

What this check does not inspect
--------------------------------

Only `actions/checkout` steps.
A privileged job can still reach pull request code with `gh pr checkout`, a
plain `git fetch origin pull/N/head`, a local composite action, or an artifact
downloaded from an untrusted run, and nothing here would notice.
Given the section above, that is a limit worth stating rather than a hole worth
plugging: the workflow file is pull-request-controlled anyway, so this check
raises the cost of a quiet regression rather than making one impossible.
"""

import argparse
import sys
from pathlib import Path

import yaml

# Event types that let a pull request's own head branch supply the code a job
# runs. `pull_request_target` is included so that introducing it does not
# silently escape this rule; it is more dangerous than `pull_request`, not less.
PULL_REQUEST_TRIGGERS = frozenset({"pull_request", "pull_request_target"})

# The only write scope a pull-request-triggerable job may hold while checking
# out pull request code. Every other write scope makes the job privileged for
# the purposes of this guard. See the module docstring.
EXEMPT_WRITE_SCOPES = frozenset({"security-events"})

# The `ref:` expressions a privileged job may check out.
ACCEPTED_REFS = frozenset(
    {
        "${{ github.event.pull_request.base.sha }}",
        "${{ github.event.pull_request.base.sha || github.sha }}",
    }
)

# The action whose `ref:` this guard inspects, matched on the part before `@`
# so that a hypothetical `actions/checkout-something` is not mistaken for it.
CHECKOUT_ACTION = "actions/checkout"


def triggers(workflow: dict) -> frozenset[str]:
    """Return the event names a workflow is triggered by.

    YAML 1.1 resolves the bare key `on` to the boolean True, which is what
    PyYAML hands back, so the key is looked up both ways.
    """
    on = workflow.get("on", workflow.get(True))
    if isinstance(on, str):
        return frozenset({on})
    if isinstance(on, (dict, list)):
        return frozenset(on)
    return frozenset()


def privileged_write_scopes(permissions) -> frozenset[str] | None:
    """Return the non-exempt write scopes a permissions value grants.

    `permissions` is a job's effective block: a mapping of scope to level, or
    one of the shorthand strings. None is returned for a job that declared no
    permissions at all, meaning the scopes are unknowable from the tree and the
    caller must treat the job as privileged.
    """
    if permissions is None:
        return None
    if permissions == "write-all":
        return frozenset({"write-all"})
    if isinstance(permissions, str):
        # "read-all", or any other shorthand that grants no write scope.
        return frozenset()
    return frozenset(
        scope
        for scope, level in permissions.items()
        if level == "write" and scope not in EXEMPT_WRITE_SCOPES
    )


def checkout_refs(job: dict) -> list:
    """Return the `ref:` input of each actions/checkout step in a job.

    None marks a checkout step that sets no ref, which is the unpinned default
    this check exists to catch.
    """
    refs = []
    for step in job.get("steps") or []:
        uses = step.get("uses")
        if not isinstance(uses, str) or uses.split("@", 1)[0] != CHECKOUT_ACTION:
            continue
        ref = (step.get("with") or {}).get("ref")
        refs.append(ref.strip() if isinstance(ref, str) else ref)
    return refs


def violations(workflow: dict) -> list[str]:
    """Return a message for each privileged job checking out unpinned code."""
    if not triggers(workflow) & PULL_REQUEST_TRIGGERS:
        return []

    found: list[str] = []
    workflow_permissions = workflow.get("permissions")
    for job_name, job in (workflow.get("jobs") or {}).items():
        permissions = job.get("permissions", workflow_permissions)
        scopes = privileged_write_scopes(permissions)
        if scopes is None:
            held = "no declared"
        elif scopes:
            held = ", ".join(sorted(scopes))
        else:
            continue
        for ref in checkout_refs(job):
            if ref in ACCEPTED_REFS:
                continue
            shown = "no ref (the pull request's merge ref)" if ref is None else ref
            found.append(
                f"job {job_name!r} holds {held} write permissions and checks out "
                f"{shown}; pin it to one of {sorted(ACCEPTED_REFS)}"
            )
    return found


def workflow_files(directory: Path) -> list[Path]:
    """Return every workflow file in *directory*, sorted.

    Both extensions: GitHub accepts either, and a check that read one would pass
    a workflow it never opened.
    """
    return sorted([*directory.glob("*.yml"), *directory.glob("*.yaml")])


def check(directory: Path) -> list[str]:
    """Return a message for every violation in the workflow files in *directory*."""
    found: list[str] = []
    for path in workflow_files(directory):
        with path.open(encoding="utf-8") as handle:
            workflow = yaml.safe_load(handle) or {}
        found.extend(f"{path}: {message}" for message in violations(workflow))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--workflows-dir",
        type=Path,
        default=Path(".github/workflows"),
        help="directory holding the workflow files (default: .github/workflows)",
    )
    args = parser.parse_args(argv)

    if not workflow_files(args.workflows_dir):
        print(f"error: found no workflow files in {args.workflows_dir}", file=sys.stderr)
        return 2
    found = check(args.workflows_dir)
    for message in found:
        print(message)
    if found:
        print(f"\n{len(found)} privileged job(s) check out pull request code.", file=sys.stderr)
        return 1
    print("No privileged pull-request job checks out pull request code.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
