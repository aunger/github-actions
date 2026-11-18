#!/usr/bin/env python3
"""Unit tests for propagate_issue_labels.py."""

import json
import os
import sys
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))
import propagate_issue_labels as pil

lc = pil.lc

# A caller's tables, as one would pass them to the action.
EXCLUDED_LABELS = [
    "orchestrate",
    "orchestrating",
    "verification needed",
    "verified",
    "changes requested",
    "changes done",
    "agents",
]
EXCLUSIVE_SETS = [
    ["p1", "p2", "p3"],
    ["verification needed", "verified"],
    ["changes requested", "changes done"],
    ["orchestrate", "orchestrating"],
    [f"snooze {days} days" for days in (3, 7, 14, 30, 90, 180)],
]
EXCLUSIVE_PREFIXES = ["c-a-", "c-r-", "test-failure"]

CONFIG = pil.Config(
    frozenset(EXCLUDED_LABELS),
    lc.ExclusiveRules([frozenset(s) for s in EXCLUSIVE_SETS], EXCLUSIVE_PREFIXES),
)


def propagate(current: list[str], issue_labels: list[str]):
    """labels_to_propagate under the fixture tables."""
    return pil.labels_to_propagate(current, issue_labels, CONFIG.excluded_labels, CONFIG.exclusive)


def _closing_issues_payload(issues: list[tuple[int, str, list[str]]]) -> dict:
    """Build a GraphQL response for the given (number, repo, labels) issues."""
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "closingIssuesReferences": {
                        "nodes": [
                            {
                                "number": number,
                                "repository": {"nameWithOwner": repo},
                                "labels": {"nodes": [{"name": lbl} for lbl in labels]},
                            }
                            for number, repo, labels in issues
                        ]
                    }
                }
            }
        }
    }


# ---------------------------------------------------------------------------
# graphql_query tests
# ---------------------------------------------------------------------------


class TestGraphqlQuery(unittest.TestCase):
    def _make_response(self, body: bytes) -> MagicMock:
        resp = MagicMock()
        resp.read.return_value = body
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_returns_data_on_success(self):
        payload = {"data": {"repository": {"pullRequest": None}}}
        response = self._make_response(json.dumps(payload).encode())
        with patch("urllib.request.urlopen", return_value=response):
            result = pil.graphql_query("query {}", {}, "tok")
        self.assertEqual(result, payload["data"])

    def test_a_502_is_retried(self):
        payload = {"data": {"x": 1}}
        error = urllib.error.HTTPError(
            "https://api.github.com/graphql", 502, "Bad Gateway", {}, None
        )
        with patch.object(pil.lc.time, "sleep"):
            with patch(
                "urllib.request.urlopen",
                side_effect=[error, self._make_response(json.dumps(payload).encode())],
            ) as mock_urlopen:
                self.assertEqual({"x": 1}, pil.graphql_query("query {}", {}, "tok"))
        self.assertEqual(2, mock_urlopen.call_count)

    def test_a_read_timeout_is_retried_since_a_query_changes_nothing(self):
        payload = {"data": {}}
        with patch.object(pil.lc.time, "sleep"):
            with patch(
                "urllib.request.urlopen",
                side_effect=[
                    TimeoutError("read timed out"),
                    self._make_response(json.dumps(payload).encode()),
                ],
            ) as mock_urlopen:
                pil.graphql_query("query {}", {}, "tok")
        self.assertEqual(2, mock_urlopen.call_count)

    def test_a_persistent_read_timeout_is_a_url_error(self):
        with patch.object(pil.lc.time, "sleep"):
            with patch("urllib.request.urlopen", side_effect=TimeoutError("read timed out")):
                with self.assertRaises(urllib.error.URLError):
                    pil.graphql_query("query {}", {}, "tok")

    def test_an_answer_that_is_not_json_is_a_runtime_error(self):
        for body in (b"<html>502</html>", b"[1, 2]"):
            with self.subTest(body=body):
                with patch("urllib.request.urlopen", return_value=self._make_response(body)):
                    with self.assertRaises(RuntimeError):
                        pil.graphql_query("query {}", {}, "tok")

    def test_propagate_to_pr_reports_an_answer_that_is_not_json(self):
        with patch("urllib.request.urlopen", return_value=self._make_response(b"<html>")):
            with patch("sys.stderr"):
                self.assertFalse(pil.propagate_to_pr("o", "r", "o/r", 5, "tok", pil.Config()))

    def test_raises_on_graphql_errors(self):
        payload = {"errors": [{"message": "Could not resolve to a PullRequest"}]}
        response = self._make_response(json.dumps(payload).encode())
        with patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(RuntimeError):
                pil.graphql_query("query {}", {}, "tok")

    def test_sends_bearer_auth_header(self):
        payload = {"data": {}}
        response = self._make_response(json.dumps(payload).encode())
        captured = {}

        def fake_urlopen(req, *_args, **_kwargs):
            captured["request"] = req
            return response

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            pil.graphql_query("query {}", {"a": 1}, "sekret")

        self.assertEqual(captured["request"].get_header("Authorization"), "Bearer sekret")
        self.assertEqual(captured["request"].get_full_url(), "https://api.github.com/graphql")

    def test_sends_the_github_headers_a_json_body_and_a_timeout(self):
        response = self._make_response(json.dumps({"data": {}}).encode())
        captured = {}

        def fake_urlopen(req, *args, **kwargs):
            captured["request"] = req
            captured["data"] = args[0] if args else kwargs.get("data")
            captured["timeout"] = kwargs.get("timeout")
            return response

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            pil.graphql_query("query Q {}", {"number": 7}, "sekret")

        request = captured["request"]
        self.assertEqual("POST", request.get_method())
        self.assertEqual("application/vnd.github+json", request.get_header("Accept"))
        self.assertEqual("2022-11-28", request.get_header("X-github-api-version"))
        self.assertEqual("application/json", request.get_header("Content-type"))
        self.assertEqual(
            {"query": "query Q {}", "variables": {"number": 7}}, json.loads(captured["data"])
        )
        self.assertEqual(pil.TIMEOUT_SECONDS, captured["timeout"])

    def test_returns_empty_dict_when_data_is_null(self):
        response = self._make_response(json.dumps({"data": None}).encode())
        with patch("urllib.request.urlopen", return_value=response):
            self.assertEqual({}, pil.graphql_query("query {}", {}, "tok"))


