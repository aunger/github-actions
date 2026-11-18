#!/usr/bin/env python3
"""Unit tests for audit_labels_by_files.py.

RULES_JSON is aunger/gallery-button-for-pixel-camera's table, as in
test_label_by_files.py; the paths are sample data.
"""

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import audit_labels_by_files as alf

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
RULES = alf.lbf.parse_rules(RULES_JSON)
RULES_ARGS = ["--rules", RULES_JSON]


def _pull(number: int, labels: list[str], merged: bool = True) -> dict:
    return {
        "number": number,
        "labels": [{"name": lbl} for lbl in labels],
        "merged_at": "2026-07-01T00:00:00Z" if merged else None,
    }


def _paths_from(mapping: dict[int, list[str] | None]):
    """Build a fetch_paths callable backed by *mapping*."""
    return lambda number: mapping[number]


def _pulls(n: int, start: int = 0) -> list[dict]:
    """Build *n* merged, unlabeled pull request dicts with distinct numbers."""
    return [_pull(start + i, []) for i in range(n)]


def quiet_main(argv: list[str]) -> tuple[int, str]:
    """Run main(*argv*) with stderr captured; return its exit code and stderr."""
    err = io.StringIO()
    with redirect_stderr(err):
        result = alf.main(argv)
    return result, err.getvalue()


def _report(pulls: list[dict], mapping: dict[int, list[str] | None]) -> alf.Report:
    return alf.build_report(pulls, _paths_from(mapping), RULES)


# ---------------------------------------------------------------------------
# fetch_merged_pulls
# ---------------------------------------------------------------------------


class TestFetchMergedPulls(unittest.TestCase):
    def test_skips_closed_but_unmerged_pull_requests(self):
        page = [_pull(3, []), _pull(2, [], merged=False), _pull(1, [])]
        with patch.object(alf.lc, "gh_api", side_effect=[page, []]):
            result = alf.fetch_merged_pulls("owner/repo", "tok")
        self.assertEqual([p["number"] for p in result], [3, 1])

    def test_stops_on_short_page(self):
        page1 = _pulls(alf.PER_PAGE)
        page2 = _pulls(1, start=alf.PER_PAGE)
        with patch.object(alf.lc, "gh_api", side_effect=[page1, page2]) as mock_api:
            result = alf.fetch_merged_pulls("owner/repo", "tok")
        self.assertEqual(len(result), alf.PER_PAGE + 1)
        self.assertEqual(mock_api.call_count, 2)

    def test_stops_on_empty_page_after_full_pages(self):
        page1 = _pulls(alf.PER_PAGE)
        page2 = _pulls(alf.PER_PAGE, start=alf.PER_PAGE)
        with patch.object(alf.lc, "gh_api", side_effect=[page1, page2, []]) as mock_api:
            result = alf.fetch_merged_pulls("owner/repo", "tok")
        self.assertEqual(len(result), alf.PER_PAGE * 2)
        self.assertEqual(mock_api.call_count, 3)

    def test_limit_truncates_the_pull_request_list(self):
        page = [_pull(i, []) for i in range(10, 0, -1)]
        with patch.object(alf.lc, "gh_api", side_effect=[page, []]) as mock_api:
            result = alf.fetch_merged_pulls("owner/repo", "tok", limit=3)
        self.assertEqual([p["number"] for p in result], [10, 9, 8])
        # Stopping mid-page means the walk never asks for a second page.
        self.assertEqual(mock_api.call_count, 1)

    def test_requests_closed_pull_requests_by_updated_newest_first(self):
        # sort=updated: the API has no sort=merged, and merging updates a pull
        # request, so updated_at approximates merge recency.
        with patch.object(alf.lc, "gh_api", return_value=[]) as mock_api:
            alf.fetch_merged_pulls("owner/repo", "tok")
        path = mock_api.call_args[0][0]
        self.assertIn("repos/owner/repo/pulls?state=closed", path)
        self.assertIn("sort=updated", path)
        self.assertIn("direction=desc", path)


# ---------------------------------------------------------------------------
# build_report
# ---------------------------------------------------------------------------


