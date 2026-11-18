#!/usr/bin/env python3
"""Unit tests for label_by_title.py.

The title cases are gallery-button-for-pixel-camera's, run against its table
(GB4PC_RULES below, with "agents" skipped on pull requests), so they exercise
the matching on a real table as well as the GB4PC table's own behavior.
"""

import json
import os
import re
import sys
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import label_by_title as lpt

lc = lpt.lc

# gallery-button-for-pixel-camera's label-by-title rules.
GB4PC_RULES = {
    "ci": [
        "(\\b|_)ci(\\b|_)",
        "(\\b|_)automat\\w+(\\b|_)",
        "(\\b|_)workflow\\w*",
        "(\\b|_)github[\\W_]*action\\w*",
        "(\\b|_)e2e\\w*",
        "(?=.*(\\b|_)merg\\w+)(?=.*((\\b|_)gat(ing|e[ds]?)(\\b|_)|(\\b|_)(un)?block\\w*|(\\b|_)prs?(\\b|_)))",
        "(\\b|_)pre-?flight\\w*",
        "(\\b|_)codeql(\\b|_)",
    ],
    "agents": [
        "(\\b|_)agent\\w*",
        "(\\b|_)dev_orchestration(\\b|_)",
        "(\\b|_)rules?(\\b|_)",
        "(\\b|_)attributions?(\\b|_)",
        "(\\b|_)bylines?(\\b|_)",
        "(?=.*(\\b|_)verif\\w+(\\b|_))(?=.*((\\b|_)planner(\\b|_)|(\\b|_)labels?(\\b|_)|(\\b|_)prs?(\\b|_)))",
        "(\\b|_)author(\\b|_)",
        "(\\b|_)reviewers?(\\b|_)",
        "(\\b|_)orchestrat\\w*",
        "(\\b|_)ci[\\W_]*monitor\\w*",
        "(\\b|_)sub[\\W_]?agent\\w*",
    ],
    "automated tests": [
        "(\\b|_)e2e(\\b|_)",
        "(\\b|_)unit(\\b|_)",
        "(\\b|_)test\\w*",
        "\\w*e2etest\\w*",
        "(\\b|_)pre-?flight\\w*",
        "(?-i:\\w*Test(\\b|_))",
    ],
}
GB4PC_RULES_JSON = json.dumps(GB4PC_RULES)
GB4PC_SKIP = frozenset({"agents"})
RULES = lpt.parse_rules(GB4PC_RULES_JSON)


def match(title: str, is_pull_request: bool = False) -> list[str]:
    """Return GB4PC's labels for *title*, as the action computes them."""
    return lpt.matching_labels(title, RULES, is_pull_request, GB4PC_SKIP)


# ---------------------------------------------------------------------------
# matching_labels tests
# ---------------------------------------------------------------------------