# ---------------------------------------------------------------------------
# fetch_closing_issue_labels tests
# ---------------------------------------------------------------------------


class TestFetchClosingIssueLabels(unittest.TestCase):
    def test_returns_labels_from_same_repo_issues(self):
        payload = _closing_issues_payload(
            [(1, "owner/repo", ["p1", "bug"]), (2, "owner/repo", ["ci"])]
        )
        with patch.object(pil, "graphql_query", return_value=payload["data"]):
            result = pil.fetch_closing_issue_labels("owner", "repo", 42, "tok")
        self.assertEqual(result, ["p1", "bug", "ci"])

    def test_skips_cross_repo_issues(self):
        payload = _closing_issues_payload(
            [(1, "owner/repo", ["p1"]), (2, "other-owner/other-repo", ["p2"])]
        )
        with patch.object(pil, "graphql_query", return_value=payload["data"]):
            result = pil.fetch_closing_issue_labels("owner", "repo", 42, "tok")
        self.assertEqual(result, ["p1"])

    def test_returns_empty_when_no_pull_request(self):
        data = {"repository": {"pullRequest": None}}
        with patch.object(pil, "graphql_query", return_value=data):
            result = pil.fetch_closing_issue_labels("owner", "repo", 42, "tok")
        self.assertEqual(result, [])

    def test_returns_empty_when_no_closing_issues(self):
        data = {"repository": {"pullRequest": {"closingIssuesReferences": {"nodes": []}}}}
        with patch.object(pil, "graphql_query", return_value=data):
            result = pil.fetch_closing_issue_labels("owner", "repo", 42, "tok")
        self.assertEqual(result, [])

    def test_returns_empty_when_closing_issue_has_no_labels(self):
        payload = _closing_issues_payload([(1, "owner/repo", [])])
        with patch.object(pil, "graphql_query", return_value=payload["data"]):
            result = pil.fetch_closing_issue_labels("owner", "repo", 42, "tok")
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# labels_to_propagate tests
# ---------------------------------------------------------------------------


