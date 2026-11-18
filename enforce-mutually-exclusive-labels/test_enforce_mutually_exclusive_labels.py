#!/usr/bin/env python3
"""Unit tests for enforce_mutually_exclusive_labels.py.

The GitHub access and the conflict rules are label_common's, tested in
shared/test_label_common.py; these tests drive main() with a real caller's
tables to check what it removes and what it reports.
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

import yaml

sys.path.insert(0, os.path.dirname(__file__))
import enforce_mutually_exclusive_labels as emxl

lc = emxl.lc

ACTION_YML = Path(__file__).resolve().parent / "action.yml"

# A real caller's tables: gallery-button-for-pixel-camera's, which pair with
# the orchestrate plugin's label protocol and carry a snooze ladder.
SNOOZE_LABELS = [f"snooze {days} days" for days in (3, 7, 14, 30, 90, 180)]
EXCLUSIVE_SETS = [
    ["p1", "p2", "p3"],
    ["verification needed", "verified"],
    ["changes requested", "changes done"],
    ["orchestrate", "orchestrating"],
    SNOOZE_LABELS,
]
EXCLUSIVE_PREFIXES = ["c-a-", "c-r-", "test-failure"]


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url=None, code=code, msg="err", hdrs=None, fp=None)


def _issue(labels: list[str]) -> dict:
    return {"labels": [{"name": lbl} for lbl in labels]}


class TestMain(unittest.TestCase):
    def setUp(self):
        self._env_patch = patch.dict(
            os.environ,
            {
                "GITHUB_TOKEN": "test-token",
                "GITHUB_REPOSITORY": "owner/repo",
                "ISSUE_NUMBER": "42",
                "ADDED_LABEL": "p1",
                "EXCLUSIVE_SETS": json.dumps(EXCLUSIVE_SETS),
                "EXCLUSIVE_PREFIXES": json.dumps(EXCLUSIVE_PREFIXES),
                # Keep main() from appending to a real Actions step output.
                "GITHUB_OUTPUT": "",
            },
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()

    def _run(self, added: str, labels: list[str] | None, *responses) -> tuple[int, list, str]:
        """Run main() for *added* on an item carrying *labels*.

        *labels* None means the API is not expected to be called. *responses*
        answer the calls after the label read, one each. Returns (exit code,
        the gh_api calls, combined output).
        """
        side_effect = [] if labels is None else [_issue(labels), *responses]
        out = io.StringIO()
        with patch.dict(os.environ, {"ADDED_LABEL": added}):
            with patch.object(lc, "gh_api", side_effect=side_effect) as mock_api:
                with redirect_stdout(out), redirect_stderr(out):
                    code = emxl.main()
        return code, mock_api.call_args_list, out.getvalue()

    def _deleted(self, calls) -> list[str]:
        return [c[0][0].rsplit("/", 1)[1] for c in calls if c.kwargs.get("method") == "DELETE"]

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def test_exit_0_when_a_required_variable_is_missing(self):
        for name in ("GITHUB_TOKEN", "GITHUB_REPOSITORY", "ISSUE_NUMBER", "ADDED_LABEL"):
            with self.subTest(missing=name):
                with patch.dict(os.environ, {name: ""}):
                    with patch.object(lc, "gh_api") as mock_api:
                        with redirect_stderr(io.StringIO()) as err:
                            self.assertEqual(emxl.main(), 0)
                mock_api.assert_not_called()
                self.assertIn(f"{name} not set", err.getvalue())

    def test_exit_1_when_issue_number_non_integer(self):
        with patch.dict(os.environ, {"ISSUE_NUMBER": "abc"}):
            with redirect_stderr(io.StringIO()):
                self.assertEqual(emxl.main(), 1)

    def test_exit_1_when_a_table_is_invalid(self):
        cases = {
            "EXCLUSIVE_SETS": ("not json", '["p1", "p2"]', '[["p1"]]', '{"a": []}'),
            "EXCLUSIVE_PREFIXES": ("not json", '"c-a-"', '[""]'),
        }
        for name, values in cases.items():
            for value in values:
                with self.subTest(table=name, value=value):
                    with patch.dict(os.environ, {name: value}):
                        with patch.object(lc, "gh_api") as mock_api:
                            with redirect_stderr(io.StringIO()) as err:
                                self.assertEqual(emxl.main(), 1)
                    mock_api.assert_not_called()
                    self.assertIn(name, err.getvalue())

    def test_no_tables_means_nothing_is_governed(self):
        with patch.dict(os.environ, {"EXCLUSIVE_SETS": "", "EXCLUSIVE_PREFIXES": ""}):
            code, calls, output = self._run("p1", None)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        self.assertIn("not in any mutually exclusive set", output)

    def test_exit_0_when_label_not_in_any_set(self):
        for label in ("bug", "ci", "hold 30 days"):
            with self.subTest(label=label):
                code, calls, _ = self._run(label, None)
                self.assertEqual(code, 0)
                self.assertEqual(calls, [])

    # ------------------------------------------------------------------
    # Fixed sets
    # ------------------------------------------------------------------

    def test_removes_conflicting_priority_label(self):
        code, calls, _ = self._run("p1", ["p2", "ci"], None)
        self.assertEqual(code, 0)
        self.assertEqual(calls[0][0][0], "repos/owner/repo/issues/42")
        self.assertEqual(self._deleted(calls), ["p2"])

    def test_removes_every_other_member_of_the_set(self):
        code, calls, _ = self._run("p1", ["p2", "p3", "ci"], None, None)
        self.assertEqual(code, 0)
        self.assertEqual(self._deleted(calls), ["p2", "p3"])

    def test_does_not_remove_when_no_conflicts(self):
        code, calls, output = self._run("p1", ["ci", "bug", "p1"])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("No conflicting labels", output)

    def test_matching_is_case_insensitive_and_deletes_by_the_carried_name(self):
        code, calls, _ = self._run("P1", ["P2", "p1"], None)
        self.assertEqual(code, 0)
        self.assertEqual(self._deleted(calls), ["P2"])

    def test_each_review_pair_is_its_own_set(self):
        cases = [
            ("verified", ["verification needed", "changes requested"], ["verification%20needed"]),
            ("changes done", ["changes requested", "verified"], ["changes%20requested"]),
            ("orchestrating", ["orchestrate", "verification needed"], ["orchestrate"]),
            ("orchestrate", ["orchestrating"], ["orchestrating"]),
        ]
        for added, carried, deleted in cases:
            with self.subTest(added=added):
                code, calls, _ = self._run(added, carried, None)
                self.assertEqual(code, 0)
                self.assertEqual(self._deleted(calls), deleted)

    def test_escalating_snooze_label_removes_shorter_one(self):
        code, calls, _ = self._run("snooze 90 days", ["snooze 30 days", "ci"], None)
        self.assertEqual(code, 0)
        self.assertEqual(self._deleted(calls), ["snooze%2030%20days"])

    def test_a_label_outside_the_snooze_set_is_left_alone(self):
        code, calls, _ = self._run("snooze 30 days", ["hold 30 days", "ci"])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)

    # ------------------------------------------------------------------
    # Prefix groups
    # ------------------------------------------------------------------

    def test_prefix_groups(self):
        cases = [
            ("c-a-opus", ["c-a-sonnet", "ci"], ["c-a-sonnet"]),
            ("c-r-haiku", ["c-r-opus", "bug"], ["c-r-opus"]),
            ("C-A-Opus", ["c-a-sonnet", "ci"], ["c-a-sonnet"]),
            ("c-r-sonnet", ["c-r-haiku", "c-a-opus", "ci"], ["c-r-haiku"]),
            ("test-failure-archive", ["test-failure", "automated tests"], ["test-failure"]),
            ("test-failure", ["test-failure-archive", "automated tests"], ["test-failure-archive"]),
        ]
        for added, carried, deleted in cases:
            with self.subTest(added=added, carried=carried):
                code, calls, _ = self._run(added, carried, *[None] * len(deleted))
                self.assertEqual(code, 0)
                self.assertEqual(self._deleted(calls), deleted)

    def test_a_prefix_label_with_no_sibling_does_nothing(self):
        for added, carried in (
            ("c-a-sonnet", ["ci", "c-r-haiku"]),
            ("test-failure", ["automated tests", "ci"]),
        ):
            with self.subTest(added=added):
                code, calls, _ = self._run(added, carried)
                self.assertEqual(code, 0)
                self.assertEqual(len(calls), 1)

    # ------------------------------------------------------------------
    # Failures
    # ------------------------------------------------------------------

    def test_exit_1_when_fetch_raises(self):
        with patch.object(lc, "gh_api", side_effect=Exception("network error")):
            with redirect_stderr(io.StringIO()):
                self.assertEqual(emxl.main(), 1)

    def test_exit_1_when_fetch_returns_non_dict(self):
        with patch.object(lc, "gh_api", return_value=None) as mock_api:
            with redirect_stderr(io.StringIO()):
                self.assertEqual(emxl.main(), 1)
        self.assertEqual(mock_api.call_count, 1)

    def test_exit_1_when_a_removal_fails(self):
        with redirect_stderr(io.StringIO()):
            code, _, _ = self._run("p1", ["p2", "ci"], _http_error(500))
        self.assertEqual(code, 1)

    def test_exit_0_when_removal_404_is_benign(self):
        code, _, _ = self._run("p1", ["p2", "ci"], _http_error(404))
        self.assertEqual(code, 0)

    # ------------------------------------------------------------------
    # The `removed` step output
    # ------------------------------------------------------------------

    def _main_with_output(self, side_effect) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = os.path.join(tmp, "output")
            open(output_path, "w").close()
            with patch.dict(os.environ, {"ADDED_LABEL": "p1", "GITHUB_OUTPUT": output_path}):
                with patch.object(lc, "gh_api", side_effect=side_effect):
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        result = emxl.main()
            with open(output_path, encoding="utf-8") as f:
                return result, f.read()

    def test_removed_output_names_each_removed_label(self):
        result, output = self._main_with_output([_issue(["p2", "p3", "ci"]), None, None])
        self.assertEqual(result, 0)
        self.assertEqual(output, 'removed=["p2", "p3"]\n')

    def test_removed_output_includes_a_label_already_gone(self):
        result, output = self._main_with_output([_issue(["p2"]), _http_error(404)])
        self.assertEqual(result, 0)
        self.assertEqual(output, 'removed=["p2"]\n')

    def test_no_removed_output_when_a_removal_fails(self):
        result, output = self._main_with_output([_issue(["p2"]), _http_error(500)])
        self.assertEqual(result, 1)
        self.assertEqual(output, "")

    def test_no_removed_output_when_nothing_conflicts(self):
        result, output = self._main_with_output([_issue(["ci"])])
        self.assertEqual(result, 0)
        self.assertEqual(output, "")


class TestActionWiring(unittest.TestCase):
    """What action.yml hands the script, which no test of main() can see."""

    def setUp(self):
        self.action = yaml.safe_load(ACTION_YML.read_text(encoding="utf-8"))
        (self.step,) = self.action["runs"]["steps"]

    def test_the_removed_output_comes_from_the_scripts_step(self):
        self.assertEqual(
            self.action["outputs"]["removed"]["value"],
            "${{ steps.%s.outputs.removed }}" % self.step["id"],
        )

    def test_the_tables_reach_the_script(self):
        env = self.step["env"]
        self.assertEqual(env["EXCLUSIVE_SETS"], "${{ inputs.exclusive-sets }}")
        self.assertEqual(env["EXCLUSIVE_PREFIXES"], "${{ inputs.exclusive-prefixes }}")

    def test_issue_and_pull_request_events_both_reach_the_script(self):
        env = self.step["env"]
        self.assertIn("github.event.issue.number", env["ISSUE_NUMBER"])
        self.assertIn("github.event.pull_request.number", env["ISSUE_NUMBER"])
        self.assertEqual(env["ADDED_LABEL"], "${{ github.event.label.name }}")

    def test_the_tables_default_to_none(self):
        for name in ("exclusive-sets", "exclusive-prefixes"):
            with self.subTest(input=name):
                self.assertEqual(json.loads(self.action["inputs"][name]["default"]), [])


if __name__ == "__main__":
    unittest.main()
