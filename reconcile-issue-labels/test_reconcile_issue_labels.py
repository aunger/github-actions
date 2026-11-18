#!/usr/bin/env python3
"""Unit tests for reconcile_issue_labels.py."""

import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import reconcile_issue_labels as ril

lc = ril.lc
pil = ril.pil

# A caller's tables, as one would pass them to the action.
EXCLUDED_LABELS = ["orchestrating", "agents"]
EXCLUSIVE_SETS = [["p1", "p2", "p3"], ["orchestrate", "orchestrating"]]
EXCLUSIVE_PREFIXES = ["c-a-", "c-r-"]

CONFIG = pil.Config(
    frozenset(EXCLUDED_LABELS),
    lc.ExclusiveRules([frozenset(s) for s in EXCLUSIVE_SETS], EXCLUSIVE_PREFIXES),
)

# ---------------------------------------------------------------------------
# fetch_open_pr_numbers tests
# ---------------------------------------------------------------------------


class TestFetchOpenPrNumbers(unittest.TestCase):
    """What this function adds to the shared listing helper is the number projection.

    The pagination it inherits is asserted against lc.fetch_open_pull_requests
    in shared/test_label_common.py, which is where that contract lives.
    """

    def test_returns_the_numbers_of_the_listed_prs(self):
        with patch.object(lc, "gh_api", side_effect=[[{"number": 5}, {"number": 7}], []]):
            result = ril.fetch_open_pr_numbers("owner/repo", "tok")
        self.assertEqual(result, [5, 7])

    def test_returns_empty_when_no_open_prs(self):
        with patch.object(lc, "gh_api", return_value=[]):
            result = ril.fetch_open_pr_numbers("owner/repo", "tok")
        self.assertEqual(result, [])

    def test_requests_open_state(self):
        captured_paths = []

        def fake_api(path, token):
            captured_paths.append(path)
            return []

        with patch.object(lc, "gh_api", side_effect=fake_api):
            ril.fetch_open_pr_numbers("owner/repo", "tok")

        self.assertIn("state=open", captured_paths[0])
        self.assertTrue(captured_paths[0].startswith("repos/owner/repo/pulls?"))


# ---------------------------------------------------------------------------
# main() tests
# ---------------------------------------------------------------------------


class TestMain(unittest.TestCase):
    def _env(self, **overrides):
        env = {
            "GITHUB_TOKEN": "tok",
            "GITHUB_REPOSITORY": "owner/repo",
            "EXCLUDED_LABELS": json.dumps(EXCLUDED_LABELS),
            "EXCLUSIVE_SETS": json.dumps(EXCLUSIVE_SETS),
            "EXCLUSIVE_PREFIXES": json.dumps(EXCLUSIVE_PREFIXES),
        }
        env.update(overrides)
        return env

    def test_missing_token_returns_1(self):
        with patch.dict(os.environ, self._env(GITHUB_TOKEN=""), clear=True):
            self.assertEqual(ril.main(), 1)

    def test_missing_repo_returns_1(self):
        with patch.dict(os.environ, self._env(GITHUB_REPOSITORY=""), clear=True):
            self.assertEqual(ril.main(), 1)

    def test_malformed_repo_returns_1(self):
        for repo in ("not-a-slug", "owner/", "owner/repo/extra"):
            with self.subTest(repo=repo):
                with patch.dict(os.environ, self._env(GITHUB_REPOSITORY=repo), clear=True):
                    with patch.object(ril, "fetch_open_pr_numbers") as mock_fetch:
                        with patch("builtins.print"):
                            self.assertEqual(ril.main(), 1)
                mock_fetch.assert_not_called()

    def test_invalid_table_returns_1_before_listing(self):
        for name in ("EXCLUDED_LABELS", "EXCLUSIVE_SETS", "EXCLUSIVE_PREFIXES"):
            with self.subTest(name=name):
                with patch.dict(os.environ, self._env(**{name: "{not json"}), clear=True):
                    with patch.object(ril, "fetch_open_pr_numbers") as mock_fetch:
                        with patch("builtins.print"):
                            self.assertEqual(ril.main(), 1)
                mock_fetch.assert_not_called()

    def test_fetch_open_prs_failure_returns_1(self):
        with patch.dict(os.environ, self._env(), clear=True):
            with patch.object(ril, "fetch_open_pr_numbers", side_effect=RuntimeError("boom")):
                self.assertEqual(ril.main(), 1)

    def test_no_open_prs_returns_0_without_propagating(self):
        with patch.dict(os.environ, self._env(), clear=True):
            with patch.object(ril, "fetch_open_pr_numbers", return_value=[]):
                with patch.object(pil, "propagate_to_pr") as mock_propagate:
                    result = ril.main()
        self.assertEqual(result, 0)
        mock_propagate.assert_not_called()

    def test_reconciles_every_open_pr_with_the_tables(self):
        with patch.dict(os.environ, self._env(), clear=True):
            with patch.object(ril, "fetch_open_pr_numbers", return_value=[5, 7, 9]):
                with patch.object(pil, "propagate_to_pr", return_value=True) as mock_propagate:
                    result = ril.main()

        self.assertEqual(result, 0)
        calls = [c.args for c in mock_propagate.call_args_list]
        self.assertEqual(
            calls,
            [
                ("owner", "repo", "owner/repo", 5, "tok", CONFIG),
                ("owner", "repo", "owner/repo", 7, "tok", CONFIG),
                ("owner", "repo", "owner/repo", 9, "tok", CONFIG),
            ],
        )

    def test_one_failure_among_several_still_reports_failure(self):
        def fake_propagate(owner, name, repo, pr_number, token, config):
            return pr_number != 7

        with patch.dict(os.environ, self._env(), clear=True):
            with patch.object(ril, "fetch_open_pr_numbers", return_value=[5, 7, 9]):
                with patch.object(pil, "propagate_to_pr", side_effect=fake_propagate):
                    result = ril.main()

        self.assertEqual(result, 1)

    def test_one_failure_does_not_stop_remaining_prs(self):
        def fake_propagate(owner, name, repo, pr_number, token, config):
            calls.append(pr_number)
            return pr_number != 7

        calls: list[int] = []
        with patch.dict(os.environ, self._env(), clear=True):
            with patch.object(ril, "fetch_open_pr_numbers", return_value=[5, 7, 9]):
                with patch.object(pil, "propagate_to_pr", side_effect=fake_propagate):
                    ril.main()

        self.assertEqual(calls, [5, 7, 9])

    def test_applies_the_exclusions_end_to_end(self):
        """One open PR, closing an issue with an excluded and a conflicting label."""

        def fake_api(path, token, method="GET", body=None):
            if path.startswith("repos/owner/repo/pulls?"):
                return [{"number": 5}]
            if method == "POST":
                posted.append(body)
                return None
            return {"labels": [{"name": "p2"}]}

        posted: list[dict] = []
        with patch.dict(os.environ, self._env(), clear=True):
            with patch.object(
                pil, "fetch_closing_issue_labels", return_value=["orchestrating", "p1", "bug"]
            ):
                with patch.object(lc, "gh_api", side_effect=fake_api):
                    with patch("builtins.print"):
                        result = ril.main()

        self.assertEqual(result, 0)
        self.assertEqual(posted, [{"labels": ["bug"]}])


if __name__ == "__main__":
    unittest.main()