class TestLabelsToPropagate(unittest.TestCase):
    def test_propagates_non_conflicting_labels(self):
        to_add, skipped, excluded = propagate([], ["p1", "bug"])
        self.assertEqual(to_add, ["p1", "bug"])
        self.assertEqual(skipped, [])
        self.assertEqual(excluded, [])

    def test_skips_labels_already_on_pr(self):
        to_add, skipped, excluded = propagate(["bug"], ["bug", "p1"])
        self.assertEqual(to_add, ["p1"])
        self.assertEqual(skipped, [])
        self.assertEqual(excluded, [])

    def test_already_present_check_is_case_insensitive(self):
        to_add, skipped, excluded = propagate(["Bug"], ["bug", "p1"])
        self.assertEqual(to_add, ["p1"])
        self.assertEqual(skipped, [])
        self.assertEqual(excluded, [])

    def test_deduplicates_labels_from_multiple_issues(self):
        to_add, skipped, excluded = propagate([], ["p1", "p1", "bug"])
        self.assertEqual(to_add, ["p1", "bug"])
        self.assertEqual(skipped, [])
        self.assertEqual(excluded, [])

    def test_skips_label_that_would_evict_existing_pr_label_fixed_set(self):
        # PR already carries p2; propagating p1 would let the mutual-exclusion
        # workflow remove p2, so p1 must be skipped instead.
        to_add, skipped, excluded = propagate(["p2"], ["p1"])
        self.assertEqual(to_add, [])
        self.assertEqual(skipped, ["p1"])
        self.assertEqual(excluded, [])

    def test_skips_label_that_would_evict_existing_pr_label_prefix_group(self):
        to_add, skipped, excluded = propagate(["c-a-opus"], ["c-a-haiku"])
        self.assertEqual(to_add, [])
        self.assertEqual(skipped, ["c-a-haiku"])
        self.assertEqual(excluded, [])

    def test_non_conflicting_prefix_label_still_propagates(self):
        to_add, skipped, excluded = propagate(["c-a-opus"], ["c-r-haiku"])
        self.assertEqual(to_add, ["c-r-haiku"])
        self.assertEqual(skipped, [])
        self.assertEqual(excluded, [])

    def test_first_accepted_candidate_blocks_a_later_conflicting_one(self):
        # No real PR label yet, but p1 is accepted first (from one linked
        # issue) and then blocks p2 (from another linked issue) within the
        # same run.
        to_add, skipped, excluded = propagate([], ["p1", "p2"])
        self.assertEqual(to_add, ["p1"])
        self.assertEqual(skipped, ["p2"])
        self.assertEqual(excluded, [])

    def test_labels_outside_any_exclusive_group_never_conflict(self):
        to_add, skipped, excluded = propagate(["automated tests"], ["ci", "bug"])
        self.assertEqual(to_add, ["ci", "bug"])
        self.assertEqual(skipped, [])
        self.assertEqual(excluded, [])

    def test_empty_issue_labels_is_a_no_op(self):
        to_add, skipped, excluded = propagate(["p1"], [])
        self.assertEqual(to_add, [])
        self.assertEqual(skipped, [])
        self.assertEqual(excluded, [])

    def test_without_tables_everything_new_propagates(self):
        to_add, skipped, excluded = pil.labels_to_propagate(["p2"], ["p1", "orchestrating"])
        self.assertEqual(to_add, ["p1", "orchestrating"])
        self.assertEqual(skipped, [])
        self.assertEqual(excluded, [])

    def test_a_label_in_two_sets_conflicts_through_either(self):
        rules = lc.ExclusiveRules([frozenset({"a", "b"}), frozenset({"a", "c"})], [])
        to_add, skipped, _excluded = pil.labels_to_propagate(["c"], ["a"], frozenset(), rules)
        self.assertEqual(to_add, [])
        self.assertEqual(skipped, ["a"])

    # -- excluded labels ---------------------------------------------------

    def test_excludes_orchestrating_even_with_no_pr_side_conflict(self):
        # The PR carries none of {orchestrate, orchestrating}, so a
        # mutual-exclusion-only guard would let this through and re-block
        # the merge gate.
        to_add, skipped, excluded = propagate([], ["orchestrating"])
        self.assertEqual(to_add, [])
        self.assertEqual(skipped, [])
        self.assertEqual(excluded, ["orchestrating"])

    def test_excludes_every_excluded_label(self):
        for label in EXCLUDED_LABELS:
            with self.subTest(label=label):
                to_add, skipped, excluded = propagate([], [label])
                self.assertEqual(to_add, [])
                self.assertEqual(skipped, [])
                self.assertEqual(excluded, [label])

    def test_exclusion_is_case_insensitive(self):
        to_add, _skipped, excluded = propagate([], ["Orchestrating"])
        self.assertEqual(to_add, [])
        self.assertEqual(excluded, ["Orchestrating"])

    def test_excluded_labels_left_out_alongside_ordinary_labels(self):
        to_add, skipped, excluded = propagate([], ["p1", "orchestrating", "ci"])
        self.assertEqual(to_add, ["p1", "ci"])
        self.assertEqual(skipped, [])
        self.assertEqual(excluded, ["orchestrating"])

    def test_excluded_label_deduplicated_across_issues(self):
        _to_add, _skipped, excluded = propagate([], ["orchestrating", "orchestrating"])
        self.assertEqual(excluded, ["orchestrating"])

    def test_excludes_agents_from_propagation(self):
        to_add, skipped, excluded = propagate([], ["agents", "p1"])
        self.assertEqual(to_add, ["p1"])
        self.assertEqual(skipped, [])
        self.assertEqual(excluded, ["agents"])

    def test_excludes_agents_even_when_the_pr_already_carries_it(self):
        # Exclusion is checked ahead of the already-present check.
        to_add, _skipped, excluded = propagate(["agents"], ["agents"])
        self.assertEqual(to_add, [])
        self.assertEqual(excluded, ["agents"])

    def test_agents_exclusion_is_case_insensitive(self):
        to_add, _skipped, excluded = propagate([], ["Agents"])
        self.assertEqual(to_add, [])
        self.assertEqual(excluded, ["Agents"])

    def test_exclusion_does_not_consume_a_skip_slot(self):
        # An excluded label and a genuinely conflicting label in the same run
        # are reported in their own distinct lists, not conflated.
        to_add, skipped, excluded = propagate(["p2"], ["orchestrating", "p1"])
        self.assertEqual(to_add, [])
        self.assertEqual(skipped, ["p1"])
        self.assertEqual(excluded, ["orchestrating"])


