#!/usr/bin/env python3
"""Unit tests for label_by_files.py.

RULES is aunger/gallery-button-for-pixel-camera's table, so the path cases from
that repository's own tests keep their assertions; the paths are sample data.
"""

import io
import json
import os
import sys
import unittest
import urllib.error
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import label_by_files as lbf

RULES_JSON = json.dumps(
    {
        "automated tests": {
            "mode": "every-file",
            "paths": [
                "**/test/**",
                "**/androidTest/**",
                "**/sharedTest/**",
                "**/test_?*.py",
                "**/test_?*.sh",
            ],
        },
        "ci": {"mode": "every-file", "paths": [".github/workflows/**", "scripts/ci/**"]},
        "agents": {
            "mode": "any-file",
            "paths": ["**/AGENTS.md", "**/CLAUDE.md", "**/agents/**", ".claude/**"],
        },
    }
)
RULES = lbf.parse_rules(RULES_JSON)


def labels_for_path(path: str) -> frozenset[str]:
    return lbf.labels_for_path(path, RULES)


def matching_labels(paths: list[str]) -> list[str]:
    return lbf.matching_labels(paths, RULES)


def labels_to_remove(paths: list[str]) -> list[str]:
    return lbf.labels_to_remove(paths, RULES)


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url=None, code=code, msg="error", hdrs=None, fp=None)


# ---------------------------------------------------------------------------
# parse_rules
# ---------------------------------------------------------------------------


