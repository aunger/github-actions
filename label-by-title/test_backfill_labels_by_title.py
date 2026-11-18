#!/usr/bin/env python3
"""Unit tests for backfill_labels_by_title.py."""

import json
import os
import sys
import tempfile
import textwrap
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import backfill_labels_by_title as blt
from test_label_by_title import GB4PC_RULES_JSON, GB4PC_SKIP, RULES

SKIP = GB4PC_SKIP
SOURCE = ["--rules", GB4PC_RULES_JSON, "--skip-on-pull-requests", '["agents"]']


def _issue(number: int, title: str, labels: list[str]) -> dict:
    return {"number": number, "title": title, "labels": [{"name": lbl} for lbl in labels]}


def _pull_request(number: int, title: str, labels: list[str]) -> dict:
    """Build an /issues item for a pull request (marked by the pull_request key)."""
    item = _issue(number, title, labels)
    item["pull_request"] = {"url": f"https://api.github.com/repos/owner/repo/pulls/{number}"}
    return item


# ---------------------------------------------------------------------------
# Tracked labels
# ---------------------------------------------------------------------------


class TestTrackedLabels(unittest.TestCase):
    def test_every_rule_label_is_tracked(self):
        items = [_issue(1, "Bump the version number", ["ci", "agents", "automated tests"])]
        _add, _match, miss, _to_apply = blt.build_report(items, RULES, SKIP)
        self.assertEqual({"ci": [1], "agents": [1], "automated tests": [1]}, dict(miss))

    def test_a_label_with_no_rule_is_not_tracked(self):
        # A label with no rule (e.g. "product") can never be matched, so it
        # would sit in "miss" forever, on every run, for every item carrying it.
        items = [_issue(1, "Bump the version number", ["product", "bug"])]
        _add, _match, miss, _to_apply = blt.build_report(items, RULES, SKIP)
        self.assertEqual({}, dict(miss))

    def test_labels_compare_case_insensitively_against_the_rules(self):
        items = [_issue(1, "Fix the CI pipeline", ["CI"]), _issue(2, "Bump it", ["Agents"])]
        add, match, miss, to_apply = blt.build_report(items, RULES, SKIP)
        self.assertEqual({"ci": [1]}, dict(match))
        self.assertEqual({}, dict(add))
        self.assertEqual({"agents": [2]}, dict(miss))
        self.assertEqual({}, dict(to_apply))


# ---------------------------------------------------------------------------
# fetch_all_issues
# ---------------------------------------------------------------------------


class TestFetchAllIssues(unittest.TestCase):
    def test_paginates_until_empty_page(self):
        # A full page (100 items) is followed by a short page (1 item) and
        # then the terminating empty page, mirroring the pagination
        # convention in archive-stale-test-failures: stop only on an empty
        # batch, not merely a short one.
        page1 = [_issue(i, f"title {i}", []) for i in range(100)]
        page2 = [_issue(100, "title 100", [])]
        with patch.object(blt.lc, "gh_api", side_effect=[page1, page2, []]) as mock_api:
            result = blt.fetch_all_issues("owner/repo", "tok")
        self.assertEqual(len(result), 101)
        self.assertEqual(mock_api.call_count, 3)

    def test_stops_on_empty_batch(self):
        with patch.object(blt.lc, "gh_api", side_effect=[[], []]) as mock_api:
            result = blt.fetch_all_issues("owner/repo", "tok")
        self.assertEqual(result, [])
        mock_api.assert_called_once()

    def test_uses_state_all(self):
        with patch.object(blt.lc, "gh_api", return_value=[]) as mock_api:
            blt.fetch_all_issues("owner/repo", "tok")
        self.assertIn("state=all", mock_api.call_args[0][0])


# ---------------------------------------------------------------------------
# build_report
# ---------------------------------------------------------------------------