# ---------------------------------------------------------------------------
# main() tests
# ---------------------------------------------------------------------------


class TestMain(unittest.TestCase):
    def _env(self, **overrides):
        env = {
            "GITHUB_TOKEN": "tok",
            "GITHUB_REPOSITORY": "owner/repo",
            "PR_NUMBER": "42",
            "EXCLUDED_LABELS": json.dumps(EXCLUDED_LABELS),
            "EXCLUSIVE_SETS": json.dumps(EXCLUSIVE_SETS),
            "EXCLUSIVE_PREFIXES": json.dumps(EXCLUSIVE_PREFIXES),
        }
        env.update(overrides)
        return env

    def test_missing_token_returns_1(self):
        with patch.dict(os.environ, self._env(GITHUB_TOKEN=""), clear=True):
            self.assertEqual(pil.main(), 1)

    def test_missing_repo_returns_1(self):
        with patch.dict(os.environ, self._env(GITHUB_REPOSITORY=""), clear=True):
            self.assertEqual(pil.main(), 1)

    def test_missing_pr_number_returns_1(self):
        with patch.dict(os.environ, self._env(PR_NUMBER=""), clear=True):
            self.assertEqual(pil.main(), 1)

    def test_malformed_repo_returns_1(self):
        for repo in ("not-a-slug", "/repo", "owner/", "owner/repo/extra"):
            with self.subTest(repo=repo):
                with patch.dict(os.environ, self._env(GITHUB_REPOSITORY=repo), clear=True):
                    with patch.object(pil, "propagate_to_pr") as mock_propagate:
                        with patch("builtins.print"):
                            self.assertEqual(pil.main(), 1)
                mock_propagate.assert_not_called()

    def test_invalid_table_returns_1(self):
        cases = [
            ("EXCLUDED_LABELS", "not json"),
            ("EXCLUDED_LABELS", '{"agents": true}'),
            ("EXCLUDED_LABELS", "[1]"),
            ("EXCLUSIVE_SETS", "not json"),
            ("EXCLUSIVE_SETS", '["p1", "p2"]'),
            ("EXCLUSIVE_SETS", '[["p1"]]'),
            ("EXCLUSIVE_SETS", '[["p1", "P1"]]'),
            ("EXCLUSIVE_PREFIXES", '"c-a-"'),
            ("EXCLUSIVE_PREFIXES", '[""]'),
        ]
        for name, value in cases:
            with self.subTest(name=name, value=value):
                with patch.dict(os.environ, self._env(**{name: value}), clear=True):
                    with patch.object(pil, "propagate_to_pr") as mock_propagate:
                        with patch("builtins.print"):
                            self.assertEqual(pil.main(), 1)
                mock_propagate.assert_not_called()

    def test_tables_default_to_none(self):
        env = self._env()
        for name in ("EXCLUDED_LABELS", "EXCLUSIVE_SETS", "EXCLUSIVE_PREFIXES"):
            del env[name]
        with patch.dict(os.environ, env, clear=True):
            with patch.object(pil, "propagate_to_pr", return_value=True) as mock_propagate:
                self.assertEqual(pil.main(), 0)
        config = mock_propagate.call_args.args[5]
        self.assertEqual(frozenset(), config.excluded_labels)
        self.assertFalse(config.exclusive)

    def test_non_integer_pr_number_returns_1(self):
        with patch.dict(os.environ, self._env(PR_NUMBER="abc"), clear=True):
            self.assertEqual(pil.main(), 1)

    def test_delegates_to_propagate_to_pr_with_parsed_args(self):
        with patch.dict(os.environ, self._env(), clear=True):
            with patch.object(pil, "propagate_to_pr", return_value=True) as mock_propagate:
                result = pil.main()

        self.assertEqual(result, 0)
        mock_propagate.assert_called_once_with("owner", "repo", "owner/repo", 42, "tok", CONFIG)

    def test_returns_1_when_propagate_to_pr_fails(self):
        with patch.dict(os.environ, self._env(), clear=True):
            with patch.object(pil, "propagate_to_pr", return_value=False):
                self.assertEqual(pil.main(), 1)