class TestBuildReport(unittest.TestCase):
    def test_applied_and_missing_label_goes_to_add(self):
        report = _report([_pull(1, [])], {1: [".github/workflows/build.yml"]})
        self.assertEqual(report.add, {"ci": [1]})
        self.assertEqual(report.to_add, {1: ["ci"]})
        self.assertEqual(report.match, {})

    def test_applied_and_present_label_goes_to_match(self):
        report = _report([_pull(2, ["ci"])], {2: [".github/workflows/build.yml"]})
        self.assertEqual(report.match, {"ci": [2]})
        self.assertEqual(report.add, {})
        self.assertEqual(report.to_add, {})

    def test_present_label_matches_ignoring_case(self):
        report = _report([_pull(2, ["CI"])], {2: [".github/workflows/build.yml"]})
        self.assertEqual(report.match, {"ci": [2]})
        self.assertEqual(report.to_add, {})

    def test_forbidden_and_carried_label_goes_to_remove(self):
        report = _report([_pull(3, ["agents"])], {3: ["app/src/main/java/Foo.kt"]})
        self.assertEqual(report.remove, {"agents": [3]})
        self.assertEqual(report.to_remove, {3: ["agents"]})

    def test_forbidden_but_absent_label_is_not_reported(self):
        report = _report([_pull(4, ["ci"])], {4: ["app/src/main/java/Foo.kt"]})
        self.assertEqual(report.remove, {})
        self.assertEqual(report.to_remove, {})

    def test_every_file_labels_are_never_proposed_for_removal(self):
        report = _report([_pull(5, ["ci", "automated tests"])], {5: ["app/src/main/java/Foo.kt"]})
        self.assertEqual(report.remove, {})

    def test_one_pull_request_can_both_gain_and_lose_a_label(self):
        report = _report([_pull(6, ["agents"])], {6: [".github/workflows/build.yml"]})
        self.assertEqual(report.to_add, {6: ["ci"]})
        self.assertEqual(report.to_remove, {6: ["agents"]})

    def test_removal_uses_the_spelling_github_returned(self):
        report = _report([_pull(7, ["Agents"])], {7: ["app/src/main/java/Foo.kt"]})
        self.assertEqual(report.to_remove, {7: ["Agents"]})

    def test_truncated_file_list_is_skipped_and_counted(self):
        report = _report(
            [_pull(8, ["agents"]), _pull(9, [])], {8: None, 9: [".github/workflows/build.yml"]}
        )
        self.assertEqual(report.skipped, [8])
        self.assertEqual(report.remove, {})
        self.assertEqual(report.add, {"ci": [9]})

    def test_empty_file_list_yields_neither_add_nor_remove(self):
        report = _report([_pull(10, ["agents"])], {10: []})
        self.assertEqual((report.add, report.remove, report.skipped), ({}, {}, []))

    def test_numbers_accumulate_per_label(self):
        report = _report(
            [_pull(1, []), _pull(2, [])],
            {1: [".github/workflows/build.yml"], 2: [".github/workflows/lint.yml"]},
        )
        self.assertEqual(sorted(report.add["ci"]), [1, 2])


# ---------------------------------------------------------------------------
# apply_changes
# ---------------------------------------------------------------------------


class TestApplyChanges(unittest.TestCase):
    def test_posts_adds_and_deletes_removals(self):
        with patch.object(alf.lc, "gh_api") as mock_api:
            result = alf.apply_changes({1: ["ci"]}, {2: ["agents"]}, "owner/repo", "tok")
        self.assertTrue(result)
        calls = [(c.args[0], c.kwargs.get("method")) for c in mock_api.call_args_list]
        self.assertEqual(
            calls,
            [
                ("repos/owner/repo/issues/1/labels", "POST"),
                ("repos/owner/repo/issues/2/labels/agents", "DELETE"),
            ],
        )

    def test_removal_log_line_says_unjustified_not_conflicting(self):
        with patch.object(alf.lc, "gh_api"):
            out = io.StringIO()
            with redirect_stdout(out):
                alf.apply_changes({}, {2: ["agents"]}, "owner/repo", "tok")
        self.assertIn("Removed unjustified label 'agents' from #2.", out.getvalue())

    def test_no_calls_when_nothing_to_do(self):
        with patch.object(alf.lc, "gh_api") as mock_api:
            result = alf.apply_changes({}, {}, "owner/repo", "tok")
        self.assertTrue(result)
        mock_api.assert_not_called()

    def test_continues_past_a_failure_but_reports_it(self):
        def fake_api(path, token, method="GET", body=None, **kwargs):
            if method == "POST" and "/1/" in path:
                raise urllib.error.HTTPError(
                    url=None, code=422, msg="Unprocessable", hdrs=None, fp=None
                )

        with patch.object(alf.lc, "gh_api", side_effect=fake_api) as mock_api:
            result = alf.apply_changes({1: ["ci"], 2: ["ci"]}, {3: ["agents"]}, "owner/repo", "tok")
        self.assertFalse(result)
        methods = [c.kwargs.get("method") for c in mock_api.call_args_list]
        self.assertEqual(methods, ["POST", "POST", "DELETE"])

    def test_a_failed_removal_is_reported(self):
        def fake_api(path, token, method="GET", body=None, **kwargs):
            if method == "DELETE":
                raise urllib.error.HTTPError(
                    url=None, code=403, msg="Forbidden", hdrs=None, fp=None
                )

        with patch.object(alf.lc, "gh_api", side_effect=fake_api):
            self.assertFalse(alf.apply_changes({}, {3: ["agents"]}, "owner/repo", "tok"))


