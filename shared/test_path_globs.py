#!/usr/bin/env python3
"""Unit tests for path_globs.py.

dependency-audit's tests cover the lock globs; these cover the shapes the
label-by-files action's rules use: `**` at either end of a glob.
"""

import fnmatch
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from path_globs import glob_regex


class GlobRegexTest(unittest.TestCase):
    def check(self, pattern: str, matching: list[str], other: list[str]) -> None:
        regex = glob_regex(pattern)
        for path in matching:
            with self.subTest(pattern=pattern, path=path):
                self.assertTrue(regex.match(path))
        for path in other:
            with self.subTest(pattern=pattern, path=path):
                self.assertFalse(regex.match(path))

    def test_leading_double_star_slash_matches_no_directory_or_any_number(self):
        self.check(
            "**/CLAUDE.md",
            ["CLAUDE.md", "a/CLAUDE.md", "a/b/c/CLAUDE.md"],
            ["xCLAUDE.md", "a/CLAUDE.md.bak", "a/claude.md"],
        )

    def test_trailing_double_star_matches_everything_below_a_directory(self):
        self.check(
            ".github/workflows/**",
            [".github/workflows/a.yml", ".github/workflows/sub/b.yml"],
            [".github/workflowsx/a.yml", "x/.github/workflows/a.yml", ".github/workflows"],
        )

    def test_double_star_at_both_ends_names_a_directory_at_any_depth(self):
        self.check(
            "**/test/**",
            ["test/a.py", "src/test/java/A.kt", "a/b/test/c"],
            ["test", "src/test", "src/tests/a.py", "src/mytest/a.py"],
        )

    def test_a_class_takes_ranges(self):
        self.check("docs/[a-z]*.md", ["docs/readme.md"], ["docs/Readme.md", "docs/-x.md"])
        self.check("v[0-9].txt", ["v5.txt"], ["vx.txt"])

    def test_a_leading_bang_negates_a_class(self):
        self.check("[!a-c]", ["d", "-"], ["a", "b"])

    def test_a_bracket_right_after_the_opening_is_a_member(self):
        self.check("[]a]", ["]", "a"], ["b"])
        self.check("[!]a]", ["b"], ["]", "a"])

    def test_a_class_never_matches_a_slash(self):
        self.check("a[!x]b", ["a-b"], ["a/b"])

    def test_an_unclosed_bracket_is_literal(self):
        self.check("a[b", ["a[b"], ["ab"])
        self.check("[!]", ["[!]"], ["a"])

    def test_a_caret_in_a_class_is_literal(self):
        self.check("[^a]", ["^", "a"], ["b"])

    def test_the_classes_agree_with_fnmatch(self):
        names = ["a", "b", "d", "-", "]", "^", "!", "x\\y", "xay", "&", "[]"]
        for pattern in [
            "[a-c]",
            "[!a-c]",
            "[]a]",
            "[!]a]",
            "[!]",
            "[^a]",
            "x[a\\]y",
            "[a&&b]",
            "[-a]",
        ]:
            regex = glob_regex(pattern)
            for name in names:
                with self.subTest(pattern=pattern, name=name):
                    self.assertEqual(fnmatch.fnmatchcase(name, pattern), bool(regex.match(name)))

    def test_the_whole_path_must_match(self):
        self.check("scripts/ci/**", ["scripts/ci/a.py"], ["other/scripts/ci/a.py"])


if __name__ == "__main__":
    unittest.main()
