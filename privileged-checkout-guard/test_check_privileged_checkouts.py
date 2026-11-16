#!/usr/bin/env python3
"""Unit tests for check_privileged_checkouts.py.

The rule itself is exercised against synthetic workflows.
Without these, a check that had stopped detecting anything would still report
a clean tree.
"""

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(__file__))
import check_privileged_checkouts as cpc
from check_privileged_checkouts import violations


class ViolationDetectionTest(unittest.TestCase):
    """The rule itself, exercised against synthetic workflows."""

    UNPINNED = """
on:
  pull_request:
permissions:
  issues: write
  pull-requests: write
jobs:
  label:
    steps:
      - uses: actions/checkout@v6
      - run: python3 scripts/label.py
"""

    def _violations(self, text: str) -> list[str]:
        return violations(yaml.safe_load(text))

    def _with_ref(self, ref: str) -> str:
        return self.UNPINNED.replace(
            "      - uses: actions/checkout@v6\n",
            "      - uses: actions/checkout@v6\n        with:\n          ref: " + ref + "\n",
        )

    def _with_permissions(self, block: str) -> str:
        return self.UNPINNED.replace("permissions:\n  issues: write\n  pull-requests: write", block)

    def test_unpinned_privileged_checkout_is_reported(self):
        found = self._violations(self.UNPINNED)
        self.assertEqual(1, len(found), found)
        self.assertIn("merge ref", found[0])

    def test_pinned_privileged_checkout_is_accepted(self):
        pinned = self._with_ref("${{ github.event.pull_request.base.sha }}")
        self.assertEqual([], self._violations(pinned))

    def test_default_branch_checkout_is_accepted(self):
        pinned = self._with_ref("${{ github.event.repository.default_branch }}")
        self.assertEqual([], self._violations(pinned))

    def test_head_sha_is_reported(self):
        found = self._violations(self._with_ref("${{ github.event.pull_request.head.sha }}"))
        self.assertEqual(1, len(found), found)
        self.assertIn("head.sha", found[0])

    def test_bare_github_sha_is_reported(self):
        # github.sha is the merge ref on a pull_request event, so on its own it
        # is exactly the ref this guard rejects.
        found = self._violations(self._with_ref("${{ github.sha }}"))
        self.assertEqual(1, len(found), found)

    def test_pull_request_target_is_covered(self):
        target = self.UNPINNED.replace("  pull_request:", "  pull_request_target:")
        self.assertEqual(1, len(self._violations(target)))

    def test_non_pull_request_workflow_is_ignored(self):
        scheduled = self.UNPINNED.replace("  pull_request:", "  schedule:\n    - cron: '0 0 * * *'")
        self.assertEqual([], self._violations(scheduled))

    def test_every_write_scope_is_privileged(self):
        # Not just issues and pull-requests: contents: write over pull request code is strictly worse.
        for scope in ("contents", "actions", "packages", "deployments", "id-token"):
            with self.subTest(scope=scope):
                found = self._violations(self._with_permissions(f"permissions:\n  {scope}: write"))
                self.assertEqual(1, len(found), found)
                self.assertIn(scope, found[0])

    def test_security_events_write_is_exempt(self):
        # A CodeQL workflow exists to analyze the pull request's code and must
        # check out the merge ref to do it.
        analyzer = self._with_permissions(
            "permissions:\n  contents: read\n  security-events: write"
        )
        self.assertEqual([], self._violations(analyzer))

    def test_security_events_does_not_exempt_a_job_holding_more(self):
        mixed = self._with_permissions("permissions:\n  security-events: write\n  contents: write")
        found = self._violations(mixed)
        self.assertEqual(1, len(found), found)
        self.assertIn("contents", found[0])
        self.assertNotIn("security-events", found[0])

    def test_read_scopes_are_ignored(self):
        reader = self._with_permissions("permissions:\n  contents: read\n  issues: read")
        self.assertEqual([], self._violations(reader))

    def test_job_permissions_replace_workflow_permissions(self):
        # A job-level block replaces the workflow-level one outright, so a
        # privileged workflow default does not make a read-only job privileged.
        narrowed = self.UNPINNED.replace(
            "  label:\n    steps:",
            "  label:\n    permissions:\n      contents: read\n    steps:",
        )
        self.assertEqual([], self._violations(narrowed))

        widened = self.UNPINNED.replace(
            "permissions:\n  issues: write\n  pull-requests: write\n", ""
        ).replace(
            "  label:\n    steps:",
            "  label:\n    permissions:\n      issues: write\n    steps:",
        )
        self.assertEqual(1, len(self._violations(widened)))

    def test_undeclared_permissions_are_treated_as_privileged(self):
        undeclared = self.UNPINNED.replace(
            "permissions:\n  issues: write\n  pull-requests: write\n", ""
        )
        found = self._violations(undeclared)
        self.assertEqual(1, len(found), found)
        self.assertIn("no declared", found[0])

    def test_write_all_is_privileged_and_read_all_is_not(self):
        self.assertEqual(1, len(self._violations(self._with_permissions("permissions: write-all"))))
        self.assertEqual([], self._violations(self._with_permissions("permissions: read-all")))

    def test_job_without_a_checkout_is_ignored(self):
        no_checkout = self.UNPINNED.replace("      - uses: actions/checkout@v6\n", "")
        self.assertEqual([], self._violations(no_checkout))

    def test_checkout_is_matched_in_any_case(self):
        # GitHub resolves the owner and repository of `uses:` case-insensitively.
        cased = self.UNPINNED.replace("actions/checkout@v6", "Actions/Checkout@v6")
        self.assertEqual(1, len(self._violations(cased)))

    def test_a_similarly_named_action_is_not_mistaken_for_checkout(self):
        other = self.UNPINNED.replace(
            "      - uses: actions/checkout@v6\n", "      - uses: actions/checkout-foo@v1\n"
        )
        self.assertEqual([], self._violations(other))



