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
    ``[...]`` is a character class. (``glob.translate`` does this from Python
    3.13 only.)
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
        elif pattern[i] == "[" and "]" in pattern[i + 2 :]:
            end = pattern.index("]", i + 2)
            body = pattern[i + 1 : end]
            if body.startswith("!"):
                out.append("[^" + re.escape(body[1:]) + "]")
            else:
                out.append("[" + re.escape(body) + "]")
            i = end + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")
