#!/usr/bin/env python3
"""Unit tests for snooze_issues.py."""

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import snooze_issues as si

lc = si.lc

# A caller's snooze table, as one would pass it to the action.
SNOOZE_LABELS = {f"snooze {days} days": days for days in (3, 7, 14, 30, 90, 180)}


class InjectedError(Exception):
    """A failure raised by a mock, so a test can tell it from any other exception."""


# ---------------------------------------------------------------------------
# snooze_issue tests
# ---------------------------------------------------------------------------


class TestSnoozeIssue(unittest.TestCase):
    def test_closes_open_issue_as_not_planned(self):
        with patch.object(
            lc,
            "gh_api",
            side_effect=[{"number": 5, "state": "open"}, None],
        ) as mock_api:
            result = si.snooze_issue(5, "snooze 30 days", "owner/repo", "tok")

        self.assertTrue(result)
        self.assertEqual(mock_api.call_count, 2)
        patch_call = mock_api.call_args_list[1]
        self.assertEqual(patch_call[1]["method"], "PATCH")
        self.assertEqual(
            patch_call[1]["body"],
            {"state": "closed", "state_reason": "not_planned"},
        )
        self.assertEqual("repos/owner/repo/issues/5", patch_call[0][0])

    def test_already_closed_is_a_noop(self):
        with patch.object(
            lc,
            "gh_api",
            side_effect=[{"number": 5, "state": "closed"}],
        ) as mock_api:
            result = si.snooze_issue(5, "snooze 30 days", "owner/repo", "tok")

        self.assertTrue(result)
        # Only the GET was made; no PATCH followed for an already-closed issue.
        self.assertEqual(mock_api.call_count, 1)

    def test_raises_on_non_dict_response(self):
        with patch.object(lc, "gh_api", return_value=None):
            with self.assertRaises(RuntimeError):
                si.snooze_issue(5, "snooze 30 days", "owner/repo", "tok")

    def test_raises_when_fetch_fails(self):
        with patch.object(lc, "gh_api", side_effect=InjectedError("network error")):
            with self.assertRaises(InjectedError):
                si.snooze_issue(5, "snooze 30 days", "owner/repo", "tok")

    def test_raises_when_patch_fails(self):
        with patch.object(
            lc,
            "gh_api",
            side_effect=[{"number": 5, "state": "open"}, InjectedError("boom")],
        ):
            with self.assertRaises(InjectedError):
                si.snooze_issue(5, "snooze 30 days", "owner/repo", "tok")


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
                "ADDED_LABEL": "snooze 30 days",
                "SNOOZE_LABELS": json.dumps(SNOOZE_LABELS),
            },
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()

    def test_exit_0_when_token_missing(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": ""}):
            result = si.main()
        self.assertEqual(result, 0)

    def test_exit_0_when_repository_missing(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": ""}):
            result = si.main()
        self.assertEqual(result, 0)

    def test_exit_0_when_issue_number_missing(self):
        with patch.dict(os.environ, {"ISSUE_NUMBER": ""}):
            result = si.main()
        self.assertEqual(result, 0)

    def test_an_event_other_than_labeled_is_a_skip(self):
        with patch.dict(os.environ, {"EVENT_ACTION": "unlabeled"}):
            with patch.object(lc, "gh_api") as mock_api:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(0, si.main())
        mock_api.assert_not_called()

    def test_exit_0_when_added_label_missing(self):
        with patch.dict(os.environ, {"ADDED_LABEL": ""}):
            result = si.main()
        self.assertEqual(result, 0)

    def test_exit_0_when_label_not_a_snooze_label(self):
        with patch.dict(os.environ, {"ADDED_LABEL": "bug"}):
            with patch.object(lc, "gh_api") as mock_api:
                result = si.main()
        self.assertEqual(result, 0)
        mock_api.assert_not_called()

    def test_exit_1_when_issue_number_non_integer(self):
        with patch.dict(os.environ, {"ISSUE_NUMBER": "abc"}):
            result = si.main()
        self.assertEqual(result, 1)

    def test_a_label_outside_the_table_does_not_snooze(self):
        """Only the caller's table counts: a similar-looking name is an ordinary label."""
        with patch.dict(os.environ, {"ADDED_LABEL": "hold 30 days"}):
            with patch.object(lc, "gh_api") as mock_api:
                result = si.main()
        self.assertEqual(result, 0)
        mock_api.assert_not_called()

    def test_snoozes_for_each_snooze_label(self):
        """Every label in the table snoozes, whatever the casing of the label."""
        for label in (*sorted(SNOOZE_LABELS), "SNOOZE 30 DAYS"):
            with self.subTest(label=label):
                with patch.dict(os.environ, {"ADDED_LABEL": label}):
                    with patch.object(si, "snooze_issue", return_value=True) as mock_snooze:
                        result = si.main()
                self.assertEqual(result, 0)
                mock_snooze.assert_called_once_with(42, label, "owner/repo", "test-token")

    def test_a_table_label_in_any_casing_snoozes(self):
        with patch.dict(
            os.environ,
            {"SNOOZE_LABELS": json.dumps({"Snooze A Week": 7}), "ADDED_LABEL": "snooze a week"},
        ):
            with patch.object(si, "snooze_issue", return_value=True) as mock_snooze:
                result = si.main()
        self.assertEqual(result, 0)
        mock_snooze.assert_called_once()

    def test_exit_1_when_snooze_raises(self):
        with patch.object(si, "snooze_issue", side_effect=Exception("boom")):
            result = si.main()
        self.assertEqual(result, 1)

    def test_exit_0_on_success(self):
        with patch.object(si, "snooze_issue", return_value=True) as mock_snooze:
            result = si.main()
        self.assertEqual(result, 0)
        mock_snooze.assert_called_once()

    def test_exit_1_when_snooze_labels_invalid(self):
        for value in (
            "",
            "not json",
            "[]",
            "{}",
            '{"snooze 3 days": 0}',
            '{"snooze 3 days": "3"}',
            '{"snooze 3 days": true}',
            '{"snooze 3 days": 3, "SNOOZE 3 DAYS": 3}',
            '{"": 3}',
        ):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"SNOOZE_LABELS": value}):
                    with patch.object(si, "snooze_issue") as mock_snooze:
                        with patch("builtins.print"):
                            result = si.main()
                self.assertEqual(result, 1)
                mock_snooze.assert_not_called()


if __name__ == "__main__":
    unittest.main()