# ---------------------------------------------------------------------------
# propagate_to_pr tests
# ---------------------------------------------------------------------------


class TestPropagateToPr(unittest.TestCase):
    def test_graphql_failure_returns_false(self):
        with patch.object(pil, "fetch_closing_issue_labels", side_effect=RuntimeError("boom")):
            result = pil.propagate_to_pr("owner", "repo", "owner/repo", 42, "tok", CONFIG)
        self.assertFalse(result)

    def test_no_closing_issues_returns_true_and_does_not_touch_labels(self):
        with patch.object(pil, "fetch_closing_issue_labels", return_value=[]):
            with patch.object(lc, "gh_api") as mock_api:
                result = pil.propagate_to_pr("owner", "repo", "owner/repo", 42, "tok", CONFIG)
        self.assertTrue(result)
        mock_api.assert_not_called()

    def test_pr_fetch_failure_returns_false(self):
        with patch.object(pil, "fetch_closing_issue_labels", return_value=["p1"]):
            with patch.object(
                lc,
                "gh_api",
                side_effect=urllib.error.HTTPError(
                    url=None, code=404, msg="Not Found", hdrs=None, fp=None
                ),
            ):
                result = pil.propagate_to_pr("owner", "repo", "owner/repo", 42, "tok", CONFIG)
        self.assertFalse(result)

    def test_applies_eligible_labels(self):
        with patch.object(pil, "fetch_closing_issue_labels", return_value=["p1", "bug"]):
            with patch.object(lc, "gh_api", return_value={"labels": [{"name": "bug"}]}) as mock_api:
                result = pil.propagate_to_pr("owner", "repo", "owner/repo", 42, "tok", CONFIG)

        self.assertTrue(result)
        post_calls = [c for c in mock_api.call_args_list if c.kwargs.get("method") == "POST"]
        self.assertEqual(len(post_calls), 1)
        self.assertEqual(post_calls[0].args[0], "repos/owner/repo/issues/42/labels")
        self.assertEqual(post_calls[0].kwargs["body"], {"labels": ["p1"]})

    def test_skip_only_case_returns_true_without_posting(self):
        # PR already carries p2; the only candidate (p1) conflicts and is
        # skipped, so nothing should be POSTed.
        with patch.object(pil, "fetch_closing_issue_labels", return_value=["p1"]):
            with patch.object(lc, "gh_api", return_value={"labels": [{"name": "p2"}]}) as mock_api:
                result = pil.propagate_to_pr("owner", "repo", "owner/repo", 42, "tok", CONFIG)

        self.assertTrue(result)
        post_calls = [c for c in mock_api.call_args_list if c.kwargs.get("method") == "POST"]
        self.assertEqual(post_calls, [])

    def test_apply_failure_returns_false(self):
        def fake_api(path, token, method="GET", body=None):
            if method == "POST":
                raise urllib.error.HTTPError(
                    url=None, code=422, msg="Unprocessable Entity", hdrs=None, fp=None
                )
            return {"labels": []}

        with patch.object(pil, "fetch_closing_issue_labels", return_value=["p1"]):
            with patch.object(lc, "gh_api", side_effect=fake_api):
                result = pil.propagate_to_pr("owner", "repo", "owner/repo", 42, "tok", CONFIG)
        self.assertFalse(result)

    def test_does_not_reapply_an_excluded_label_the_pr_just_lost(self):
        # The issue still carries "orchestrating" (active orchestration), but
        # the PR just had it removed alone to clear the merge gate. A routine
        # event must not put it back.
        with patch.object(pil, "fetch_closing_issue_labels", return_value=["orchestrating", "p1"]):
            with patch.object(lc, "gh_api", return_value={"labels": [{"name": "ci"}]}) as mock_api:
                result = pil.propagate_to_pr("owner", "repo", "owner/repo", 713, "tok", CONFIG)

        self.assertTrue(result)
        post_calls = [c for c in mock_api.call_args_list if c.kwargs.get("method") == "POST"]
        self.assertEqual(len(post_calls), 1)
        self.assertEqual(post_calls[0].kwargs["body"], {"labels": ["p1"]})

    def test_does_not_propagate_agents_from_a_linked_issue(self):
        # An issue about agents tooling carries `agents`; a PR fixing it
        # without touching an agents path must not inherit the label.
        with patch.object(pil, "fetch_closing_issue_labels", return_value=["agents", "p1"]):
            with patch.object(lc, "gh_api", return_value={"labels": []}) as mock_api:
                result = pil.propagate_to_pr("owner", "repo", "owner/repo", 42, "tok", CONFIG)

        self.assertTrue(result)
        post_calls = [c for c in mock_api.call_args_list if c.kwargs.get("method") == "POST"]
        self.assertEqual(len(post_calls), 1)
        self.assertEqual(post_calls[0].kwargs["body"], {"labels": ["p1"]})


