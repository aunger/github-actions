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
  that a tag move can change, and Dependabot's github-actions entry reaches
  every action's directory to bump it;
- Dependabot's uv entries reach each requirements `.in` once, and each entry's
  groups split exactly that entry's `.in` pins from the rest;
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
PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*==")


def action_files() -> list[Path]:
    return sorted(
        path for path in ROOT.glob("*/action.yml") if not path.parent.name.startswith(".")
    )


def dependabot_updates() -> list[dict]:
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    return config["updates"]


def normalized(name: str) -> str:
    # PEP 503, the form Dependabot's uv parser gives a dependency's name.
    return re.sub(r"[-_.]+", "-", name).lower()


def uv_reach(entry: dict) -> set[str]:
    # The uv file fetcher reads the directory it is given and each directory
    # immediately below it, less what exclude-paths names; `dir/**` is the only
    # pattern form handled here.
    base = ROOT / entry["directory"].strip("/")
    excluded = {pattern.removesuffix("/**").strip("/") for pattern in entry.get("exclude-paths") or []}
    reached = set()
    for path in [*base.glob("*.in"), *base.glob("*/*.in")]:
        relative = path.relative_to(base)
        if len(relative.parts) == 2 and relative.parts[0] in excluded:
            continue
        reached.add(str(path.relative_to(ROOT)))
    return reached


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

    def test_every_shell_script_a_step_runs_is_executable(self):
        # Every .sh file a step names is run directly, so it needs the execute
        # bit, which git keeps; the Python scripts are run through python3.
        for path in action_files():
            for index, step in enumerate(steps(path)):
                for relative in ACTION_PATH_FILE.findall(step.get("run") or ""):
                    if relative.endswith(".sh"):
                        target = (path.parent / relative).resolve()
                        with self.subTest(action=path.parent.name, step=index, file=relative):
                            self.assertTrue(target.stat().st_mode & 0o111, f"{target} is not executable")

    def test_every_tracked_shell_script_is_executable(self):
        # The check above sees only what a step names; this covers a script that
        # another script runs, and the tests.
        for script in sorted(ROOT.glob("*/*.sh")):
            with self.subTest(script=str(script.relative_to(ROOT))):
                self.assertTrue(script.stat().st_mode & 0o111, f"{script} is not executable")

    def test_every_third_party_action_is_pinned_to_a_full_sha(self):
        for path in action_files():
            for index, step in enumerate(steps(path)):
                uses = step.get("uses")
                if uses is not None:
                    with self.subTest(action=path.parent.name, step=index):
                        self.assertRegex(uses + " ", FULL_SHA, f"{uses} is not pinned to a commit")

    def test_dependabot_reaches_every_action(self):
        entries = [u for u in dependabot_updates() if u["package-ecosystem"] == "github-actions"]
        self.assertEqual(1, len(entries), "expected one github-actions entry")
        directories = set(entries[0].get("directories") or [entries[0].get("directory")])
        self.assertIn("/", directories, "the workflows are not covered")
        for path in action_files():
            with self.subTest(action=path.parent.name):
                self.assertTrue(
                    "/*" in directories or f"/{path.parent.name}" in directories,
                    f"Dependabot does not read {path.parent.name}/action.yml",
                )

    def test_dependabot_uv_entries_reach_each_requirements_in_once(self):
        entries = [u for u in dependabot_updates() if u["package-ecosystem"] == "uv"]
        for entry in entries:
            self.assertIn("directory", entry, "give each uv entry one directory, so it can exclude others")
        reached = [path for entry in entries for path in sorted(uv_reach(entry))]
        tracked = sorted(str(p.relative_to(ROOT)) for p in [*ROOT.glob("*.in"), *ROOT.glob("*/*.in")])
        self.assertEqual(tracked, sorted(reached), "each .in should be reached by exactly one uv entry")

    def test_dependabot_uv_groups_match_the_in_pins(self):
        for entry in (u for u in dependabot_updates() if u["package-ecosystem"] == "uv"):
            with self.subTest(directory=entry["directory"]):
                pins = set()
                for path in uv_reach(entry):
                    for line in (ROOT / path).read_text(encoding="utf-8").splitlines():
                        match = PIN.match(line.split("#", 1)[0].strip())
                        if match:
                            pins.add(normalized(match[1]))
                self.assertTrue(pins, "found no pins")
                groups = entry["groups"]
                self.assertEqual(pins, set(groups["python-direct"]["patterns"]))
                self.assertEqual(["*"], groups["python-transitive"]["patterns"])
                self.assertEqual(pins, set(groups["python-transitive"]["exclude-patterns"]))

    def test_every_action_is_listed_in_the_readme(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for path in action_files():
            with self.subTest(action=path.parent.name):
                self.assertIn(f"`{path.parent.name}`", readme)


if __name__ == "__main__":
    unittest.main()