class TestMatchingLabelsCi(unittest.TestCase):
    def test_bare_ci_word_matches(self):
        self.assertIn("ci", match("Fix the CI pipeline"))

    def test_ci_prefixed_by_underscore_matches(self):
        self.assertIn("ci", match("Move ci_monitor.py into scripts/"))

    def test_ci_within_longer_word_does_not_match(self):
        self.assertNotIn("ci", match("Fix the specific config"))

    def test_automat_suffixes_match(self):
        self.assertIn("ci", match("Automate version sync"))
        self.assertIn("ci", match("Add automated coverage"))
        self.assertIn("ci", match("Validate automation-required labels"))

    def test_case_insensitive(self):
        self.assertIn("ci", match("fix the ci pipeline"))
        self.assertIn("ci", match("Fix The CI Pipeline"))

    def test_workflow_suffixes_match(self):
        self.assertIn("ci", match("Add workflow_dispatch trigger"))
        self.assertIn("ci", match("Daily workflow to archive stale issues"))

    def test_github_action_matches_with_separator_variants(self):
        self.assertIn("ci", match("GitHub Action: remove verified label"))
        self.assertIn("ci", match("Enforce label sets via GitHub Actions"))
        self.assertIn("ci", match("Fix github_action bug"))

    def test_e2e_suffix_matches_in_ci(self):
        self.assertIn("ci", match("Capture E2E test video on failure"))

    def test_e2e_without_leading_boundary_does_not_match_ci(self):
        # "E2E" inside "VisualE2ETest" has no boundary before it (preceded
        # by "l"), so the suffix-only e2e\w* rule doesn't fire.
        self.assertNotIn("ci", match("Add VisualE2Efixture helper"))

    def test_e2etest_camel_case_identifier_does_not_match_ci(self):
        # The e2etest identifier rule is testing-only by design; CI-auto-
        # filed test-failure issue titles don't get ci from this alone.
        self.assertNotIn(
            "ci",
            match("[GalleryButtonVisualE2ETest] test2a_emptyGalleryNoGreenAfterTap"),
        )

    def test_preflight_variants_match(self):
        self.assertIn("ci", match("Preflight: Pixel Launcher isn't responding"))
        self.assertIn("ci", match("CI pre-flight: replace fixed sleep"))

    def test_preflight_with_suffix_matches(self):
        # A trailing \w* keeps this consistent with the other ci-rule
        # patterns (e.g. automat\w+, e2e\w*), which allow a suffix rather
        # than requiring a trailing word boundary right after the term.
        self.assertIn("ci", match("Stop preflighting on every retry"))

    def test_codeql_matches(self):
        self.assertIn("ci", match("Add CodeQL workflow for scanning"))

    def test_merge_alone_does_not_match(self):
        # "merge" needs a companion word (gate/block/PR); bare merge/merging
        # is too generic on its own.
        self.assertNotIn("ci", match("Merge feature branches together"))

    def test_merge_with_gate_matches(self):
        self.assertIn("ci", match("Add to label-based merge gate"))
        self.assertIn("ci", match("Extend the merge gating logic"))

    def test_gate_does_not_match_gateway(self):
        # gat(ing|e[ds]?) is deliberately narrower than an open gate\w*, so
        # it doesn't sweep in unrelated words like "gateway".
        self.assertNotIn("ci", match("Merge feature via the new gateway service"))

    def test_merge_with_block_matches_either_order(self):
        self.assertIn("ci", match("GitHub Action: block merge when label present"))
        self.assertIn("ci", match("'Merging is blocked' after checks pass"))

    def test_merge_with_unblock_matches(self):
        # (un)?block\w* -- "unblock merges" has no boundary before "block"
        # for a plain block\w* rule, so the "un" prefix must be explicit.
        self.assertIn("ci", match("Report empty results to unblock merges"))

    def test_merge_with_pr_matches_either_order(self):
        self.assertIn("ci", match("(re PR #349) Disable CodeQL setup before merging"))

    def test_merge_without_any_companion_does_not_match(self):
        self.assertNotIn(
            "ci",
            match("File tracking issues for before-merging requirements instead of consulting"),
        )


class TestMatchingLabelsAgents(unittest.TestCase):
    def test_agent_word_matches(self):
        self.assertIn("agents", match("Add agent guidance for code reviews"))

    def test_agent_suffix_matches(self):
        self.assertIn("agents", match("Tighten guidance for agents"))

    def test_agent_prefixed_by_underscore_matches(self):
        self.assertIn("agents", match("Rename the_agent module"))

    def test_dev_orchestration_matches(self):
        self.assertIn("agents", match("Update dev_orchestration.md for clarity"))

    def test_rule_and_rules_match(self):
        self.assertIn("agents", match("Add orchestrator timestamp reporting rule"))
        self.assertIn("agents", match("Rework scope creep rules"))

    def test_ruled_does_not_match(self):
        self.assertNotIn("agents", match("The court ruled in our favor"))

    def test_attribution_and_byline_match(self):
        self.assertIn("agents", match("Claude attribution/byline settings"))
        self.assertIn("agents", match("Clear commit attribution in settings.json"))

    def test_verif_suffixes_match(self):
        self.assertIn("agents", match("Add PR verification agent instructions"))
        self.assertIn("agents", match("Test PR: verify remove-verified-on-push"))

    def test_verif_with_planner_companion_matches(self):
        self.assertIn("agents", match("Verification Planner should file tracking issues"))

    def test_verif_without_companion_does_not_match(self):
        # "verif\\w+" alone is too generic: it sweeps in unrelated Gradle
        # dependency-verification work (#714, #760) without a companion word.
        self.assertNotIn(
            "agents",
            match("Harden the Gradle build with dependency verification metadata"),
        )
        self.assertNotIn(
            "agents",
            match(
                "Migrate to Gradle 9.x: generate the wrapper and verification metadata"
                " via a manual CI workflow"
            ),
        )

    def test_author_exact_word_matches(self):
        self.assertIn("agents", match("Empower Author to file issues for review requests"))

    def test_authors_does_not_match(self):
        self.assertNotIn("agents", match("List all the authors of this project"))

    def test_review_requires_suffix(self):
        self.assertIn("agents", match("Clarify Reviewer account sharing"))
        self.assertIn("agents", match("Add a reviewers checklist"))
        self.assertNotIn("agents", match("Leave a review"))

    def test_reviewing_and_reviewed_do_not_match(self):
        self.assertNotIn("agents", match("Reviewing PRs takes too long"))
        self.assertNotIn("agents", match("The reviewed manuscript needs edits"))
        self.assertNotIn("agents", match("Collect reviews from beta testers"))

    def test_orchestrat_suffixes_match(self):
        self.assertIn("agents", match("Orchestrator directions: CI review loop"))
        self.assertIn("agents", match("Build an /orchestrate Claude Code plugin"))

    def test_ci_monitor_matches_with_separator_variants(self):
        self.assertIn("agents", match("CI Monitor: handle two lagging endpoints"))
        self.assertIn("agents", match("Speed up ci_monitor's poll interval"))
        self.assertIn("agents", match("Refactor CIMonitor internals"))

    def test_subagent_concatenated_matches(self):
        self.assertIn("agents", match("Make subagent delegation dependable"))
        self.assertIn("agents", match("Replace CiWatcher subagents with a Monitor loop"))

    def test_subagent_with_single_separator_matches(self):
        self.assertIn("agents", match("Fix the sub-agent handoff"))
        self.assertIn("agents", match("Fix the sub agent handoff"))
        self.assertIn("agents", match("Fix the sub_agent handoff"))


