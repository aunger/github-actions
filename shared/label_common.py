#!/usr/bin/env python3
"""label_common.py: What the label actions share.

- `gh_api`: one call through `github_api.gh_api`, retried with backoff on a
  transient failure.
- `fetch_open_pull_requests`: every open pull request, page by page.
- `remove_labels`: one DELETE per label, a 404 counting as already gone.
- The parsers for the JSON tables a caller passes as `with:` inputs, which
  raise `InputError` with the input's name on a malformed value.
- `ExclusiveRules`: mutually exclusive label sets and prefix groups, and which
  of an item's labels a newly added label conflicts with.
- `write_output`: one step output, for the actions that report one.
- `format_categories`: the JSON report the command-line tools print.

Label names are compared case-insensitively throughout, as GitHub compares
them, so every parser lowercases what it returns unless it says otherwise.

This module has no `main`; it is imported, not run directly.
"""

import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
from dataclasses import dataclass, field

from github_api import gh_api as _gh_api_once

# ---------------------------------------------------------------------------
# GitHub access
# ---------------------------------------------------------------------------

# A GitHub API blip (a 5xx, a 429, or a network-level URLError) may clear on a
# repeat, so it is retried with exponential backoff before the call fails. A
# real error (any other 4xx, such as a 404 already-gone or a 422 validation
# failure) is never retried: repeating it would not change the outcome.
MAX_ATTEMPTS = 4
INITIAL_BACKOFF_SECONDS = 1.0
BACKOFF_MULTIPLIER = 2.0


def is_transient_error(exc: Exception) -> bool:
    """Return True if *exc* is a GitHub API failure a repeat may clear.

    ``HTTPError`` is a subclass of ``URLError``, so it is checked first.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or 500 <= exc.code <= 599
    return isinstance(exc, urllib.error.URLError)


def gh_api(
    path: str,
    token: str,
    method: str = "GET",
    body: object = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> object:
    """Call the API at *path* and return the parsed JSON (None for an empty body).

    A transient failure (see `is_transient_error`) is retried up to
    *max_attempts* times in all. Once they are spent, or on any other error,
    raises ``urllib.error.HTTPError`` or ``urllib.error.URLError``.

    urllib wraps only a failure to send the request in URLError. A failure
    while reading the answer (a read timeout, a dropped connection) comes out
    as a bare OSError or http.client.HTTPException, so it is wrapped in a
    URLError here, for the callers' handlers. The request may have been
    applied by then, so it is retried only for a method a repeat cannot
    duplicate: not a POST, which could post a comment twice.
    """
    backoff = INITIAL_BACKOFF_SECONDS
    for attempt in range(1, max_attempts + 1):
        read_failed = False
        try:
            try:
                return _gh_api_once(path, token, method=method, body=body)
            except urllib.error.URLError:
                raise
            except (OSError, http.client.HTTPException) as exc:
                read_failed = True
                raise urllib.error.URLError(exc) from exc
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            last = attempt >= max_attempts
            if last or not is_transient_error(exc) or (read_failed and method == "POST"):
                raise
            print(
                f"Transient error on {method} {path} "
                f"(attempt {attempt}/{max_attempts}): {exc}. "
                f"Retrying in {backoff:g}s.",
                file=sys.stderr,
            )
            time.sleep(backoff)
            backoff *= BACKOFF_MULTIPLIER
    raise AssertionError("gh_api retry loop exited without returning or raising")


# GitHub caps a page of pull requests at 100, so a short page is the last page.
PULLS_PER_PAGE = 100


def fetch_open_pull_requests(repo: str, token: str) -> list[dict]:
    """Return every open pull request in *repo*, following pagination.

    The list endpoint filters on nothing finer than state, base branch and head
    branch name, so a caller wanting a subset filters the result itself.
    """
    pulls: list[dict] = []
    page = 1
    while True:
        batch = gh_api(
            f"repos/{repo}/pulls?state=open&per_page={PULLS_PER_PAGE}&page={page}",
            token=token,
        )
        if not batch:
            break
        pulls.extend(batch)
        if len(batch) < PULLS_PER_PAGE:
            break
        page += 1
    return pulls


def fetch_label_names(repo: str, number: int, token: str) -> list[str]:
    """Return the label names issue or pull request *number* carries now.

    Raises ``RuntimeError`` if the response is not an object, and the urllib
    errors on a failed request.
    """
    item = gh_api(f"repos/{repo}/issues/{number}", token=token)
    if not isinstance(item, dict):
        raise RuntimeError(f"unexpected response fetching #{number}: {item!r}")
    return [label["name"] for label in item.get("labels", [])]


def add_labels(repo: str, number: int, labels: list[str], token: str) -> None:
    """Add *labels* to issue or pull request *number*; raises as gh_api does."""
    gh_api(
        f"repos/{repo}/issues/{number}/labels",
        token=token,
        method="POST",
        body={"labels": labels},
    )


def remove_labels(
    number: int,
    to_remove: list[str],
    repo: str,
    token: str,
    reason: str = "conflicting",
) -> bool:
    """Remove each of *to_remove* from issue or pull request *number*, logging each.

    *reason* goes into the log line ("Removed {reason} label '<name>' from #N."),
    so each caller says what kind of removal it made.

    Returns True if every removal succeeded or found the label already gone (a
    404), False if any failed otherwise.
    """
    all_succeeded = True
    for label in to_remove:
        encoded = urllib.parse.quote(label, safe="")
        try:
            gh_api(
                f"repos/{repo}/issues/{number}/labels/{encoded}",
                token=token,
                method="DELETE",
            )
            print(f"Removed {reason} label '{label}' from #{number}.")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                print(
                    f"Label '{label}' not found on #{number} (already removed?).",
                    file=sys.stderr,
                )
            else:
                print(f"Error removing label '{label}' from #{number}: {exc}", file=sys.stderr)
                all_succeeded = False
        except urllib.error.URLError as exc:
            print(
                f"Network error removing label '{label}' from #{number}: {exc}",
                file=sys.stderr,
            )
            all_succeeded = False
    return all_succeeded


def write_output(name: str, value: str) -> None:
    """Append `name=value` to the step's GITHUB_OUTPUT file; nothing outside Actions."""
    output_path = os.environ.get("GITHUB_OUTPUT", "")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as f:
        f.write(f"{name}={value}\n")