class MainTest(unittest.TestCase):
    """The command line: which files it reads, and what it prints and returns."""

    PINNED = ViolationDetectionTest.UNPINNED.replace(
        "      - uses: actions/checkout@v6\n",
        "      - uses: actions/checkout@v6\n        with:\n"
        "          ref: ${{ github.event.pull_request.base.sha }}\n",
    )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def run_main(self) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cpc.main(["--workflows-dir", str(self.dir)])
        return code, out.getvalue(), err.getvalue()

    def test_clean_directory_passes(self):
        (self.dir / "ok.yml").write_text(self.PINNED, encoding="utf-8")
        code, out, _ = self.run_main()
        self.assertEqual(0, code)
        self.assertIn("No privileged", out)

    def test_violation_is_named_with_its_file_and_fails(self):
        (self.dir / "ok.yml").write_text(self.PINNED, encoding="utf-8")
        (self.dir / "bad.yml").write_text(ViolationDetectionTest.UNPINNED, encoding="utf-8")
        code, out, _ = self.run_main()
        self.assertEqual(1, code)
        self.assertIn("bad.yml", out)
        self.assertNotIn("ok.yml", out)

    def test_yaml_extension_is_read_too(self):
        (self.dir / "bad.yaml").write_text(ViolationDetectionTest.UNPINNED, encoding="utf-8")
        code, out, _ = self.run_main()
        self.assertEqual(1, code)
        self.assertIn("bad.yaml", out)

    def test_directory_without_workflows_is_an_error_not_a_pass(self):
        code, _, err = self.run_main()
        self.assertEqual(2, code)
        self.assertIn("no workflow files", err)

    def test_empty_workflow_file_is_ignored(self):
        (self.dir / "empty.yml").write_text("", encoding="utf-8")
        (self.dir / "ok.yml").write_text(self.PINNED, encoding="utf-8")
        self.assertEqual(0, self.run_main()[0])


if __name__ == "__main__":
    unittest.main()
