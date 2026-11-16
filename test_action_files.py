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
- Dependabot's uv entries reach each requirements `.in` and lock once, and each
  entry's groups split that entry's `.in` pins (less its WORKAROUND_PINS) from
  the rest;
- every action is listed in `README.md`.
"""

import fnmatch
import functools
import posixpath
import re
import subprocess
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent

sys.path.insert(0, str(ROOT / "dependency-audit"))
import audit_requirements  # noqa: E402

# A step reaches a file in the repository through $GITHUB_ACTION_PATH, which is
# the action's own directory, so `../x/y.sh` is a sibling action's file.
ACTION_PATH_FILE = re.compile(r"\$GITHUB_ACTION_PATH/([A-Za-z0-9_./-]+)")
FULL_SHA = re.compile(r"@[0-9a-f]{40}(\s|$)")
EXPRESSION = re.compile(r"\$\{\{")

# Pins an `.in` carries only to work around Dependabot's uv parser, for a
# package that is really transitive (the `.in` says why), keyed by that `.in`.
# They belong in python-transitive, so a tool bump cannot hold them back.
WORKAROUND_PINS = {"dependency-audit/requirements-audit.in": {"typing-extensions"}}


def action_files() -> list[Path]:
    return sorted(
        path for path in ROOT.glob("*/action.yml") if not path.parent.name.startswith(".")
    )


def dependabot_updates() -> list[dict]:
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    return config["updates"]


@functools.cache
def requirement_files() -> tuple[str, ...]:
    # Every requirements `.in` and `.txt` lock at any depth, as root-relative
    # paths, found as the audit action finds locks (by file name, outside hidden
    # directories). The uv fetcher takes any `.in` or `.txt` whose name contains
    # "requirements" (requirements_file?); others, such as a MANIFEST.in, it
    # reads only if their content looks like requirements, which is not modeled
    # here.
    # Dependabot sees only committed files, so where git can say which files
    # are tracked, the rest (a virtualenv's, say) are left out.
    found = sorted(
        path.relative_to(ROOT).as_posix()
        for glob in ("**/*requirements*.in", "**/*requirements*.txt")
        for path in audit_requirements.discover_locks(ROOT, glob)
    )
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return tuple(found)
    tracked = set(listed.split("\0"))
    return tuple(path for path in found if path in tracked)


def dependabot_excludes(path: str, patterns: list[str]) -> bool:
    # dependabot-core's FileFiltering.exclude_path?, which filter_excluded
    # applies to each listed path, relative to the repository root whatever the
    # entry's directory: an exact or directory-prefix match, `dir/**`, or a
    # glob. Ruby's fnmatch is tried without FNM_PATHNAME, where `*` crosses `/`
    # as fnmatchcase's does, and with it, where `**/` also matches no directory,
    # as glob_regex's does.
    for pattern in patterns:
        if "{" in pattern or "[" in pattern:
            raise AssertionError(
                f"exclude-paths pattern {pattern!r}: braces and bracket classes are not modeled here"
            )
        for candidate in {pattern, posixpath.normpath(pattern.rstrip("/")).lstrip("/")}:
            base = candidate.removesuffix("/**")
            if (
                path == base
                or path.startswith(base + "/")
                or fnmatch.fnmatchcase(path, candidate)
                or audit_requirements.glob_regex(candidate).match(path)
            ):
                return True
    return False


def uv_reach(entry: dict) -> set[str]:
    # The uv file fetcher lists the directory it is given and each directory
    # immediately below it, and filter_excluded drops a listed subdirectory or
    # file that exclude-paths matches. Files that a `-r` or `-c` line pulls in
    # are fetched without that filter; parse_pins rejects such lines, so the
    # group test fails rather than miss one.
    directory = entry["directory"].strip("/")
    patterns = entry.get("exclude-paths") or []
    reached = set()
    for path in requirement_files():
        parent = posixpath.dirname(path)
        if parent == directory:
            listed = [path]
        elif posixpath.dirname(parent) == directory:
            listed = [parent, path]
        else:
            continue
        if not any(dependabot_excludes(item, patterns) for item in listed):
            reached.add(path)
    return reached


def in_pins(path: str) -> set[str]:
    # parse_pins skips indented lines, which in a lock are hashes and `# via`
    # comments; pip and uv strip the indent from an `.in`'s lines, so this does
    # too, after joining a line that ends in a backslash to the one it continues.
    joined = (ROOT / path).read_text(encoding="utf-8").replace("\\\n", " ")
    text = "\n".join(line.lstrip() for line in joined.splitlines())
    try:
        return {pin.name for pin in audit_requirements.parse_pins(text, path)}
    except audit_requirements.AuditError as error:
        raise AssertionError(
            f"{error} (this test reads an `.in` as `name==version` pins only; extras, "
            "ranges and -r/-c includes are not modeled, and Dependabot fetches an "
            "include even from an excluded path)"
        ) from error


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

    def uv_entries(self) -> list[dict]:
        entries = [u for u in dependabot_updates() if u["package-ecosystem"] == "uv"]
        for entry in entries:
            self.assertIn("directory", entry, "give each uv entry one directory, so it can exclude others")
        return entries

    def test_dependabot_uv_entries_reach_each_requirements_file_once(self):
        reaches = {e["directory"]: uv_reach(e) for e in self.uv_entries()}
        for path in requirement_files():
            with self.subTest(file=path):
                reaching = [directory for directory, reach in reaches.items() if path in reach]
                self.assertEqual(1, len(reaching), f"reached by {reaching or 'no uv entry'}")

    def test_dependabot_uv_groups_match_the_in_pins(self):
        for entry in self.uv_entries():
            with self.subTest(directory=entry["directory"]):
                direct = set()
                for path in uv_reach(entry):
                    if path.endswith(".in"):
                        direct |= in_pins(path) - WORKAROUND_PINS.get(path, set())
                self.assertTrue(direct, "reaches no .in pins (a lock with no .in is not modeled)")
                groups = entry.get("groups")
                self.assertIsInstance(groups, dict, "groups: needs python-direct and python-transitive")
                for name, key in (("python-direct", "patterns"), ("python-transitive", "patterns"),
                                  ("python-transitive", "exclude-patterns")):
                    self.assertIn(key, groups.get(name) or {}, f"groups: needs {name} with {key}")
                self.assertEqual(direct, set(groups["python-direct"]["patterns"]))
                self.assertEqual(["*"], groups["python-transitive"]["patterns"])
                self.assertEqual(direct, set(groups["python-transitive"]["exclude-patterns"]))

    def test_workaround_pins_are_still_pinned(self):
        for path, names in WORKAROUND_PINS.items():
            with self.subTest(file=path):
                self.assertTrue((ROOT / path).is_file(), "no such .in; drop its WORKAROUND_PINS entry")
                self.assertLessEqual(names, in_pins(path), "no longer pinned; drop it from WORKAROUND_PINS")

    def test_every_action_is_listed_in_the_readme(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for path in action_files():
            with self.subTest(action=path.parent.name):
                self.assertIn(f"`{path.parent.name}`", readme)


if __name__ == "__main__":
    unittest.main()