# ---------------------------------------------------------------------------
# format_report
# ---------------------------------------------------------------------------


class TestFormatReport(unittest.TestCase):
    def test_output_is_valid_json_with_all_three_categories(self):
        report = alf.Report({"ci": [2, 1]}, {}, {"agents": [3]}, {}, {}, [])
        parsed = json.loads(alf.format_report(report))
        self.assertEqual(parsed, {"add": {"ci": [1, 2]}, "match": {}, "remove": {"agents": [3]}})


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


class TestMain(unittest.TestCase):
    def setUp(self):
        self._env_patch = patch.dict(
            os.environ,
            {"GITHUB_TOKEN": "test-token", "GITHUB_REPOSITORY": "owner/repo"},
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()

    def test_requires_a_mode_flag(self):
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            alf.main(RULES_ARGS)

    def test_requires_a_rules_source(self):
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            alf.main(["-n"])

    def test_rules_and_workflow_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            alf.main(["-n", "--rules", RULES_JSON, "--workflow", "x.yml"])

    def test_exit_1_when_token_missing(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": ""}):
            self.assertEqual(quiet_main(["-n", *RULES_ARGS])[0], 1)

    def test_exit_1_when_repository_missing(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": ""}):
            self.assertEqual(quiet_main(["-n", *RULES_ARGS])[0], 1)

    def test_exit_1_when_limit_is_not_positive(self):
        self.assertEqual(quiet_main(["-n", *RULES_ARGS, "--limit", "0"])[0], 1)

    def test_exit_1_when_rules_are_invalid_before_any_fetch(self):
        with patch.object(alf, "fetch_merged_pulls") as mock_fetch:
            result, err = quiet_main(["-n", "--rules", "{}"])
        self.assertEqual(result, 1)
        self.assertIn("rules", err)
        mock_fetch.assert_not_called()

    def test_dry_run_writes_nothing(self):
        with patch.object(alf, "fetch_merged_pulls", return_value=[_pull(1, ["agents"])]):
            with patch.object(alf.lbf, "fetch_changed_files", return_value=["app/src/main/Foo.kt"]):
                with patch.object(alf, "apply_changes") as mock_apply:
                    result = alf.main(["-n", "-q", *RULES_ARGS])
        self.assertEqual(result, 0)
        mock_apply.assert_not_called()

    def test_dry_run_prints_the_report(self):
        with patch.object(alf, "fetch_merged_pulls", return_value=[_pull(1, ["agents"])]):
            with patch.object(alf.lbf, "fetch_changed_files", return_value=["app/src/main/Foo.kt"]):
                out = io.StringIO()
                with redirect_stdout(out):
                    alf.main(["-n", *RULES_ARGS])
        self.assertEqual(
            json.loads(out.getvalue()), {"add": {}, "match": {}, "remove": {"agents": [1]}}
        )

    def test_force_posts_and_deletes(self):
        pulls = [_pull(1, []), _pull(2, ["agents"])]
        paths = {1: [".github/workflows/build.yml"], 2: ["app/src/main/Foo.kt"]}
        with patch.object(alf, "fetch_merged_pulls", return_value=pulls):
            with patch.object(
                alf.lbf,
                "fetch_changed_files",
                side_effect=lambda repo, number, token: paths[number],
            ):
                with patch.object(alf.lc, "gh_api") as mock_api:
                    result = alf.main(["-f", "-q", *RULES_ARGS])
        self.assertEqual(result, 0)
        calls = [
            (c.args[0], c.kwargs.get("method"), c.kwargs.get("body"))
            for c in mock_api.call_args_list
        ]
        self.assertEqual(
            calls,
            [
                ("repos/owner/repo/issues/1/labels", "POST", {"labels": ["ci"]}),
                ("repos/owner/repo/issues/2/labels/agents", "DELETE", None),
            ],
        )

    def test_force_returns_1_when_a_write_fails(self):
        with patch.object(alf, "fetch_merged_pulls", return_value=[_pull(1, [])]):
            with patch.object(
                alf.lbf, "fetch_changed_files", return_value=[".github/workflows/build.yml"]
            ):
                with patch.object(
                    alf.lc, "gh_api", side_effect=urllib.error.URLError("network error")
                ):
                    result = quiet_main(["-f", "-q", *RULES_ARGS])[0]
        self.assertEqual(result, 1)

    def test_per_pull_request_fetch_failure_is_skipped_not_fatal(self):
        pulls = [_pull(1, ["agents"]), _pull(2, [])]

        def fake_fetch(repo, number, token):
            if number == 1:
                raise urllib.error.HTTPError(
                    url=None, code=404, msg="Not Found", hdrs=None, fp=None
                )
            return [".github/workflows/build.yml"]

        with patch.object(alf, "fetch_merged_pulls", return_value=pulls):
            with patch.object(alf.lbf, "fetch_changed_files", side_effect=fake_fetch):
                with patch.object(alf, "apply_changes", return_value=True) as mock_apply:
                    result, err = quiet_main(["-f", "-q", *RULES_ARGS])
        self.assertEqual(result, 0)
        # #1 yielded no verdict, so nothing is written for it.
        to_add, to_remove = mock_apply.call_args[0][0], mock_apply.call_args[0][1]
        self.assertEqual(to_add, {2: ["ci"]})
        self.assertEqual(to_remove, {})
        self.assertIn("Skipped 1 of 2", err)

    def test_exit_1_when_the_pull_request_list_cannot_be_fetched(self):
        with patch.object(alf, "fetch_merged_pulls", side_effect=RuntimeError("boom")):
            self.assertEqual(quiet_main(["-n", "-q", *RULES_ARGS])[0], 1)

    def test_limit_is_passed_through_to_the_fetch(self):
        with patch.object(alf, "fetch_merged_pulls", return_value=[]) as mock_fetch:
            alf.main(["-n", "-q", *RULES_ARGS, "--limit", "5"])
        self.assertEqual(mock_fetch.call_args[0][2], 5)


# ---------------------------------------------------------------------------
# --workflow
# ---------------------------------------------------------------------------


WORKFLOW = """\
name: Label PR by changed files
on:
  pull_request:
    types: [opened, synchronize]
jobs:
  label-by-files:
    runs-on: ubuntu-latest
    steps:
      - name: Apply labels matching the changed files
        uses: aunger/vcs-automation/label-by-files@0123456789abcdef0123456789abcdef01234567 # v1.2.0
        with:
          rules: |
            {"docs": {"mode": "any-file", "paths": ["docs/**"]}}
"""


class TestWorkflowRules(unittest.TestCase):
    def setUp(self):
        self._env_patch = patch.dict(
            os.environ,
            {"GITHUB_TOKEN": "test-token", "GITHUB_REPOSITORY": "owner/repo"},
        )
        self._env_patch.start()
        self._dir = tempfile.TemporaryDirectory()
        self.workflow = Path(self._dir.name) / "label-by-files.yml"

    def tearDown(self):
        self._dir.cleanup()
        self._env_patch.stop()

    def test_reads_the_rules_from_the_step_using_the_action(self):
        self.workflow.write_text(WORKFLOW, encoding="utf-8")
        with patch.object(alf, "fetch_merged_pulls", return_value=[_pull(1, ["docs"])]):
            with patch.object(alf.lbf, "fetch_changed_files", return_value=["app/Foo.kt"]):
                out = io.StringIO()
                with redirect_stdout(out):
                    result = alf.main(["-n", "--workflow", str(self.workflow)])
        self.assertEqual(result, 0)
        self.assertEqual(
            json.loads(out.getvalue()), {"add": {}, "match": {}, "remove": {"docs": [1]}}
        )

    def test_exit_1_when_no_step_uses_the_action(self):
        self.workflow.write_text(
            WORKFLOW.replace("vcs-automation/label-by-files@", "vcs-automation/label-by-title@"),
            encoding="utf-8",
        )
        with patch.object(alf, "fetch_merged_pulls") as mock_fetch:
            result, err = quiet_main(["-n", "--workflow", str(self.workflow)])
        self.assertEqual(result, 1)
        self.assertIn("0 steps using the label-by-files action", err)
        mock_fetch.assert_not_called()

    def test_exit_1_when_the_workflow_cannot_be_read(self):
        result, err = quiet_main(["-n", "--workflow", str(self.workflow)])
        self.assertEqual(result, 1)
        self.assertIn("cannot read", err)


if __name__ == "__main__":
    unittest.main()