class TestBuildReport(unittest.TestCase):
    def test_matched_and_missing_label_goes_to_add(self):
        items = [_issue(1, "Fix the CI pipeline", [])]
        add, _match, _miss, to_apply = blt.build_report(items, RULES, SKIP)
        self.assertEqual(add["ci"], [1])
        self.assertEqual(to_apply[1], ["ci"])

    def test_matched_and_present_label_goes_to_match(self):
        items = [_issue(2, "Fix the CI pipeline", ["ci"])]
        _add, match, _miss, to_apply = blt.build_report(items, RULES, SKIP)
        self.assertEqual(match["ci"], [2])
        self.assertNotIn(2, to_apply)

    def test_tracked_label_present_but_unmatched_goes_to_miss(self):
        items = [_issue(3, "Bump the version number", ["agents"])]
        _add, _match, miss, to_apply = blt.build_report(items, RULES, SKIP)
        self.assertEqual(miss["agents"], [3])
        self.assertNotIn(3, to_apply)

    def test_untracked_label_present_but_unmatched_is_ignored(self):
        items = [_issue(4, "Bump the version number", ["bug"])]
        add, match, miss, _to_apply = blt.build_report(items, RULES, SKIP)
        self.assertEqual(add, {})
        self.assertEqual(match, {})
        self.assertEqual(miss, {})

    def test_product_label_is_not_tracked(self):
        # "product" has no title-matching rule, so it must never land in
        # "miss": that category would then contain every "product"-labeled
        # issue/PR on every run, forever, drowning out genuine rule drift.
        items = [_issue(6, "Bump the version number", ["product"])]
        add, match, miss, _to_apply = blt.build_report(items, RULES, SKIP)
        self.assertEqual(add, {})
        self.assertEqual(match, {})
        self.assertEqual(miss, {})

    def test_ci_present_but_title_no_longer_matches_goes_to_miss(self):
        items = [_issue(5, "Bump the version number", ["ci"])]
        _add, _match, miss, _to_apply = blt.build_report(items, RULES, SKIP)
        self.assertEqual(miss["ci"], [5])

    def test_multiple_items_accumulate_per_label(self):
        items = [
            _issue(1, "Fix the CI pipeline", []),
            _issue(2, "Automate the release", []),
        ]
        add, _match, _miss, _to_apply = blt.build_report(items, RULES, SKIP)
        self.assertEqual(sorted(add["ci"]), [1, 2])

    def test_multi_label_title_produces_multiple_to_apply_entries(self):
        items = [_issue(1, "Add CiWatcher agent and fix Reviewer CI-polling loop", [])]
        _add, _match, _miss, to_apply = blt.build_report(items, RULES, SKIP)
        self.assertIn("ci", to_apply[1])
        self.assertIn("agents", to_apply[1])

    # -- skip-on-pull-requests labels ----------------------------------------

    def test_pull_request_never_gets_a_file_determined_label_proposed(self):
        # -f would otherwise reapply what label-by-files' any-file rule removed.
        items = [_pull_request(1, "Tighten guidance for agents", [])]
        add, _match, _miss, to_apply = blt.build_report(items, RULES, SKIP)
        self.assertNotIn("agents", add)
        self.assertNotIn(1, to_apply)

    def test_pull_request_carrying_a_file_determined_label_is_not_a_miss(self):
        # Reporting it would put every `agents`-labeled PR in "miss" on every
        # run: the permanent noise tracking only rule labels exists to avoid.
        items = [_pull_request(2, "Bump the version number", ["agents"])]
        _add, _match, miss, _to_apply = blt.build_report(items, RULES, SKIP)
        self.assertEqual(miss, {})

    def test_pull_request_still_reports_labels_the_title_rule_owns(self):
        items = [_pull_request(3, "Bump the version number", ["ci", "agents"])]
        _add, _match, miss, _to_apply = blt.build_report(items, RULES, SKIP)
        self.assertEqual(miss["ci"], [3])
        self.assertNotIn("agents", miss)

    def test_issue_without_a_pull_request_key_is_unchanged(self):
        items = [_issue(4, "Tighten guidance for agents", [])]
        add, _match, _miss, to_apply = blt.build_report(items, RULES, SKIP)
        self.assertEqual(add["agents"], [4])
        self.assertEqual(to_apply[4], ["agents"])


