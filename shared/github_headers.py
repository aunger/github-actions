#!/usr/bin/env python3
"""github_headers.py: Shared GitHub REST API request headers.

The scripts in this repository that call the GitHub REST API authenticate the
same way. This module holds that one shared helper so each script imports it
instead of carrying its own byte-identical copy.

This module has no `main`; it is imported, not run directly.
aunger/dot-and-tilde's github-supplement plugin carries two copies, in
link_gh_issues.py and verify_github_write.py, so that it imports nothing from
this repository.
A change here is made there in the same pass: it lands in dot-and-tilde first,
then here, together with the bump of the commit shared/test_plugin_copies.py
compares against to one that carries it.
"""


def github_headers(token: str) -> dict[str, str]:
    """Return the standard header set for an authenticated GitHub REST API call."""
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