def format_categories(categories: list[tuple[str, dict[str, list[int]]]]) -> str:
    """Render named label-to-numbers *categories* as JSON.

    Minimized except for a newline after each `"category":` and after each
    label's number list. The command-line tools beside the label actions
    (backfill_labels_by_title.py, audit_labels_by_files.py) print their reports
    with it, so the reports have one shape.
    """
    category_parts = []
    for name, labels in categories:
        label_parts = [
            f"{json.dumps(label)}:{json.dumps(sorted(labels[label]), separators=(',', ':'))}\n"
            for label in sorted(labels)
        ]
        category_parts.append(f"{json.dumps(name)}:\n{{{','.join(label_parts)}}}")
    return "{" + ",".join(category_parts) + "}"


# ---------------------------------------------------------------------------
# Table inputs
# ---------------------------------------------------------------------------


class InputError(ValueError):
    """A table input that is not valid JSON of the shape its action expects."""


def load_json(name: str, text: str, default: object = None) -> object:
    """Parse input *name*'s JSON *text*; blank text gives *default*.

    With *default* None, blank text is an error, since the input is required.
    """
    if not text.strip():
        if default is None:
            raise InputError(f"{name} is required and was not given")
        return default

    def no_repeats(pairs: list[tuple[str, object]]) -> dict:
        # json.loads keeps only the last of a repeated key, which would hide a
        # table entry the caller wrote.
        result: dict = {}
        for key, value in pairs:
            if key in result:
                raise InputError(f"{name}: {key!r} is listed twice")
            result[key] = value
        return result

    try:
        return json.loads(text, object_pairs_hook=no_repeats)
    except InputError:
        raise
    except ValueError as exc:
        raise InputError(f"{name} is not valid JSON ({exc}): {text!r}") from exc