# ---------------------------------------------------------------------------
# apply_labels
# ---------------------------------------------------------------------------


class TestApplyLabels(unittest.TestCase):
    def test_posts_once_per_issue(self):
        to_apply = {1: ["ci"], 2: ["agents", "automated tests"]}
        with patch.object(blt.lc, "gh_api") as mock_api:
            result = blt.apply_labels(to_apply, "owner/repo", "tok")
        self.assertEqual(mock_api.call_count, 2)
        paths = [c[0][0] for c in mock_api.call_args_list]
        self.assertIn("repos/owner/repo/issues/1/labels", paths)
        self.assertIn("repos/owner/repo/issues/2/labels", paths)
        self.assertTrue(result)

    def test_no_calls_when_empty(self):
        with patch.object(blt.lc, "gh_api") as mock_api:
            result = blt.apply_labels({}, "owner/repo", "tok")
        mock_api.assert_not_called()
        self.assertTrue(result)

    def test_continues_after_http_error_but_reports_failure(self):
        error = urllib.error.HTTPError(url=None, code=422, msg="Unprocessable", hdrs=None, fp=None)
        calls = []

        def fake_api(path, token, method="GET", body=None):
            calls.append(path)
            if "/1/" in path:
                raise error

        with patch.object(blt.lc, "gh_api", side_effect=fake_api):
            result = blt.apply_labels({1: ["ci"], 2: ["ci"]}, "owner/repo", "tok")
        self.assertEqual(len(calls), 2)
        self.assertFalse(result)

    def test_continues_after_url_error_but_reports_failure(self):
        error = urllib.error.URLError("network error")
        with patch.object(blt.lc, "gh_api", side_effect=error):
            result = blt.apply_labels({1: ["ci"]}, "owner/repo", "tok")  # should not raise
        self.assertFalse(result)

    def test_one_failure_among_several_still_reports_failure(self):
        def fake_api(path, token, method="GET", body=None):
            if "/2/" in path:
                raise urllib.error.HTTPError(
                    url=None, code=500, msg="Server Error", hdrs=None, fp=None
                )

        with patch.object(blt.lc, "gh_api", side_effect=fake_api):
            result = blt.apply_labels({1: ["ci"], 2: ["ci"], 3: ["ci"]}, "owner/repo", "tok")
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# format_report
# ---------------------------------------------------------------------------


class TestFormatReport(unittest.TestCase):
    def test_output_is_valid_json(self):
        text = blt.format_report({"ci": [2, 1]}, {}, {"product": [3]})
        parsed = json.loads(text)
        self.assertEqual(parsed, {"add": {"ci": [1, 2]}, "match": {}, "miss": {"product": [3]}})

    def test_numbers_are_sorted(self):
        text = blt.format_report({"ci": [3, 1, 2]}, {}, {})
        parsed = json.loads(text)
        self.assertEqual(parsed["add"]["ci"], [1, 2, 3])

    def test_all_three_keys_always_present(self):
        text = blt.format_report({}, {}, {})
        parsed = json.loads(text)
        self.assertEqual(set(parsed.keys()), {"add", "match", "miss"})

    def test_newline_after_each_category_key(self):
        text = blt.format_report({"ci": [1]}, {}, {})
        self.assertIn('"add":\n{', text)

    def test_newline_after_each_number_list(self):
        text = blt.format_report({"ci": [1]}, {}, {})
        self.assertIn("[1]\n", text)

    def test_no_spaces_in_output(self):
        text = blt.format_report({"ci": [1, 2]}, {"agents": [3]}, {"product": [4]})
        self.assertNotIn(" ", text)

    def test_numbers_are_json_numbers_not_strings(self):
        text = blt.format_report({"ci": [1]}, {}, {})
        self.assertIn("[1]", text)
        self.assertNotIn('["1"]', text)

    def test_include_match_false_omits_match_key(self):
        text = blt.format_report(
            {"ci": [1]}, {"agents": [2]}, {"product": [3]}, include_match=False
        )
        parsed = json.loads(text)
        self.assertEqual(set(parsed.keys()), {"add", "miss"})
        self.assertNotIn("match", parsed)

    def test_include_match_true_is_the_default(self):
        text = blt.format_report({}, {"agents": [2]}, {})
        parsed = json.loads(text)
        self.assertIn("match", parsed)
        self.assertEqual(parsed["match"], {"agents": [2]})


