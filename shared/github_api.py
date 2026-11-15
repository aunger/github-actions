#!/usr/bin/env python3
"""github_api.py: One call to the GitHub REST API.

The scripts in this repository that call the API go through `gh_api`, so the
headers, the timeout and the handling of a missing token are the same for all
of them.

This module has no `main`; it is imported, not run directly.
"""

import json

from github_headers import github_headers
from https_only import urlopen_https

API_ROOT = "https://api.github.com/"

# Seconds before a call gives up. Without one, a call that hangs holds the job
# until the runner's own limit.
TIMEOUT_SECONDS = 30


def gh_api(path: str, token: str, method: str = "GET", body: object = None) -> object:
    """Call the API at *path* (relative to API_ROOT) and return the parsed JSON.

    An empty *token* sends no Authorization header, for the calls that work
    unauthenticated. A *body* is sent as JSON. An empty response returns None.
    Raises ``urllib.error.HTTPError`` or ``urllib.error.URLError`` on failure,
    so callers handle errors explicitly.
    """
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
        raw = response.read()
    return json.loads(raw) if raw else None
