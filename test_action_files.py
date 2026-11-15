#!/usr/bin/env python3
"""Checks that hold for every action in this repository.

Each directory with an `action.yml` is an action.
What is checked here is what a person editing one is likely to get wrong and no
other test would notice, since the actions run only on GitHub:

- the file parses, names the action and describes it, and is a composite action;
- every `run` step names its `shell`, which a composite action requires;
- no `run` script interpolates `${{ ... }}`, so a value a caller or an event
  controls (a branch name, a comment) is never pasted into shell text;
- every `GITHUB_ACTION_PATH` file a step runs exists in the action's checkout;
- every `uses:` is a full-length commit SHA, so an action here never runs code
  that a tag move can change;
- every action is listed in `README.md`.
"""

import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent

# A step reaches a file in the repository through $GITHUB_ACTION_PATH, which is
# the action's own directory, so `../x/y.sh` is a sibling action's file.
ACTION_PATH_FILE = re.compile(r"\$GITHUB_ACTION_PATH/([A-Za-z0-9_./-]+)")
FULL_SHA = re.compile(r"@[0-9a-f]{40}(\s|$)")
EXPRESSION = re.compile(r"\$\{\{")


def action_files() -> list[Path]:
    return sorted(
        path for path in ROOT.glob("*/action.yml") if not path.parent.name.startswith(".")
    )


def steps(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["runs"]["steps"]


class ActionFilesTest(unittest.TestCase):
    def test_there_are_actions_to_check(self):
        self.assertTrue(action_files(), "found no action.yml files")

    def test_each_action_is_a_described_composite_action(self):
        for path in action_files():
            with self.subTest(action=path.parent.name):
                action = yaml.safe_load(path.read_text(encoding="utf-8"))
                self.assertTrue(action.get("name"), "no name")
                self.assertTrue(action.get("description"), "no description")
                self.assertEqual("composite", action["runs"]["using"])
                for name, spec in (action.get("inputs") or {}).items():
                    self.assertTrue(spec.get("description"), f"input {name} has no description")

    def test_every_run_step_names_its_shell(self):
        for path in action_files():
            for index, step in enumerate(steps(path)):
                if "run" in step:
                    with self.subTest(action=path.parent.name, step=index):
                        self.assertTrue(step.get("shell"), "a composite run step needs a shell")

    def test_no_run_script_interpolates_an_expression(self):
        for path in action_files():
            for index, step in enumerate(steps(path)):
                run = step.get("run")
                if run is not None:
                    with self.subTest(action=path.parent.name, step=index):
                        self.assertIsNone(
                            EXPRESSION.search(run),
                            "pass the value through env: and read it as a shell variable",
                        )

    def test_every_file_a_step_runs_exists(self):
        for path in action_files():
            for index, step in enumerate(steps(path)):
                for relative in ACTION_PATH_FILE.findall(step.get("run") or ""):
                    with self.subTest(action=path.parent.name, step=index, file=relative):
                        self.assertTrue((path.parent / relative).resolve().is_file())

    def test_every_script_a_step_runs_is_executable_or_run_by_an_interpreter(self):
        # A step that names a .sh file directly needs the file's execute bit,
        # which git keeps; one run through `python` or `bash` does not.
        for path in action_files():
            for index, step in enumerate(steps(path)):
                run = (step.get("run") or "").strip().strip("'\"")
                if run.startswith('"$GITHUB_ACTION_PATH') or run.startswith("$GITHUB_ACTION_PATH"):
                    target = path.parent / ACTION_PATH_FILE.search(run).group(1)
                    with self.subTest(action=path.parent.name, step=index):
                        self.assertTrue(target.stat().st_mode & 0o111, f"{target} is not executable")

    def test_every_third_party_action_is_pinned_to_a_full_sha(self):
        for path in action_files():
            for index, step in enumerate(steps(path)):
                uses = step.get("uses")
                if uses is not None:
                    with self.subTest(action=path.parent.name, step=index):
                        self.assertRegex(uses + " ", FULL_SHA, f"{uses} is not pinned to a commit")

    def test_every_action_is_listed_in_the_readme(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for path in action_files():
            with self.subTest(action=path.parent.name):
                self.assertIn(f"`{path.parent.name}`", readme)


if __name__ == "__main__":
    unittest.main()
