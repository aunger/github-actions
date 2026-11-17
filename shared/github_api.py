#!/usr/bin/env python3
"""github_api.py: One call to the GitHub REST API, or one page of a list.

The scripts in this repository that call the API go through `gh_api`, so the
headers, the timeout and the handling of a missing token are the same for all
of them.

This module has no `main`; it is imported, not run directly.
"""

import json
import re

from github_headers import github_headers
from https_only import urlopen_https

API_ROOT = "https://api.github.com/"

# Seconds before a call gives up. Without one, a call that hangs holds the job
# until the runner's own limit.
TIMEOUT_SECONDS = 30

# A Link header entry for the next page, e.g. `<https://...&page=3>; rel="next"`.
NEXT_LINK = re.compile(r'<[^>]*>\s*;\s*rel="next"')


def gh_api(
    path: str, token: str, method: str = "GET", body: object = None, read_response: bool = True
) -> object:
    """Call the API at *path* (relative to API_ROOT) and return the parsed JSON.

    An empty *token* sends no Authorization header, for the calls that work
    unauthenticated. A *body* is sent as JSON. An empty response returns None.
    Raises ``urllib.error.HTTPError`` or ``urllib.error.URLError`` on failure,
    so callers handle errors explicitly.

    With *read_response* false the response body is neither read nor parsed,
    and None is returned once the status is 2xx: for a write whose echoed
    object the caller discards, so a cut-off or garbled echo cannot turn an
    applied write into an error. A GET has nothing but its response, so
    asking for one unread raises ValueError.
    """
    if method == "GET" and not read_response:
        raise ValueError("read_response=False is for writes, not for a GET")
    return _call(path, token, method, body, read_response)[0]


def gh_api_page(path: str, token: str) -> tuple[object, bool]:
    """GET one page of a list at *path*; return it and whether a next page exists.

    Whether there is a next page is read from the response's Link header,
    which GitHub sends on every paginated list, so a caller neither guesses
    from the page's length nor asks for an empty page to find the end.
    Raises as gh_api does.
    """
    data, link = _call(path, token, "GET", None)
    return data, bool(NEXT_LINK.search(link))


def _call(
    path: str, token: str, method: str, body: object, read_response: bool = True
) -> tuple[object, str]:
    """Make the call; return the parsed JSON and the Link header ("" if none)."""
    headers = github_headers(token)
    if not token:
        del headers["Authorization"]
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    with urlopen_https(
        API_ROOT + path, data, headers=headers, method=method, timeout=TIMEOUT_SECONDS
    ) as response:
        if not read_response:
            return None, ""
        raw = response.read()
        link = response.headers.get("Link") or ""
    return (json.loads(raw) if raw else None), link
