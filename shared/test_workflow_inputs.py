#!/usr/bin/env python3
"""Unit tests for workflow_inputs.py."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import workflow_inputs as wi

SHA = "0123456789abcdef0123456789abcdef01234567"


class ActionInputsTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "workflow.yml"

    def inputs(self, text: str, action: str = "label-by-title") -> dict[str, str]:
        self.path.write_text(text, encoding="utf-8")
        return wi.action_inputs(self.path, action)

    def refused(self, text: str, action: str = "label-by-title") -> str:
        self.path.write_text(text, encoding="utf-8")
        with self.assertRaises(wi.WorkflowInputError) as caught:
            wi.action_inputs(self.path, action)
        return str(caught.exception)

    def test_remote_form_with_a_sha_and_comment(self):
        found = self.inputs(
            "jobs:\n  j:\n    steps:\n"
            "      - uses: actions/checkout@v7\n"
            f"      - uses: aunger/vcs-automation/label-by-title@{SHA} # v1.2.0\n"
            '        with:\n          rules: \'{"ci": "ci"}\'\n'
        )
        self.assertEqual({"rules": '{"ci": "ci"}'}, found)

    def test_remote_form_with_any_owner_repo_and_ref(self):
        found = self.inputs(
            "jobs:\n  j:\n    steps:\n"
            "      - uses: someone/fork/label-by-title@main\n"
            "        with:\n          rules: x\n"
        )
        self.assertEqual({"rules": "x"}, found)

    def test_local_form(self):
        found = self.inputs(
            "jobs:\n  j:\n    steps:\n"
            "      - uses: ./label-by-title\n        with:\n          rules: x\n"
        )
        self.assertEqual({"rules": "x"}, found)

    def test_local_form_with_a_trailing_slash(self):
        found = self.inputs(
            "jobs:\n  j:\n    steps:\n"
            "      - uses: ./label-by-title/\n        with:\n          rules: x\n"
        )
        self.assertEqual({"rules": "x"}, found)

    def test_a_block_scalar_keeps_its_text(self):
        found = self.inputs(
            "jobs:\n  j:\n    steps:\n"
            f"      - uses: aunger/vcs-automation/label-by-title@{SHA}\n"
            '        with:\n          rules: |\n            {\n              "ci": "ci"\n'
            "            }\n"
        )
        self.assertEqual('{\n  "ci": "ci"\n}\n', found["rules"])

    def test_a_step_with_no_with_gives_no_inputs(self):
        found = self.inputs("jobs:\n  j:\n    steps:\n      - uses: ./label-by-title\n")
        self.assertEqual({}, found)

    def test_non_string_values_are_given_as_strings(self):
        found = self.inputs(
            "jobs:\n  j:\n    steps:\n"
            "      - uses: ./label-by-title\n        with:\n          a: 10\n          b:\n"
        )
        self.assertEqual({"a": "10", "b": ""}, found)

    def test_steps_across_jobs_are_found(self):
        found = self.inputs(
            "jobs:\n  a:\n    steps:\n      - run: echo\n"
            "  b:\n    steps:\n      - uses: ./label-by-title\n        with:\n          rules: x\n"
        )
        self.assertEqual({"rules": "x"}, found)

    def test_another_action_with_a_longer_name_does_not_count(self):
        message = self.refused(
            "jobs:\n  j:\n    steps:\n"
            "      - uses: aunger/vcs-automation/label-by-title-extra@main\n"
            "      - uses: aunger/vcs-automation/x/label-by-title@main\n"
            "      - uses: ./label-by-titles\n"
        )
        self.assertIn("0 steps", message)

    def test_no_step_using_the_action_is_refused(self):
        message = self.refused("jobs:\n  j:\n    steps:\n      - uses: actions/checkout@v7\n")
        self.assertIn("0 steps", message)
        self.assertIn("label-by-title", message)

    def test_two_steps_using_the_action_are_refused(self):
        message = self.refused(
            "jobs:\n  a:\n    steps:\n      - uses: ./label-by-title\n"
            "  b:\n    steps:\n      - uses: aunger/vcs-automation/label-by-title@main\n"
        )
        self.assertIn("2 steps", message)

    def test_a_workflow_with_no_jobs_is_refused(self):
        self.assertIn("0 steps", self.refused("name: empty\n"))
        self.assertIn("0 steps", self.refused(""))
        self.assertIn("0 steps", self.refused("- a list\n"))

    def test_a_missing_file_is_refused(self):
        with self.assertRaises(wi.WorkflowInputError) as caught:
            wi.action_inputs(Path(self._dir.name) / "missing.yml", "label-by-title")
        self.assertIn("cannot read", str(caught.exception))

    def test_invalid_yaml_is_refused(self):
        self.assertIn("cannot read", self.refused("jobs: [\n"))

    def test_without_pyyaml_the_error_says_what_to_install(self):
        self.path.write_text("jobs: {}\n", encoding="utf-8")
        with patch.dict(sys.modules, {"yaml": None}):
            with self.assertRaises(wi.WorkflowInputError) as caught:
                wi.action_inputs(self.path, "label-by-title")
        self.assertIn("requirements.txt", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
