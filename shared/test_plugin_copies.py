#!/usr/bin/env python3
"""The github-supplement plugin's copies of the modules here match them.

aunger/dot-and-tilde's github-supplement plugin imports nothing from this
repository, so two of its scripts carry copies of what this directory defines:

- bin/link_gh_issues.py: github_headers(), and https_only.py's
  _after_https_scheme() and https_request();
- scripts/verify_github_write.py (a hook that reads back what was written to
  GitHub): the same three, and session_bylines.py's BYLINE_RE and
  strip_bylines(), which must recognize the bylines the strip-session-bylines
  action removes, or the hook reports a mismatch for every byline stripped.

This test fetches dot-and-tilde at PINNED_COMMIT and fails when a copy and its
original stop being the same code there. A change to one of these modules is
therefore made in dot-and-tilde first, and lands here together with the bump of
PINNED_COMMIT to a commit that carries it; the pull request that makes it here
stays red until both agree. Pinning keeps this repository's CI from changing
with dot-and-tilde's main. Where the fetch fails the test skips with the
reason, except when CI is set, where it fails.

Docstrings and comments are left out of the comparison, which is of the parsed
definitions: each copy's docstring may say where it came from. orchestrate's
ci_monitor.py has its own _after_https_scheme, written in its own style, and is
not compared.
"""

import ast
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
UPSTREAM = "https://github.com/aunger/dot-and-tilde.git"
# A commit on dot-and-tilde's main. Move it only to another commit on main,
# which a merge can not make unreachable.
PINNED_COMMIT = "b8326e01d584f913eee9568c3ba6c94ecaf22c8e"
PLUGIN = "plugins/github-supplement"
GIT_TIMEOUT_SECONDS = 120

HTTPS_HELPERS = {
    "github_headers": "github_headers.py",
    "_after_https_scheme": "https_only.py",
    "https_request": "https_only.py",
}

# path in the plugin -> {top-level name: the module here that defines it}
COPIES = {
    "bin/link_gh_issues.py": HTTPS_HELPERS,
    "scripts/verify_github_write.py": {
        **HTTPS_HELPERS,
        "BYLINE_RE": "session_bylines.py",
        "strip_bylines": "session_bylines.py",
    },
}


class FetchFailed(Exception):
    """dot-and-tilde could not be fetched."""


def fetch(directory: str) -> "callable":
    """Fetch PINNED_COMMIT into a bare repository; return a function reading one file."""
    git = shutil.which("git")
    git_dir = os.path.join(directory, "upstream.git")

    def run(*args: str) -> str:
        # --git-dir, so a GIT_DIR inherited from a git hook cannot redirect it.
        return subprocess.run(
            [git, "--git-dir", git_dir, *args],
            capture_output=True,
            text=True,
            check=True,
            timeout=GIT_TIMEOUT_SECONDS,
        ).stdout

    try:
        if git is None:
            raise FileNotFoundError("git is not on PATH")
        run("init", "-q", "--bare", git_dir)
        run("fetch", "-q", "--depth", "1", UPSTREAM, PINNED_COMMIT)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
        detail = getattr(error, "stderr", None) or error
        raise FetchFailed(f"could not fetch {UPSTREAM} at {PINNED_COMMIT}: {detail}") from error

    def read(path: str) -> str:
        return run("show", f"FETCH_HEAD:{PLUGIN}/{path}")

    return read


def definition_without_docstring(source: str, name: str, where: str) -> str:
    """Return the ast dump of the top-level function or assignment *name*, docstring removed."""
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:]
            return ast.dump(node)
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.dump(node)
    raise AssertionError(f"{where} defines no top-level {name}")


def read_original(module: str) -> str:
    with open(os.path.join(HERE, module), encoding="utf-8") as handle:
        return handle.read()


class TestPluginCopies(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        try:
            cls.read = staticmethod(fetch(cls._directory.name))
        except FetchFailed as error:
            cls._directory.cleanup()
            if os.environ.get("CI"):
                raise
            raise unittest.SkipTest(str(error)) from error

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def test_each_copy_matches_its_original(self):
        for path, names in COPIES.items():
            copy = self.read(path)
            for name, module in names.items():
                with self.subTest(copy=path, name=name):
                    self.assertEqual(
                        definition_without_docstring(copy, name, path),
                        definition_without_docstring(read_original(module), name, module),
                        f"{name} in dot-and-tilde's {PLUGIN}/{path} at {PINNED_COMMIT} "
                        f"differs from shared/{module}; change both in the same pass",
                    )


class TestComparison(unittest.TestCase):
    """The comparison itself, so a check that stopped comparing would not pass quietly."""

    def test_docstrings_are_ignored_and_code_is_not(self):
        a = 'def f():\n    """One."""\n    return 1\n'
        b = 'def f():\n    """Two."""\n    return 1\n'
        c = 'def f():\n    """One."""\n    return 2\n'
        self.assertEqual(definition_without_docstring(a, "f", "a"), definition_without_docstring(b, "f", "b"))
        self.assertNotEqual(definition_without_docstring(a, "f", "a"), definition_without_docstring(c, "f", "c"))

    def test_a_changed_pattern_differs(self):
        a = 'import re\nBYLINE_RE = re.compile(r"x")\n'
        b = 'import re\nBYLINE_RE = re.compile(r"y")\n'
        self.assertNotEqual(
            definition_without_docstring(a, "BYLINE_RE", "a"),
            definition_without_docstring(b, "BYLINE_RE", "b"),
        )

    def test_a_missing_definition_fails(self):
        with self.assertRaises(AssertionError):
            definition_without_docstring("x = 1\n", "BYLINE_RE", "here")


if __name__ == "__main__":
    unittest.main()