# ---------------------------------------------------------------------------
# parse_skip_matches
# ---------------------------------------------------------------------------


class TestParseSkipMatches(unittest.TestCase):
    def test_none_includes_match_with_no_skips(self):
        include_match, labels = blt.parse_skip_matches(None)
        self.assertTrue(include_match)
        self.assertEqual(labels, frozenset())

    def test_empty_string_omits_match_entirely(self):
        include_match, labels = blt.parse_skip_matches("")
        self.assertFalse(include_match)
        self.assertEqual(labels, frozenset())

    def test_single_label(self):
        include_match, labels = blt.parse_skip_matches("ci")
        self.assertTrue(include_match)
        self.assertEqual(labels, frozenset({"ci"}))

    def test_multiple_labels(self):
        include_match, labels = blt.parse_skip_matches("ci,agents")
        self.assertTrue(include_match)
        self.assertEqual(labels, frozenset({"ci", "agents"}))

    def test_whitespace_around_labels_is_stripped(self):
        _include_match, labels = blt.parse_skip_matches("ci, agents , automated tests")
        self.assertEqual(labels, frozenset({"ci", "agents", "automated tests"}))

    def test_trailing_comma_does_not_add_empty_label(self):
        _include_match, labels = blt.parse_skip_matches("ci,")
        self.assertEqual(labels, frozenset({"ci"}))

    def test_double_quoted_labels_are_unquoted(self):
        # A shell only strips one layer of quoting, e.g.
        # --skip-matches='"ci","agents"' leaves the inner quotes literal.
        _include_match, labels = blt.parse_skip_matches('"ci","agents"')
        self.assertEqual(labels, frozenset({"ci", "agents"}))

    def test_single_quoted_labels_are_unquoted(self):
        _include_match, labels = blt.parse_skip_matches("'ci','agents'")
        self.assertEqual(labels, frozenset({"ci", "agents"}))

    def test_quotes_and_whitespace_together_are_stripped(self):
        _include_match, labels = blt.parse_skip_matches("\"ci\", agents , 'automated tests'")
        self.assertEqual(labels, frozenset({"ci", "agents", "automated tests"}))


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------