# ---------------------------------------------------------------------------
# Configuration tests
# ---------------------------------------------------------------------------


class TestSplitRepo(unittest.TestCase):
    def test_owner_and_name(self):
        self.assertEqual(("owner", "repo"), pil.split_repo("owner/repo"))
        self.assertEqual(("o-1", "r.x_y"), pil.split_repo("o-1/r.x_y"))

    def test_rejects_what_is_not_owner_slash_name(self):
        for repo in ("", "owner", "owner/", "/repo", "/", "owner/repo/extra", "owner//repo"):
            with self.subTest(repo=repo):
                self.assertIsNone(pil.split_repo(repo))


class TestLoadConfig(unittest.TestCase):
    def test_reads_and_lowercases_the_tables(self):
        config = pil.load_config(
            {
                "EXCLUDED_LABELS": '["Agents"]',
                "EXCLUSIVE_SETS": '[["P1", "p2"]]',
                "EXCLUSIVE_PREFIXES": '["C-A-"]',
            }
        )
        self.assertEqual(frozenset({"agents"}), config.excluded_labels)
        self.assertEqual([frozenset({"p1", "p2"})], config.exclusive.sets)
        self.assertEqual(["c-a-"], config.exclusive.prefixes)

    def test_blank_tables_are_empty(self):
        config = pil.load_config({"EXCLUDED_LABELS": " ", "EXCLUSIVE_SETS": ""})
        self.assertEqual(frozenset(), config.excluded_labels)
        self.assertFalse(config.exclusive)

    def test_invalid_table_raises_input_error(self):
        with self.assertRaises(lc.InputError):
            pil.load_config({"EXCLUSIVE_SETS": "[[1, 2]]"})


if __name__ == "__main__":
    unittest.main()
