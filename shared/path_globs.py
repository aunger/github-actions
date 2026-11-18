#!/usr/bin/env python3
"""path_globs.py: Root-relative path globs as regular expressions.

`dependency-audit` matches its lock glob with this, and `label-by-files` its
path rules.

This module has no `main`; it is imported, not run directly.
"""

import re


def glob_regex(pattern: str) -> re.Pattern:
    """Return a regex matching the root-relative, forward-slash paths *pattern* matches.

    The pattern is a ``pathlib`` glob: ``**/`` matches any number of directories,
    none included, ``*`` and ``?`` match within one path component, and
    ``[...]`` is a character class with ranges, negated by a leading ``!``,
    which like ``?`` never matches ``/``. A ``]`` right after the ``[`` or
    ``[!`` is a member, and a ``[`` with no closing ``]`` is literal, as in
    ``fnmatch``. (``glob.translate`` does this from Python 3.13 only.)
    """
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[" and (end := _class_end(pattern, i)) is not None:
            out.append(_class_regex(pattern[i + 1 : end]))
            i = end + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def _class_end(pattern: str, start: int) -> int | None:
    """Return the index of the `]` closing the class opened at *start*, or None."""
    j = start + 1
    if j < len(pattern) and pattern[j] == "!":
        j += 1
    if j < len(pattern) and pattern[j] == "]":
        j += 1
    end = pattern.find("]", j)
    return None if end < 0 else end


def _class_regex(body: str) -> str:
    """Return the regex for a glob class's *body*, the text between its brackets."""
    negate = body.startswith("!")
    if negate:
        body = body[1:]
    # Backslash is literal in a glob; `&`, `~` and `|` doubled, and a nested
    # `[`, are what a future re may read as set operations, as fnmatch notes.
    body = re.sub(r"([\\&~|\[])", r"\\\1", body)
    if negate:
        return f"(?!/)[^{body}]"
    if body.startswith("^"):
        body = "\\" + body
    return f"(?!/)[{body}]"