class TestParseArgs(unittest.TestCase):
    def test_dry_run_flag(self):
        args = blt.parse_args(["-n", *SOURCE])
        self.assertTrue(args.dry_run)
        self.assertFalse(args.force)

    def test_force_flag(self):
        args = blt.parse_args(["-f", *SOURCE])
        self.assertTrue(args.force)
        self.assertFalse(args.dry_run)

    def test_long_flags(self):
        self.assertTrue(blt.parse_args(["--dry-run", *SOURCE]).dry_run)
        self.assertTrue(blt.parse_args(["--force", *SOURCE]).force)

    def test_neither_flag_is_an_error(self):
        with self.assertRaises(SystemExit):
            blt.parse_args([*SOURCE])

    def test_both_flags_is_an_error(self):
        with self.assertRaises(SystemExit):
            blt.parse_args(["-n", "-f", *SOURCE])

    def test_quiet_flag_short_and_long(self):
        self.assertTrue(blt.parse_args(["-n", "-q", *SOURCE]).quiet)
        self.assertTrue(blt.parse_args(["-n", "--quiet", *SOURCE]).quiet)

    def test_quiet_defaults_to_false(self):
        self.assertFalse(blt.parse_args(["-n", *SOURCE]).quiet)

    def test_skip_matches_defaults_to_none(self):
        self.assertIsNone(blt.parse_args(["-n", *SOURCE]).skip_matches)

    def test_skip_matches_alone_is_empty_string(self):
        self.assertEqual(blt.parse_args(["-n", "--skip-matches", *SOURCE]).skip_matches, "")

    def test_skip_matches_equals_form(self):
        args = blt.parse_args(["-n", "--skip-matches=ci,agents", *SOURCE])
        self.assertEqual(args.skip_matches, "ci,agents")

    def test_skip_matches_space_form(self):
        # Python argparse's nargs='?' accepts the space form too, unlike
        # strict GNU getopt_long (which requires "=" for optional
        # arguments); we follow argparse's convention here.
        args = blt.parse_args(["-n", "--skip-matches", "ci,agents", *SOURCE])
        self.assertEqual(args.skip_matches, "ci,agents")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