class TestMatchingLabelsAutomatedTests(unittest.TestCase):
    def test_e2e_matches(self):
        self.assertIn("automated tests", match("Add E2E instrumented test"))

    def test_e2e_within_camel_case_identifier_does_not_match_the_plain_e2e_rule(self):
        # No boundary surrounds "E2E" inside "VisualE2Efixture", and it's
        # not followed by "Test", so neither e2e rule fires.
        self.assertEqual(match("Add VisualE2Efixture helper"), [])

    def test_e2etest_camel_case_identifier_matches_automated_tests(self):
        # The dedicated e2etest identifier rule (both sides open) catches
        # what the plain \be2e\b rule can't reach inside CamelCase names.
        self.assertIn("automated tests", match("Add GalleryButtonVisualE2ETest"))

    def test_preflight_matches_automated_tests(self):
        self.assertIn(
            "automated tests",
            match("CI pre-flight: MockCameraActivity ready signal"),
        )

    def test_preflight_with_suffix_matches_automated_tests(self):
        self.assertIn("automated tests", match("Stop preflighting on every retry"))

    def test_camel_case_test_suffix_matches_case_sensitively(self):
        self.assertIn("automated tests", match("Add Gaussian-blur noise to ShapeMatcherTest"))
        self.assertIn(
            "automated tests",
            match("Six instrumented (androidTest) classes are skipped"),
        )

    def test_lowercase_camel_test_identifier_does_not_match(self):
        # "shapematchertest" (all lowercase, no separators) has no leading
        # boundary before its final "test" for the ordinary test\w* rule
        # (preceded by "r"), so only a case-insensitive version of the new
        # \w*Test(\b|_) rule could catch it--and it must not.
        self.assertEqual(match("shapematchertest"), [])

    def test_common_english_words_ending_in_test_do_not_match(self):
        # Each of these ends in the literal substring "test" with no
        # boundary before it, so only a case-insensitive version of the new
        # \w*Test(\b|_) rule could wrongly catch them. If this test starts
        # failing, the case-sensitivity of that rule has regressed.
        result = match("Set the latest, fastest, greatest contest and protest")
        self.assertEqual(result, [])

    def test_unit_exact_word_matches(self):
        self.assertIn("automated tests", match("Skip Python and shell unit tests"))

    def test_units_does_not_match_unit_rule(self):
        # "units" alone (no "test") should not match the strict \bunit\b rule.
        self.assertNotIn("automated tests", match("Convert distance units to metric"))

    def test_test_suffixes_match(self):
        self.assertIn("automated tests", match("Add noisy tests for coverage"))
        self.assertIn("automated tests", match("Add :testgallery module"))

    def test_test_prefixed_by_underscore_matches(self):
        self.assertIn(
            "automated tests",
            match("Speed up ci_monitor's poll interval unit_tests"),
        )


