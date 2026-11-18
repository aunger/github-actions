#!/usr/bin/env python3
"""workflow_inputs.py: Read an action's `with:` inputs out of a caller's workflow.

The label actions take their tables as `with:` inputs, so a caller's workflow
file is where its tables live. The command-line tools beside those actions
(`backfill_labels_by_title.py`, `audit_labels_by_files.py`) read the same
tables from there, so a person running one locally passes the workflow file
rather than a second copy of the table.

A step uses the action when its `uses:` is `<owner>/<repo>/<action>@<ref>` for
any owner, repository and ref, or `./<action>`, the way this repository's own
workflows call it.

Reading YAML needs PyYAML, which `requirements.txt` pins; it is imported only
when a workflow is read, so the actions themselves never need it.

This module has no `main`; it is imported, not run directly.
"""

import re
from pathlib import Path


class WorkflowInputError(ValueError):
    """The workflow file has no single step using the action, or it cannot be read."""


def _uses_action(uses: object, action: str) -> bool:
    if not isinstance(uses, str):
        return False
    remote = re.fullmatch(r"[^/@\s]+/[^/@\s]+/(?P<path>[^@\s]+)@\S+", uses.strip())
    if remote:
        return remote.group("path") == action
    return uses.strip().rstrip("/") == f"./{action}"


def action_inputs(workflow: Path, action: str) -> dict[str, str]:
    """Return the `with:` mapping of the one step in *workflow* that uses *action*.

    Raises WorkflowInputError when the file cannot be read or parsed, or when no
    step, or more than one, uses the action. Each value is returned as the
    string the runner would pass.
    """
    try:
        import yaml
    except ModuleNotFoundError as exc:
        raise WorkflowInputError(
            "reading a workflow needs PyYAML; install this repository's requirements.txt"
        ) from exc
    try:
        document = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise WorkflowInputError(f"cannot read {workflow}: {exc}") from exc

    found = []
    jobs = (document or {}).get("jobs") if isinstance(document, dict) else None
    for job in (jobs or {}).values():
        for step in (job or {}).get("steps") or []:
            if isinstance(step, dict) and _uses_action(step.get("uses"), action):
                found.append(step.get("with") or {})
    if len(found) != 1:
        raise WorkflowInputError(
            f"{workflow} has {len(found)} steps using the {action} action; expected one"
        )
    return {str(key): "" if value is None else str(value) for key, value in found[0].items()}