def _label_name(name: str, value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InputError(f"{name}: {where} must be a non-empty label name, not {value!r}")
    return value.strip().lower()


def parse_label_list(name: str, text: str, required: bool = False) -> frozenset[str]:
    """Parse a JSON array of label names into a lowercased set.

    Blank *text* is an empty set, unless *required*.
    """
    data = load_json(name, text, default=None if required else [])
    if not isinstance(data, list):
        raise InputError(f"{name} must be a JSON array of label names, not {data!r}")
    labels = frozenset(_label_name(name, item, "each entry") for item in data)
    if required and not labels:
        raise InputError(f"{name} must name at least one label")
    return labels


def parse_label_sets(name: str, text: str) -> list[frozenset[str]]:
    """Parse a JSON array of arrays of label names; blank *text* is no sets.

    A set of fewer than two labels excludes nothing, so it is refused as a
    likely mistake.
    """
    data = load_json(name, text, default=[])
    if not isinstance(data, list):
        raise InputError(f"{name} must be a JSON array of arrays of label names, not {data!r}")
    sets = []
    for entry in data:
        if not isinstance(entry, list):
            raise InputError(f"{name}: each set must be an array of label names, not {entry!r}")
        members = frozenset(_label_name(name, item, "each set member") for item in entry)
        if len(members) < 2:
            raise InputError(f"{name}: a set needs at least two different labels: {entry!r}")
        sets.append(members)
    return sets


def parse_prefixes(name: str, text: str) -> list[str]:
    """Parse a JSON array of label prefixes into a lowercased list; blank is none."""
    data = load_json(name, text, default=[])
    if not isinstance(data, list):
        raise InputError(f"{name} must be a JSON array of prefixes, not {data!r}")
    prefixes = []
    for item in data:
        if not isinstance(item, str) or not item:
            raise InputError(f"{name}: each prefix must be a non-empty string, not {item!r}")
        prefixes.append(item.lower())
    return prefixes


def parse_snooze_labels(name: str, text: str) -> dict[str, int]:
    """Parse a JSON object mapping each snooze label to its days.

    The labels keep the caller's spelling, which the wake action searches by
    and names in its comment; compare them lowercased. Required: the snooze
    actions have nothing to do without one.
    """
    data = load_json(name, text)
    if not isinstance(data, dict) or not data:
        raise InputError(f"{name} must be a non-empty JSON object of label: days, not {data!r}")
    ladder = {}
    for label, days in data.items():
        key = _label_name(name, label, "each key")
        if isinstance(days, bool) or not isinstance(days, int) or days < 1:
            raise InputError(f"{name}: {label!r} must map to a whole number of days, 1 or more")
        if key in {existing.lower() for existing in ladder}:
            raise InputError(f"{name}: {label!r} is listed twice")
        ladder[label.strip()] = days
    return ladder


# ---------------------------------------------------------------------------
# Mutually exclusive labels
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExclusiveRules:
    """Mutually exclusive label sets and prefix groups, all lowercased.

    *sets*: at most one member of each set belongs on an item.
    *prefixes*: at most one label starting with each prefix belongs on an item.
    """

    sets: list[frozenset[str]] = field(default_factory=list)
    prefixes: list[str] = field(default_factory=list)

    @classmethod
    def parse(cls, sets_name: str, sets_text: str, prefixes_name: str, prefixes_text: str):
        return cls(
            parse_label_sets(sets_name, sets_text), parse_prefixes(prefixes_name, prefixes_text)
        )

    def __bool__(self) -> bool:
        return bool(self.sets or self.prefixes)

    def sets_of(self, label: str) -> list[frozenset[str]]:
        """Return every set *label* belongs to."""
        lower = label.lower()
        return [label_set for label_set in self.sets if lower in label_set]

    def prefixes_of(self, label: str) -> list[str]:
        """Return every prefix *label* starts with."""
        lower = label.lower()
        return [prefix for prefix in self.prefixes if lower.startswith(prefix)]

    def governs(self, label: str) -> bool:
        """Return True if *label* is in any set or starts with any prefix."""
        return bool(self.sets_of(label) or self.prefixes_of(label))

    def conflicts(self, added: str, current: list[str]) -> list[str]:
        """Return the labels in *current* that adding *added* excludes, in order.

        A label conflicts when it shares a set or a prefix with *added* and is
        not *added* itself. Each is listed once, under the name *current* gives it.
        """
        added_lower = added.lower()
        sets = self.sets_of(added)
        prefixes = self.prefixes_of(added)
        found = []
        for label in current:
            lower = label.lower()
            if lower == added_lower or label in found:
                continue
            if any(lower in s for s in sets) or any(lower.startswith(p) for p in prefixes):
                found.append(label)
        return found