class TestMatchingLabelsCombined(unittest.TestCase):
    def test_multiple_labels_from_one_title(self):
        result = match("Add CiWatcher agent and fix Reviewer CI-polling loop")
        self.assertIn("ci", result)
        self.assertIn("agents", result)
        self.assertNotIn("automated tests", result)

    def test_no_match_returns_empty_list(self):
        self.assertEqual(match("Set overlay default position to y=75%"), [])


class TestFileDeterminedLabelSuppression(unittest.TestCase):
    def test_issue_path_is_the_default_and_still_gets_agents(self):
        self.assertIn("agents", match("Add PR verification agent instructions"))

    def test_pull_request_path_suppresses_agents(self):
        self.assertNotIn(
            "agents",
            match("Add PR verification agent instructions", is_pull_request=True),
        )

    def test_pull_request_path_keeps_every_other_matching_label(self):
        title = "Add CiWatcher agent and fix Reviewer CI-polling loop"
        self.assertEqual(match(title, is_pull_request=True), ["ci"])


# ---------------------------------------------------------------------------
# main() tests
# ---------------------------------------------------------------------------


class TestMain(unittest.TestCase):
    def setUp(self):
        self._env_patch = patch.dict(
            os.environ,
            {
                "GITHUB_TOKEN": "test-token",
                "GITHUB_REPOSITORY": "owner/repo",
                "ISSUE_NUMBER": "42",
                "ISSUE_TITLE": "Fix the CI pipeline",
                "RULES": GB4PC_RULES_JSON,
                "SKIP_ON_PULL_REQUESTS": '["agents"]',
            },
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()

    def test_exit_1_when_token_missing(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": ""}):
            result = lpt.main()
        self.assertEqual(result, 1)

    def test_exit_1_when_repository_missing(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": ""}):
            result = lpt.main()
        self.assertEqual(result, 1)

    def test_exit_1_when_issue_number_missing(self):
        with patch.dict(os.environ, {"ISSUE_NUMBER": ""}):
            result = lpt.main()
        self.assertEqual(result, 1)

    def test_exit_1_when_issue_number_non_integer(self):
        with patch.dict(os.environ, {"ISSUE_NUMBER": "abc"}):
            result = lpt.main()
        self.assertEqual(result, 1)

    def test_no_api_call_when_title_matches_nothing(self):
        with patch.dict(os.environ, {"ISSUE_TITLE": "Set overlay default position"}):
            with patch.object(lpt.lc, "gh_api") as mock_api:
                result = lpt.main()
        self.assertEqual(result, 0)
        mock_api.assert_not_called()

    def test_posts_matching_labels(self):
        with patch.object(lpt.lc, "gh_api") as mock_api:
            result = lpt.main()
        self.assertEqual(result, 0)
        mock_api.assert_called_once()
        args, kwargs = mock_api.call_args
        self.assertEqual(args[0], "repos/owner/repo/issues/42/labels")
        self.assertEqual(kwargs["method"], "POST")
        self.assertEqual(kwargs["body"], {"labels": ["ci"]})

    def test_posts_multiple_matching_labels(self):
        with patch.dict(
            os.environ, {"ISSUE_TITLE": "Add CiWatcher agent and fix Reviewer CI-polling loop"}
        ):
            with patch.object(lpt.lc, "gh_api") as mock_api:
                result = lpt.main()
        self.assertEqual(result, 0)
        body = mock_api.call_args[1]["body"]
        self.assertIn("ci", body["labels"])
        self.assertIn("agents", body["labels"])

    def test_is_pull_request_true_omits_agents_from_the_post(self):
        with patch.dict(
            os.environ,
            {
                "ISSUE_TITLE": "Add CiWatcher agent and fix Reviewer CI-polling loop",
                "IS_PULL_REQUEST": "true",
            },
        ):
            with patch.object(lpt.lc, "gh_api") as mock_api:
                result = lpt.main()
        self.assertEqual(result, 0)
        self.assertEqual(mock_api.call_args[1]["body"], {"labels": ["ci"]})

    def test_is_pull_request_is_case_and_whitespace_tolerant(self):
        # GitHub Actions renders the boolean expression as "true"/"false",
        # but a hand-run invocation may not be so tidy.
        for value in ("TRUE", " true "):
            with self.subTest(value=value):
                with patch.dict(
                    os.environ,
                    {
                        "ISSUE_TITLE": "Add CiWatcher agent and fix Reviewer CI-polling loop",
                        "IS_PULL_REQUEST": value,
                    },
                ):
                    with patch.object(lpt.lc, "gh_api") as mock_api:
                        lpt.main()
                self.assertEqual(mock_api.call_args[1]["body"], {"labels": ["ci"]})

    def test_omitted_or_false_is_pull_request_behaves_as_before(self):
        # Fail open: a caller that forgets the variable labels a PR from its
        # title exactly as it did before, and the file signal removes the
        # label on the next push.
        for env in ({}, {"IS_PULL_REQUEST": ""}, {"IS_PULL_REQUEST": "false"}):
            with self.subTest(env=env):
                with patch.dict(
                    os.environ,
                    {
                        "ISSUE_TITLE": "Add CiWatcher agent and fix Reviewer CI-polling loop",
                        **env,
                    },
                ):
                    with patch.object(lpt.lc, "gh_api") as mock_api:
                        lpt.main()
                body = mock_api.call_args[1]["body"]
                self.assertIn("agents", body["labels"])
                self.assertIn("ci", body["labels"])

    def test_no_post_when_agents_was_the_only_match_on_a_pull_request(self):
        with patch.dict(
            os.environ,
            {
                "ISSUE_TITLE": "Tighten guidance for agents",
                "IS_PULL_REQUEST": "true",
            },
        ):
            with patch.object(lpt.lc, "gh_api") as mock_api:
                result = lpt.main()
        self.assertEqual(result, 0)
        mock_api.assert_not_called()

    def test_exit_1_on_http_error(self):
        error = urllib.error.HTTPError(url=None, code=422, msg="Unprocessable", hdrs=None, fp=None)
        with patch.object(lpt.lc, "gh_api", side_effect=error):
            result = lpt.main()
        self.assertEqual(result, 1)

    def test_exit_1_on_url_error(self):
        error = urllib.error.URLError("network error")
        with patch.object(lpt.lc, "gh_api", side_effect=error):
            result = lpt.main()
        self.assertEqual(result, 1)


# ---------------------------------------------------------------------------
# parse_rules tests
# ---------------------------------------------------------------------------


class TestParseRules(unittest.TestCase):
    def assert_refused(self, text: str, fragment: str = ""):
        with self.assertRaises(lc.InputError) as caught:
            lpt.parse_rules(text)
        self.assertIn("RULES", str(caught.exception))
        if fragment:
            self.assertIn(fragment, str(caught.exception))

    def test_blank_is_refused_as_required(self):
        self.assert_refused("", "required")
        self.assert_refused("   \n")

    def test_invalid_json_is_refused(self):
        self.assert_refused("{ci: x}", "not valid JSON")

    def test_non_object_is_refused(self):
        self.assert_refused('["ci"]')
        self.assert_refused('"ci"')

    def test_empty_object_is_refused(self):
        self.assert_refused("{}")

    def test_blank_label_is_refused(self):
        self.assert_refused('{" ": "x"}')

    def test_label_listed_twice_in_different_case_is_refused(self):
        self.assert_refused('{"CI": "a", "ci": "b"}', "listed twice")

    def test_empty_array_is_refused(self):
        self.assert_refused('{"ci": []}')

    def test_empty_regex_is_refused(self):
        self.assert_refused('{"ci": ""}')
        self.assert_refused('{"ci": ["a", ""]}')

    def test_non_string_regex_is_refused(self):
        self.assert_refused('{"ci": 1}')
        self.assertRaises(lc.InputError, lpt.parse_rules, '{"ci": ["a", 2]}')

    def test_bad_regex_is_refused_naming_the_label(self):
        self.assert_refused('{"ci": "("}', "'ci'")

    def test_a_single_string_is_one_regex(self):
        rules = lpt.parse_rules('{"docs": "readme"}')
        self.assertEqual(["docs"], lpt.matching_labels("Fix the README", rules))

    def test_array_entries_are_alternatives(self):
        rules = lpt.parse_rules('{"docs": ["readme", "changelog"]}')
        self.assertEqual(["docs"], lpt.matching_labels("Update the changelog", rules))
        self.assertEqual([], lpt.matching_labels("Update the build", rules))

    # parse_rules wraps each alternative in (?:...) and joins them, which still
    # compiles "(a" and "b)" as "(?:(a)|(?:b))". Remove this decorator once
    # parse_rules compiles each alternative on its own first.
    def test_each_alternative_must_be_a_whole_regex_on_its_own(self):
        # Joined, "(a" and "b)" read as one valid regex; each alone is not.
        self.assert_refused('{"x": ["(a", "b)"]}', "'x'")

    def test_anchors_apply_within_their_own_alternative(self):
        rules = lpt.parse_rules('{"x": ["^start", "end$"]}')
        self.assertEqual([], lpt.matching_labels("no start or end here", rules))
        self.assertEqual(["x"], lpt.matching_labels("start here", rules))

    def test_matching_is_case_insensitive(self):
        rules = lpt.parse_rules('{"docs": "readme"}')
        self.assertEqual(["docs"], lpt.matching_labels("READme", rules))

    def test_inline_flag_opts_out_of_case_insensitivity(self):
        rules = lpt.parse_rules('{"x": "(?-i:Test)"}')
        self.assertEqual(["x"], lpt.matching_labels("FooTest", rules))
        self.assertEqual([], lpt.matching_labels("footest", rules))

    def test_labels_keep_the_spelling_given_and_rule_order(self):
        rules = lpt.parse_rules('{"Docs": "readme", "CI": "ci"}')
        self.assertEqual(["Docs", "CI"], lpt.matching_labels("ci readme", rules))

    def test_patterns_are_compiled_regexes(self):
        for pattern in RULES.values():
            self.assertIsInstance(pattern, re.Pattern)

    def test_the_gb4pc_fixture_parses_to_its_three_labels(self):
        self.assertEqual(["ci", "agents", "automated tests"], list(RULES))


class TestSkipOnPullRequests(unittest.TestCase):
    def test_skip_applies_only_to_pull_requests(self):
        rules = lpt.parse_rules('{"agents": "agent"}')
        skip = frozenset({"agents"})
        self.assertEqual(["agents"], lpt.matching_labels("agent work", rules, False, skip))
        self.assertEqual([], lpt.matching_labels("agent work", rules, True, skip))

    def test_skip_compares_case_insensitively(self):
        rules = lpt.parse_rules('{"Agents": "agent"}')
        skip = lc.parse_label_list("SKIP_ON_PULL_REQUESTS", '["AGENTS"]')
        self.assertEqual([], lpt.matching_labels("agent work", rules, True, skip))

    def test_no_skip_list_skips_nothing(self):
        rules = lpt.parse_rules('{"agents": "agent"}')
        self.assertEqual(["agents"], lpt.matching_labels("agent work", rules, True))


class TestMainTables(unittest.TestCase):
    def setUp(self):
        self._env_patch = patch.dict(
            os.environ,
            {
                "GITHUB_TOKEN": "test-token",
                "GITHUB_REPOSITORY": "owner/repo",
                "ISSUE_NUMBER": "42",
                "ISSUE_TITLE": "Tighten guidance for agents",
                "IS_PULL_REQUEST": "true",
                "RULES": GB4PC_RULES_JSON,
                "SKIP_ON_PULL_REQUESTS": '["agents"]',
            },
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()

    def test_exit_1_and_no_call_when_rules_missing(self):
        with patch.dict(os.environ, {"RULES": ""}):
            with patch.object(lc, "gh_api") as mock_api:
                self.assertEqual(1, lpt.main())
        mock_api.assert_not_called()

    def test_exit_1_and_no_call_when_rules_invalid(self):
        for value in ("not json", "[]", '{"ci": "("}'):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"RULES": value}):
                    with patch.object(lc, "gh_api") as mock_api:
                        self.assertEqual(1, lpt.main())
                mock_api.assert_not_called()

    def test_exit_1_and_no_call_when_skip_list_invalid(self):
        for value in ("not json", '{"agents": 1}', '[""]', "[1]"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"SKIP_ON_PULL_REQUESTS": value}):
                    with patch.object(lc, "gh_api") as mock_api:
                        self.assertEqual(1, lpt.main())
                mock_api.assert_not_called()

    def test_unset_skip_list_skips_nothing_on_a_pull_request(self):
        with patch.dict(os.environ, {"SKIP_ON_PULL_REQUESTS": ""}):
            with patch.object(lc, "gh_api") as mock_api:
                self.assertEqual(0, lpt.main())
        self.assertEqual({"labels": ["agents"]}, mock_api.call_args.kwargs["body"])

    def test_transient_error_is_retried_by_the_shared_gh_api(self):
        error = urllib.error.HTTPError(url=None, code=502, msg="Bad Gateway", hdrs=None, fp=None)
        with patch.dict(os.environ, {"IS_PULL_REQUEST": "false"}):
            with patch.object(lc, "_gh_api_once", side_effect=[error, None]) as mock_once:
                with patch.object(lc.time, "sleep"):
                    self.assertEqual(0, lpt.main())
        self.assertEqual(2, mock_once.call_count)


if __name__ == "__main__":
    unittest.main()