class TestMain(unittest.TestCase):
    def setUp(self):
        self._env_patch = patch.dict(
            os.environ, {"GITHUB_TOKEN": "test-token", "GITHUB_REPOSITORY": "owner/repo"}
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()

    def test_exit_1_when_token_missing(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": ""}):
            result = blt.main(["-n", *SOURCE])
        self.assertEqual(result, 1)

    def test_exit_1_when_repository_missing(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": ""}):
            result = blt.main(["-n", *SOURCE])
        self.assertEqual(result, 1)

    def test_exit_1_when_fetch_fails(self):
        with patch.object(blt, "fetch_all_issues", side_effect=Exception("boom")):
            result = blt.main(["-n", *SOURCE])
        self.assertEqual(result, 1)

    def test_dry_run_does_not_apply(self):
        items = [_issue(1, "Fix the CI pipeline", [])]
        with patch.object(blt, "fetch_all_issues", return_value=items):
            with patch.object(blt, "apply_labels") as mock_apply:
                result = blt.main(["-n", *SOURCE])
        self.assertEqual(result, 0)
        mock_apply.assert_not_called()

    def test_force_applies(self):
        items = [_issue(1, "Fix the CI pipeline", [])]
        with patch.object(blt, "fetch_all_issues", return_value=items):
            with patch.object(blt, "apply_labels", return_value=True) as mock_apply:
                result = blt.main(["-f", *SOURCE])
        self.assertEqual(result, 0)
        mock_apply.assert_called_once()
        to_apply_arg = mock_apply.call_args[0][0]
        self.assertEqual(to_apply_arg[1], ["ci"])

    def test_force_exit_1_when_apply_labels_reports_failure(self):
        items = [_issue(1, "Fix the CI pipeline", [])]
        with patch.object(blt, "fetch_all_issues", return_value=items):
            with patch.object(blt, "apply_labels", return_value=False):
                result = blt.main(["-f", *SOURCE])
        self.assertEqual(result, 1)

    def test_force_exit_1_still_prints_report(self):
        items = [_issue(1, "Fix the CI pipeline", [])]
        with patch.object(blt, "fetch_all_issues", return_value=items):
            with patch.object(blt, "apply_labels", return_value=False):
                with patch("builtins.print") as mock_print:
                    result = blt.main(["-f", *SOURCE])
        self.assertEqual(result, 1)
        parsed = json.loads(mock_print.call_args_list[-1].args[0])
        self.assertIn("add", parsed)

    def test_prints_report(self):
        items = [_issue(1, "Fix the CI pipeline", [])]
        with patch.object(blt, "fetch_all_issues", return_value=items):
            with patch("builtins.print") as mock_print:
                blt.main(["-n", *SOURCE])
        # The report is the last of possibly several print() calls (apply_labels
        # would have printed one line per issue had -f been passed).
        printed = mock_print.call_args_list[-1].args[0]
        parsed = json.loads(printed)
        self.assertIn("add", parsed)

    def test_quiet_suppresses_the_report(self):
        items = [_issue(1, "Fix the CI pipeline", [])]
        with patch.object(blt, "fetch_all_issues", return_value=items):
            with patch("builtins.print") as mock_print:
                result = blt.main(["-n", "-q", *SOURCE])
        self.assertEqual(result, 0)
        mock_print.assert_not_called()

    def test_quiet_with_force_still_applies_but_prints_nothing(self):
        items = [_issue(1, "Fix the CI pipeline", [])]
        with patch.object(blt, "fetch_all_issues", return_value=items):
            with patch.object(blt, "apply_labels", return_value=True) as mock_apply:
                with patch("builtins.print") as mock_print:
                    result = blt.main(["-f", "-q", *SOURCE])
        self.assertEqual(result, 0)
        mock_apply.assert_called_once()
        mock_print.assert_not_called()

    def test_skip_matches_with_no_value_omits_match_key(self):
        items = [_issue(1, "Fix the CI pipeline", ["ci"])]
        with patch.object(blt, "fetch_all_issues", return_value=items):
            with patch("builtins.print") as mock_print:
                blt.main(["-n", "--skip-matches", *SOURCE])
        parsed = json.loads(mock_print.call_args_list[-1].args[0])
        self.assertNotIn("match", parsed)

    def test_skip_matches_with_value_filters_that_label_only(self):
        items = [
            _issue(1, "Fix the CI pipeline", ["ci"]),
            _issue(2, "Tighten guidance for agents", ["agents"]),
        ]
        with patch.object(blt, "fetch_all_issues", return_value=items):
            with patch("builtins.print") as mock_print:
                blt.main(["-n", "--skip-matches=ci", *SOURCE])
        parsed = json.loads(mock_print.call_args_list[-1].args[0])
        self.assertNotIn("ci", parsed["match"])
        self.assertEqual(parsed["match"]["agents"], [2])

    def test_no_skip_matches_leaves_match_untouched(self):
        items = [_issue(1, "Fix the CI pipeline", ["ci"])]
        with patch.object(blt, "fetch_all_issues", return_value=items):
            with patch("builtins.print") as mock_print:
                blt.main(["-n", *SOURCE])
        parsed = json.loads(mock_print.call_args_list[-1].args[0])
        self.assertEqual(parsed["match"]["ci"], [1])


# ---------------------------------------------------------------------------
# Where the rules come from
# ---------------------------------------------------------------------------


def _workflow(rules_json: str, skip_json: str | None = '["agents"]') -> str:
    """Return a workflow whose one step uses label-by-title with these inputs."""
    skip = f"\n          skip-on-pull-requests: '{skip_json}'" if skip_json is not None else ""
    rules = textwrap.indent(rules_json, " " * 12)
    return (
        "name: Label by title\n"
        "on:\n  issues:\n    types: [opened, edited]\n"
        "jobs:\n  label:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - uses: aunger/vcs-automation/label-by-title@"
        "0123456789abcdef0123456789abcdef01234567 # v1.2.0\n"
        "        with:\n"
        f"          rules: |\n{rules}{skip}\n"
    )


class TestRuleSource(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "label-by-title.yml"

    def write(self, text: str) -> Path:
        self.path.write_text(text, encoding="utf-8")
        return self.path

    def test_a_rule_source_is_required(self):
        with self.assertRaises(SystemExit):
            blt.parse_args(["-n"])

    def test_workflow_and_rules_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            blt.parse_args(["-n", "--workflow", "x.yml", "--rules", "{}"])

    def test_rules_flag_gives_rules_and_skip(self):
        rules, skip = blt.load_rules(blt.parse_args(["-n", *SOURCE]))
        self.assertEqual(list(RULES), list(rules))
        self.assertEqual(frozenset({"agents"}), skip)

    def test_rules_flag_without_skip_skips_nothing(self):
        _rules, skip = blt.load_rules(blt.parse_args(["-n", "--rules", GB4PC_RULES_JSON]))
        self.assertEqual(frozenset(), skip)

    def test_workflow_gives_the_steps_rules_and_skip(self):
        path = self.write(_workflow(json.dumps(json.loads(GB4PC_RULES_JSON), indent=2)))
        rules, skip = blt.load_rules(blt.parse_args(["-n", "--workflow", str(path)]))
        self.assertEqual(list(RULES), list(rules))
        self.assertEqual(
            {label: p.pattern for label, p in RULES.items()},
            {label: p.pattern for label, p in rules.items()},
        )
        self.assertEqual(frozenset({"agents"}), skip)

    def test_workflow_without_skip_skips_nothing(self):
        path = self.write(_workflow('{"ci": "ci"}', skip_json=None))
        _rules, skip = blt.load_rules(blt.parse_args(["-n", "--workflow", str(path)]))
        self.assertEqual(frozenset(), skip)

    def test_skip_flag_with_workflow_is_refused(self):
        path = self.write(_workflow('{"ci": "ci"}'))
        args = blt.parse_args(["-n", "--workflow", str(path), "--skip-on-pull-requests", '["ci"]'])
        with self.assertRaises(blt.lc.InputError):
            blt.load_rules(args)

    def test_workflow_step_without_rules_is_refused(self):
        path = self.write(
            "jobs:\n  j:\n    steps:\n      - uses: aunger/vcs-automation/label-by-title@main\n"
        )
        with self.assertRaises(blt.lc.InputError):
            blt.load_rules(blt.parse_args(["-n", "--workflow", str(path)]))


class TestMainRuleErrors(unittest.TestCase):
    def setUp(self):
        self._env_patch = patch.dict(
            os.environ, {"GITHUB_TOKEN": "test-token", "GITHUB_REPOSITORY": "owner/repo"}
        )
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)

    def assert_exit_1_without_fetching(self, argv: list[str]):
        with patch.object(blt, "fetch_all_issues") as mock_fetch:
            self.assertEqual(1, blt.main(argv))
        mock_fetch.assert_not_called()

    def test_invalid_rules(self):
        for rules in ("not json", "{}", '{"ci": "("}'):
            with self.subTest(rules=rules):
                self.assert_exit_1_without_fetching(["-n", "--rules", rules])

    def test_invalid_skip_list(self):
        self.assert_exit_1_without_fetching(
            ["-n", "--rules", GB4PC_RULES_JSON, "--skip-on-pull-requests", "{}"]
        )

    def test_missing_workflow_file(self):
        missing = str(Path(self._dir.name) / "missing.yml")
        self.assert_exit_1_without_fetching(["-n", "--workflow", missing])

    def test_workflow_with_no_label_by_title_step(self):
        path = Path(self._dir.name) / "other.yml"
        path.write_text(
            "jobs:\n  j:\n    steps:\n      - uses: actions/checkout@v7\n", encoding="utf-8"
        )
        self.assert_exit_1_without_fetching(["-n", "--workflow", str(path)])

    def test_force_applies_labels_in_the_rules_spelling(self):
        items = [_issue(1, "Fix the docs", [])]
        with patch.object(blt, "fetch_all_issues", return_value=items):
            with patch.object(blt.lc, "gh_api") as mock_api:
                with patch("builtins.print"):
                    self.assertEqual(0, blt.main(["-f", "-q", "--rules", '{"Docs": "docs"}']))
        self.assertEqual({"labels": ["Docs"]}, mock_api.call_args.kwargs["body"])


if __name__ == "__main__":
    unittest.main()