class TestParseRules(unittest.TestCase):
    def test_parses_each_label_mode_and_globs(self):
        by_label = {rule.label: rule for rule in RULES}
        self.assertEqual(set(by_label), {"automated tests", "ci", "agents"})
        self.assertEqual(by_label["ci"].mode, "every-file")
        self.assertEqual(by_label["agents"].mode, "any-file")
        self.assertEqual(by_label["ci"].globs, (".github/workflows/**", "scripts/ci/**"))

    def test_keeps_the_label_spelling_given(self):
        rules = lbf.parse_rules('{" Docs ": {"mode": "any-file", "paths": ["docs/**"]}}')
        self.assertEqual([rule.label for rule in rules], ["Docs"])

    def test_rejects_malformed_tables(self):
        cases = {
            "blank": "",
            "not JSON": "{",
            "not an object": '["ci"]',
            "empty object": "{}",
            "empty label": '{"": {"mode": "any-file", "paths": ["a"]}}',
            "duplicate label ignoring case": (
                '{"CI": {"mode": "any-file", "paths": ["a"]},'
                ' "ci": {"mode": "any-file", "paths": ["b"]}}'
            ),
            "rule not an object": '{"ci": ["a"]}',
            "bad mode": '{"ci": {"mode": "all", "paths": ["a"]}}',
            "missing mode": '{"ci": {"paths": ["a"]}}',
            "missing paths": '{"ci": {"mode": "any-file"}}',
            "extra key": '{"ci": {"mode": "any-file", "paths": ["a"], "x": 1}}',
            "empty paths": '{"ci": {"mode": "any-file", "paths": []}}',
            "paths not an array": '{"ci": {"mode": "any-file", "paths": "a"}}',
            "empty glob": '{"ci": {"mode": "any-file", "paths": [""]}}',
            "non-string glob": '{"ci": {"mode": "any-file", "paths": [1]}}',
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(lbf.lc.InputError):
                    lbf.parse_rules(text)

    def test_error_names_the_input(self):
        with self.assertRaises(lbf.lc.InputError) as caught:
            lbf.parse_rules("{}", name="rules")
        self.assertIn("rules", str(caught.exception))

    def test_a_glob_that_cannot_compile_is_an_input_error(self):
        # A reversed range is a valid-looking class that no regex accepts.
        text = json.dumps({"x": {"mode": "any-file", "paths": ["docs/[z-a].md"]}})
        with self.assertRaisesRegex(lbf.lc.InputError, "'x' has an invalid glob"):
            lbf.parse_rules(text)

    def test_an_exactly_repeated_label_is_refused(self):
        rule = '{"mode": "any-file", "paths": ["a/**"]}'
        with self.assertRaisesRegex(lbf.lc.InputError, "listed twice"):
            lbf.parse_rules(f'{{"x": {rule}, "x": {rule}}}')


# ---------------------------------------------------------------------------
# Glob semantics the docstring promises
# ---------------------------------------------------------------------------


class TestGlobSemantics(unittest.TestCase):
    def rule(self, glob: str) -> lbf.Rule:
        return lbf.parse_rules(json.dumps({"x": {"mode": "any-file", "paths": [glob]}}))[0]

    def test_directory_glob_does_not_match_a_file_with_that_name(self):
        rule = self.rule("**/test/**")
        self.assertTrue(rule.matches("test/a.py"))
        self.assertTrue(rule.matches("a/b/test/c/d.py"))
        self.assertFalse(rule.matches("a/test"))
        self.assertFalse(rule.matches("test"))

    def test_trailing_double_star_matches_everything_below(self):
        rule = self.rule("scripts/ci/**")
        self.assertTrue(rule.matches("scripts/ci/a.py"))
        self.assertTrue(rule.matches("scripts/ci/labels/deep/a.py"))
        self.assertFalse(rule.matches("scripts/cix/a.py"))
        self.assertFalse(rule.matches("other/scripts/ci/a.py"))

    def test_leading_double_star_slash_matches_at_the_root_too(self):
        rule = self.rule("**/AGENTS.md")
        self.assertTrue(rule.matches("AGENTS.md"))
        self.assertTrue(rule.matches("a/b/AGENTS.md"))
        self.assertFalse(rule.matches("a/xAGENTS.md"))

    def test_single_star_stays_within_a_component(self):
        rule = self.rule("**/test_?*.py")
        self.assertTrue(rule.matches("scripts/test_x.py"))
        self.assertFalse(rule.matches("scripts/test_.py"))
        self.assertFalse(rule.matches("scripts/test_a/b.py"))

    def test_matching_is_case_sensitive(self):
        rule = self.rule("**/AGENTS.md")
        self.assertFalse(rule.matches("agents.md"))
        self.assertFalse(rule.matches("docs/Agents.md"))


# ---------------------------------------------------------------------------
# labels_for_path tests
# ---------------------------------------------------------------------------


class TestLabelsForPath(unittest.TestCase):
    def test_gradle_test_source_sets_give_automated_tests(self):
        for path in (
            "app/src/test/java/com/gb4pc/Foo.kt",
            "app/src/androidTest/java/com/gb4pc/Foo.kt",
            "app/src/sharedTest/java/com/gb4pc/Foo.kt",
            "e2e-mock-gallery/src/test/java/com/gb4pc/Foo.kt",
        ):
            self.assertEqual(labels_for_path(path), frozenset({"automated tests"}), msg=path)

    def test_test_script_basename_gives_automated_tests(self):
        self.assertEqual(
            labels_for_path("scripts/lint/test_check_md040.py"),
            frozenset({"automated tests"}),
        )

    def test_workflow_yaml_gives_ci(self):
        self.assertEqual(labels_for_path(".github/workflows/build.yml"), frozenset({"ci"}))

    def test_ci_script_gives_ci(self):
        for path in (
            "scripts/ci/labels/label_by_title.py",
            "scripts/ci/test-support/dismiss_anr.sh",
        ):
            self.assertEqual(labels_for_path(path), frozenset({"ci"}), msg=path)

    def test_ci_test_script_gives_both_ci_and_automated_tests(self):
        # Two globs of different labels match one path.
        self.assertEqual(
            labels_for_path("scripts/ci/labels/test_label_by_title.py"),
            frozenset({"automated tests", "ci"}),
        )

    def test_a_path_outside_every_glob_is_not_ci(self):
        self.assertEqual(labels_for_path("scripts/lint/lint.sh"), frozenset())

    def test_path_under_an_any_file_prefix_is_that_label_only(self):
        path = ".claude/skills/orchestrate/scripts/ci_monitor/ci_monitor.py"
        self.assertEqual(labels_for_path(path), frozenset({"agents"}))

    def test_unclassified_paths_give_empty_set(self):
        for path in (
            "app/src/main/java/com/gb4pc/Foo.kt",
            "README.md",
            ".github/release.yml",
            ".github/allowed-test-failures.txt",
        ):
            self.assertEqual(labels_for_path(path), frozenset(), msg=path)

    def test_file_literally_named_test_is_not_itself_a_test(self):
        self.assertEqual(labels_for_path("app/src/main/test"), frozenset())

    def test_agents_path_set_gives_agents(self):
        for path in (
            "AGENTS.md",
            "CLAUDE.md",
            "agents/code_edit.md",
            "agents/pr_creation.md",
            "scripts/agents/verify_github_write.py",
            ".claude/rules/prose-style.md",
            ".claude/settings.json",
            ".claude/skills/orchestrate/scripts/update_gh_labels.sh",
            ".claude/skills/orchestrate/scripts/ci_monitor/CLAUDE.md",
        ):
            self.assertEqual(labels_for_path(path), frozenset({"agents"}), msg=path)

    def test_agents_test_script_gives_agents_and_automated_tests(self):
        self.assertEqual(
            labels_for_path(".claude/skills/orchestrate/scripts/test_update_gh_labels.sh"),
            frozenset({"agents", "automated tests"}),
        )

    def test_workflow_claude_md_gives_agents_and_ci(self):
        self.assertEqual(
            labels_for_path(".github/workflows/CLAUDE.md"),
            frozenset({"agents", "ci"}),
        )

    def test_agents_matching_is_case_sensitive_and_directory_only(self):
        # A file named "agents" is not the directory the glob names, and a
        # lowercase docs/agents.md is a different document from AGENTS.md.
        for path in ("app/src/main/agents", "docs/agents.md"):
            self.assertEqual(labels_for_path(path), frozenset(), msg=path)


# ---------------------------------------------------------------------------
# matching_labels tests
# ---------------------------------------------------------------------------


class TestMatchingLabels(unittest.TestCase):
    def test_empty_list_gives_no_labels(self):
        self.assertEqual(matching_labels([]), [])

    def test_all_test_paths_give_automated_tests(self):
        self.assertEqual(
            matching_labels(
                [
                    "app/src/test/java/com/gb4pc/FooTest.kt",
                    "app/src/androidTest/java/com/gb4pc/BarTest.kt",
                ]
            ),
            ["automated tests"],
        )

    def test_all_workflow_paths_give_ci(self):
        self.assertEqual(
            matching_labels([".github/workflows/build.yml", ".github/workflows/lint.yml"]),
            ["ci"],
        )

    def test_mixing_test_and_workflow_files_gives_no_labels(self):
        # Unanimity: a test path justifies only "automated tests" and a
        # workflow path only "ci", so their intersection is empty.
        self.assertEqual(
            matching_labels(
                [
                    "app/src/test/java/com/gb4pc/FooTest.kt",
                    ".github/workflows/build.yml",
                ]
            ),
            [],
        )

    def test_test_files_plus_one_product_file_gives_no_labels(self):
        self.assertEqual(
            matching_labels(
                [
                    "app/src/test/java/com/gb4pc/FooTest.kt",
                    "app/src/androidTest/java/com/gb4pc/BarTest.kt",
                    "app/src/main/java/com/gb4pc/Foo.kt",
                ]
            ),
            [],
        )

    def test_test_source_sets_only(self):
        paths = [
            "app/src/androidTest/java/com/gb4pc/e2e/visual/ColorMatch.kt",
            "app/src/sharedTest/java/com/gb4pc/e2e/visual/PixelMask.kt",
            "app/src/test/java/com/gb4pc/e2e/visual/PixelMaskTest.kt",
        ]
        self.assertEqual(matching_labels(paths), ["automated tests"])

    def test_a_single_workflow_file(self):
        self.assertEqual(matching_labels([".github/workflows/strip-session-bylines.yml"]), ["ci"])

    def test_script_plus_its_test_sibling_and_workflow(self):
        # The script scores {ci} and its test {ci, automated tests}, so
        # unanimity fires on {ci}.
        paths = [
            ".github/workflows/label-by-files.yml",
            "scripts/ci/labels/label_by_files.py",
            "scripts/ci/labels/test_label_by_files.py",
        ]
        self.assertEqual(matching_labels(paths), ["ci"])

    # -- the any-file (if-and-only-if) half ----------------------------------

    def test_one_agents_path_among_unrelated_files_still_gives_agents(self):
        self.assertEqual(
            matching_labels(["agents/code_edit.md", "app/src/main/java/Foo.kt"]),
            ["agents"],
        )

    def test_agents_path_does_not_drag_an_every_file_label_along(self):
        self.assertEqual(
            matching_labels(["agents/x.md", ".github/workflows/build.yml"]),
            ["agents"],
        )

    def test_one_path_can_produce_both_modes(self):
        self.assertEqual(matching_labels([".github/workflows/CLAUDE.md"]), ["agents", "ci"])

    def test_any_file_applies_though_most_paths_justify_another_label(self):
        paths = [
            ".claude/environment.md",
            ".claude/hooks/session-start.sh",
            ".github/workflows/CLAUDE.md",
            ".github/workflows/build.yml",
            ".github/workflows/lint.yml",
        ]
        self.assertIn("agents", matching_labels(paths))

    def test_an_any_file_label_is_not_held_to_unanimity(self):
        # Mode, not the globs, decides how a label combines across paths.
        rules = lbf.parse_rules(
            '{"a": {"mode": "any-file", "paths": ["x/**"]},'
            ' "b": {"mode": "every-file", "paths": ["x/**"]}}'
        )
        self.assertEqual(lbf.matching_labels(["x/1", "y/2"], rules), ["a"])
        self.assertEqual(lbf.matching_labels(["x/1", "x/2"], rules), ["a", "b"])


# ---------------------------------------------------------------------------
# labels_to_remove tests
# ---------------------------------------------------------------------------


class TestLabelsToRemove(unittest.TestCase):
    def test_empty_list_removes_nothing(self):
        # An empty changed-file list is missing evidence, not evidence of
        # absence.
        self.assertEqual(labels_to_remove([]), [])

    def test_product_only_list_removes_agents(self):
        self.assertEqual(labels_to_remove(["app/src/main/java/com/gb4pc/Foo.kt"]), ["agents"])

    def test_any_agents_path_keeps_agents(self):
        self.assertEqual(
            labels_to_remove(["app/src/main/java/Foo.kt", ".claude/settings.json"]),
            [],
        )

    def test_all_test_list_removes_agents(self):
        # Whether the PR carries the label is the caller's concern.
        self.assertEqual(
            labels_to_remove(["app/src/test/java/com/gb4pc/FooTest.kt"]),
            ["agents"],
        )

    def test_every_file_labels_are_never_removed(self):
        self.assertNotIn("ci", labels_to_remove(["app/src/main/java/Foo.kt"]))
        self.assertNotIn("automated tests", labels_to_remove(["app/src/main/java/Foo.kt"]))

    def test_paths_outside_the_any_file_globs_remove_it_and_add_nothing(self):
        paths = [
            "scripts/ci_monitor/README.md",
            "scripts/ci_monitor/ci_monitor.py",
            "scripts/test_ci_monitor.py",
        ]
        self.assertEqual(labels_to_remove(paths), ["agents"])
        self.assertEqual(matching_labels(paths), [])


# ---------------------------------------------------------------------------
# fetch_changed_files tests
# ---------------------------------------------------------------------------


class TestFetchChangedFiles(unittest.TestCase):
    def _page(self, n: int, start: int = 0) -> list[dict]:
        return [{"filename": f"file{i}.py"} for i in range(start, start + n)]

    def test_stops_on_short_page(self):
        page1 = self._page(100)
        page2 = self._page(1, start=100)
        with patch.object(lbf.lc, "gh_api", side_effect=[page1, page2]) as mock_api:
            result = lbf.fetch_changed_files("owner/repo", 42, "tok")
        self.assertEqual(len(result), 101)
        self.assertEqual(mock_api.call_count, 2)

    def test_stops_on_empty_first_page(self):
        with patch.object(lbf.lc, "gh_api", return_value=[]) as mock_api:
            result = lbf.fetch_changed_files("owner/repo", 42, "tok")
        self.assertEqual(result, [])
        mock_api.assert_called_once()

    def test_single_short_page_returns_its_paths(self):
        with patch.object(lbf.lc, "gh_api", return_value=[{"filename": "a.py"}]):
            result = lbf.fetch_changed_files("owner/repo", 42, "tok")
        self.assertEqual(result, ["a.py"])

    def test_hitting_max_pages_without_a_short_page_returns_none(self):
        full_page = self._page(100)
        with patch.object(lbf.lc, "gh_api", return_value=full_page) as mock_api:
            result = lbf.fetch_changed_files("owner/repo", 42, "tok")
        self.assertIsNone(result)
        self.assertEqual(mock_api.call_count, lbf.MAX_PAGES)

    def test_uses_pull_files_endpoint(self):
        with patch.object(lbf.lc, "gh_api", return_value=[]) as mock_api:
            lbf.fetch_changed_files("owner/repo", 42, "tok")
        self.assertIn("repos/owner/repo/pulls/42/files", mock_api.call_args[0][0])


# ---------------------------------------------------------------------------
# main() tests
# ---------------------------------------------------------------------------


class FakeApi:
    """Stands in for lc.gh_api: records calls, answers the PR's labels.

    *labels* are the PR's current labels; *delete_error* is raised on DELETE,
    *get_error* on the GET of the PR, *post_error* on the POST.
    """

    def __init__(self, labels=(), delete_error=None, get_error=None, post_error=None):
        self.labels = list(labels)
        self.delete_error = delete_error
        self.get_error = get_error
        self.post_error = post_error
        self.calls: list[tuple[str, str, object]] = []

    def __call__(self, path, token, method="GET", body=None, **kwargs):
        self.calls.append((method, path, body))
        if method == "DELETE" and self.delete_error is not None:
            raise self.delete_error
        if method == "POST" and self.post_error is not None:
            raise self.post_error
        if method == "GET":
            if self.get_error is not None:
                raise self.get_error
            return {"labels": [{"name": name} for name in self.labels]}
        return None

    def paths(self, method: str) -> list[str]:
        return [path for m, path, _ in self.calls if m == method]

    def bodies(self, method: str) -> list[object]:
        return [body for m, _, body in self.calls if m == method]


class TestMain(unittest.TestCase):
    def setUp(self):
        self._env_patch = patch.dict(
            os.environ,
            {
                "GITHUB_TOKEN": "test-token",
                "GITHUB_REPOSITORY": "owner/repo",
                "PR_NUMBER": "42",
                "RULES": RULES_JSON,
            },
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()

    def run_main(self, paths, api: FakeApi | None = None) -> tuple[int, FakeApi]:
        api = api or FakeApi()
        fetch = (
            patch.object(lbf, "fetch_changed_files", side_effect=paths)
            if isinstance(paths, Exception)
            else patch.object(lbf, "fetch_changed_files", return_value=paths)
        )
        with fetch, patch.object(lbf.lc, "gh_api", side_effect=api):
            result = lbf.main()
        return result, api

    def test_exit_1_when_required_configuration_is_missing(self):
        for name in ("GITHUB_TOKEN", "GITHUB_REPOSITORY", "PR_NUMBER", "RULES"):
            with self.subTest(variable=name), patch.dict(os.environ, {name: ""}):
                with patch.object(lbf, "fetch_changed_files") as mock_fetch:
                    self.assertEqual(lbf.main(), 1)
                mock_fetch.assert_not_called()

    def test_exit_1_when_pr_number_non_integer(self):
        with patch.dict(os.environ, {"PR_NUMBER": "abc"}):
            self.assertEqual(lbf.main(), 1)

    def test_exit_1_when_rules_are_invalid(self):
        for text in ("{", "[]", '{"ci": {"mode": "some-file", "paths": ["a"]}}'):
            with self.subTest(rules=text), patch.dict(os.environ, {"RULES": text}):
                with patch.object(lbf, "fetch_changed_files") as mock_fetch:
                    self.assertEqual(lbf.main(), 1)
                mock_fetch.assert_not_called()

    def test_no_post_and_no_delete_when_no_files_match_and_label_absent(self):
        result, api = self.run_main(["app/src/main/java/Foo.kt"])
        self.assertEqual(result, 0)
        self.assertEqual(api.paths("POST"), [])
        # The rule forbids `agents` here, but the PR does not carry it, so a
        # DELETE would only 404.
        self.assertEqual(api.paths("DELETE"), [])

    def test_deletes_a_carried_label_no_changed_file_justifies(self):
        result, api = self.run_main(["app/src/main/java/Foo.kt"], FakeApi(labels=["agents", "p1"]))
        self.assertEqual(result, 0)
        self.assertEqual(api.paths("POST"), [])
        self.assertEqual(api.paths("DELETE"), ["repos/owner/repo/issues/42/labels/agents"])

    def test_deletes_by_the_spelling_github_returned(self):
        _, api = self.run_main(["app/src/main/java/Foo.kt"], FakeApi(labels=["Agents"]))
        self.assertEqual(api.paths("DELETE"), ["repos/owner/repo/issues/42/labels/Agents"])

    def test_no_verdict_when_file_list_possibly_truncated(self):
        result, api = self.run_main(None)
        self.assertEqual(result, 0)
        self.assertEqual(api.calls, [])

    def test_no_current_label_fetch_when_diff_justifies_agents(self):
        # labels_to_remove is [] when a changed path justifies "agents", so
        # the GET of the PR's current labels never runs.
        result, api = self.run_main(["agents/code_edit.md"])
        self.assertEqual(result, 0)
        self.assertEqual(api.bodies("POST"), [{"labels": ["agents"]}])
        self.assertEqual(api.paths("GET"), [])

    def test_removal_log_line_says_unjustified_not_conflicting(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.run_main(["app/src/main/java/Foo.kt"], FakeApi(labels=["agents"]))
        self.assertIn("Removed unjustified label 'agents'", out.getvalue())

    def test_no_verdict_when_file_list_is_empty(self):
        result, api = self.run_main([])
        self.assertEqual(result, 0)
        self.assertEqual(api.calls, [])

    def test_exit_1_when_removal_fails_with_a_non_404(self):
        api = FakeApi(labels=["agents"], delete_error=_http_error(500))
        result, _ = self.run_main(["app/src/main/java/Foo.kt"], api)
        self.assertEqual(result, 1)

    def test_exit_0_when_removal_404s(self):
        # A 404 means the label was already gone, which is the end state
        # wanted, not a failure.
        api = FakeApi(labels=["agents"], delete_error=_http_error(404))
        result, _ = self.run_main(["app/src/main/java/Foo.kt"], api)
        self.assertEqual(result, 0)

    def test_exit_1_when_the_current_label_fetch_fails(self):
        api = FakeApi(get_error=urllib.error.URLError("network error"))
        result, _ = self.run_main(["app/src/main/java/Foo.kt"], api)
        self.assertEqual(result, 1)

    def test_posts_and_deletes_are_both_attempted_in_one_run(self):
        # A diff that justifies `ci` but forbids `agents`, on a PR carrying it.
        result, api = self.run_main([".github/workflows/build.yml"], FakeApi(labels=["agents"]))
        self.assertEqual(result, 0)
        self.assertEqual(api.bodies("POST"), [{"labels": ["ci"]}])
        self.assertEqual(api.paths("DELETE"), ["repos/owner/repo/issues/42/labels/agents"])

    def test_posts_matching_label(self):
        result, api = self.run_main([".github/workflows/build.yml"])
        self.assertEqual(result, 0)
        self.assertEqual(api.paths("POST"), ["repos/owner/repo/issues/42/labels"])
        self.assertEqual(api.bodies("POST"), [{"labels": ["ci"]}])

    def test_exit_1_on_fetch_http_error(self):
        result, _ = self.run_main(_http_error(404))
        self.assertEqual(result, 1)

    def test_exit_1_on_fetch_url_error(self):
        result, _ = self.run_main(urllib.error.URLError("network error"))
        self.assertEqual(result, 1)

    def test_exit_1_on_apply_http_error(self):
        api = FakeApi(post_error=_http_error(422))
        result, _ = self.run_main([".github/workflows/build.yml"], api)
        self.assertEqual(result, 1)

    def test_exit_1_on_apply_url_error(self):
        api = FakeApi(post_error=urllib.error.URLError("network error"))
        result, _ = self.run_main([".github/workflows/build.yml"], api)
        self.assertEqual(result, 1)

    def test_a_failed_add_still_attempts_the_removal(self):
        api = FakeApi(labels=["agents"], post_error=_http_error(422))
        result, _ = self.run_main([".github/workflows/build.yml"], api)
        self.assertEqual(result, 1)
        self.assertEqual(api.paths("DELETE"), ["repos/owner/repo/issues/42/labels/agents"])


if __name__ == "__main__":
    unittest.main()
