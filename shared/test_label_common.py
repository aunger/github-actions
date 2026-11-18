#!/usr/bin/env python3
"""Unit tests for label_common.py."""

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))
import label_common as lc


def _http_error(code: int) -> urllib.error.HTTPError:
    """Build an HTTPError carrying *code*."""
    return urllib.error.HTTPError(url=None, code=code, msg="err", hdrs=None, fp=None)


def _response(body: bytes) -> MagicMock:
    """A urlopen() result whose context manager reads *body*."""
    response = MagicMock()
    response.__enter__.return_value.read.return_value = body
    response.__enter__.return_value.headers = {}
    return response


# ---------------------------------------------------------------------------
# gh_api
# ---------------------------------------------------------------------------


class TestGhApi(unittest.TestCase):
    """The retry wrapper, exercised through urlopen rather than mocked itself."""

    def test_returns_parsed_json_for_non_empty_body(self):
        payload = {"labels": [{"name": "p1"}]}
        with patch("urllib.request.urlopen", return_value=_response(json.dumps(payload).encode())):
            result = lc.gh_api("repos/owner/repo/issues/1", token="tok")
        self.assertEqual(result, payload)

    def test_returns_none_for_empty_body(self):
        """DELETE /labels/:name answers 204 No Content with an empty body."""
        with patch("urllib.request.urlopen", return_value=_response(b"")):
            result = lc.gh_api("repos/owner/repo/issues/1/labels/p2", token="tok", method="DELETE")
        self.assertIsNone(result)

    def test_sends_the_method_and_a_json_body(self):
        with patch("urllib.request.urlopen", return_value=_response(b"[]")) as mock_urlopen:
            lc.gh_api(
                "repos/o/r/issues/1/labels", token="tok", method="POST", body={"labels": ["x"]}
            )
        request = mock_urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(mock_urlopen.call_args.args[1]), {"labels": ["x"]})
        self.assertEqual(request.full_url, "https://api.github.com/repos/o/r/issues/1/labels")

    def test_retries_transient_503_then_succeeds(self):
        payload = {"labels": [{"name": "p1"}]}
        with patch.object(lc.time, "sleep") as mock_sleep:
            with patch(
                "urllib.request.urlopen",
                side_effect=[_http_error(503), _response(json.dumps(payload).encode())],
            ) as mock_urlopen:
                result = lc.gh_api("repos/owner/repo/issues/1", token="tok")
        self.assertEqual(result, payload)
        self.assertEqual(mock_urlopen.call_count, 2)
        mock_sleep.assert_called_once()

    def test_retries_429_then_succeeds(self):
        with patch.object(lc.time, "sleep"):
            with patch(
                "urllib.request.urlopen", side_effect=[_http_error(429), _response(b"")]
            ) as mock_urlopen:
                result = lc.gh_api(
                    "repos/owner/repo/issues/1/labels/p2", token="tok", method="DELETE"
                )
        self.assertIsNone(result)
        self.assertEqual(mock_urlopen.call_count, 2)

    def test_retries_url_error_then_succeeds(self):
        with patch.object(lc.time, "sleep"):
            with patch(
                "urllib.request.urlopen",
                side_effect=[urllib.error.URLError("connection reset"), _response(b'{"ok": true}')],
            ) as mock_urlopen:
                result = lc.gh_api("repos/owner/repo/issues/1", token="tok")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(mock_urlopen.call_count, 2)

    def test_retries_exhaust_and_raise_last_transient(self):
        with patch.object(lc.time, "sleep") as mock_sleep:
            with patch(
                "urllib.request.urlopen",
                side_effect=[_http_error(503), _http_error(502), _http_error(500)],
            ) as mock_urlopen:
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    lc.gh_api("repos/owner/repo/issues/1", token="tok", max_attempts=3)
        self.assertEqual(ctx.exception.code, 500)
        self.assertEqual(mock_urlopen.call_count, 3)
        # One sleep between each pair of attempts: three attempts, two sleeps.
        self.assertEqual(mock_sleep.call_count, 2)

    def test_does_not_retry_404(self):
        with patch.object(lc.time, "sleep") as mock_sleep:
            with patch(
                "urllib.request.urlopen", side_effect=[_http_error(404), _response(b"{}")]
            ) as mock_urlopen:
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    lc.gh_api("repos/owner/repo/issues/1/labels/p2", token="tok", method="DELETE")
        self.assertEqual(ctx.exception.code, 404)
        self.assertEqual(mock_urlopen.call_count, 1)
        mock_sleep.assert_not_called()

    def test_does_not_retry_422(self):
        with patch.object(lc.time, "sleep") as mock_sleep:
            with patch("urllib.request.urlopen", side_effect=_http_error(422)) as mock_urlopen:
                with self.assertRaises(urllib.error.HTTPError):
                    lc.gh_api("repos/owner/repo/issues/1", token="tok")
        self.assertEqual(mock_urlopen.call_count, 1)
        mock_sleep.assert_not_called()

    def test_backoff_grows_exponentially(self):
        with patch.object(lc.time, "sleep") as mock_sleep:
            with patch("urllib.request.urlopen", side_effect=[_http_error(503)] * 4):
                with self.assertRaises(urllib.error.HTTPError):
                    lc.gh_api("repos/owner/repo/issues/1", token="tok", max_attempts=4)
        delays = [call.args[0] for call in mock_sleep.call_args_list]
        b0 = lc.INITIAL_BACKOFF_SECONDS
        m = lc.BACKOFF_MULTIPLIER
        self.assertEqual(delays, [b0, b0 * m, b0 * m * m])


class TestIsTransientError(unittest.TestCase):
    def test_5xx_is_transient(self):
        for code in (500, 502, 503, 504, 599):
            self.assertTrue(lc.is_transient_error(_http_error(code)), code)

    def test_429_is_transient(self):
        self.assertTrue(lc.is_transient_error(_http_error(429)))

    def test_url_error_is_transient(self):
        self.assertTrue(lc.is_transient_error(urllib.error.URLError("boom")))

    def test_other_4xx_is_not_transient(self):
        for code in (400, 401, 403, 404, 422):
            self.assertFalse(lc.is_transient_error(_http_error(code)), code)

    def test_non_url_exception_is_not_transient(self):
        self.assertFalse(lc.is_transient_error(ValueError("nope")))


# ---------------------------------------------------------------------------
# fetch_open_pull_requests
# ---------------------------------------------------------------------------


def _pull(number: int) -> dict:
    """A pull request object with enough shape for a caller to project from."""
    return {"number": number, "head": {"sha": f"{number:040d}"}, "labels": []}


class TestFetchOpenPullRequests(unittest.TestCase):
    """The listing check-blocking-labels and reconcile-issue-labels share.

    Both inherit the pagination asserted here, so it is asserted once against
    the helper rather than once per caller.
    """

    def test_returns_the_raw_objects_so_callers_can_project_them(self):
        page = [_pull(808), _pull(832)]
        with patch.object(lc, "gh_api", side_effect=[page]):
            found = lc.fetch_open_pull_requests("o/r", "tok")
        self.assertEqual(found, page)

    def test_requests_open_pull_requests_a_full_page_at_a_time(self):
        with patch.object(lc, "gh_api", side_effect=[[_pull(808)]]) as gh_api:
            lc.fetch_open_pull_requests("o/r", "tok")
        path = gh_api.call_args[0][0]
        self.assertEqual(path, f"repos/o/r/pulls?state=open&per_page={lc.PULLS_PER_PAGE}&page=1")
        self.assertEqual(gh_api.call_args.kwargs["token"], "tok")

    def test_a_short_first_page_costs_one_call(self):
        with patch.object(lc, "gh_api", side_effect=[[_pull(808)]]) as gh_api:
            found = lc.fetch_open_pull_requests("o/r", "tok")
        self.assertEqual([pr["number"] for pr in found], [808])
        self.assertEqual(gh_api.call_count, 1)

    def test_paginates_until_a_short_page(self):
        full_page = [_pull(n) for n in range(lc.PULLS_PER_PAGE)]
        with patch.object(lc, "gh_api", side_effect=[full_page, [_pull(808)]]) as gh_api:
            found = lc.fetch_open_pull_requests("o/r", "tok")
        self.assertEqual(len(found), lc.PULLS_PER_PAGE + 1)
        self.assertEqual(gh_api.call_count, 2)
        self.assertIn("page=2", gh_api.call_args[0][0])

    def test_paginates_until_an_empty_page_when_the_last_page_is_exactly_full(self):
        full_page = [_pull(n) for n in range(lc.PULLS_PER_PAGE)]
        with patch.object(lc, "gh_api", side_effect=[full_page, []]) as gh_api:
            found = lc.fetch_open_pull_requests("o/r", "tok")
        self.assertEqual(len(found), lc.PULLS_PER_PAGE)
        self.assertEqual(gh_api.call_count, 2)

    def test_no_open_pull_requests_yields_an_empty_list(self):
        with patch.object(lc, "gh_api", side_effect=[[]]) as gh_api:
            self.assertEqual(lc.fetch_open_pull_requests("o/r", "tok"), [])
        self.assertEqual(gh_api.call_count, 1)

    def test_an_api_error_propagates_to_the_caller(self):
        with patch.object(lc, "gh_api", side_effect=_http_error(403)):
            with self.assertRaises(urllib.error.HTTPError):
                lc.fetch_open_pull_requests("o/r", "tok")


# ---------------------------------------------------------------------------
# fetch_label_names, add_labels
# ---------------------------------------------------------------------------


class TestFetchLabelNames(unittest.TestCase):
    def test_returns_the_names_in_the_api_spelling(self):
        item = {"labels": [{"name": "P1"}, {"name": "verification needed"}]}
        with patch.object(lc, "gh_api", return_value=item) as gh_api:
            self.assertEqual(lc.fetch_label_names("o/r", 7, "tok"), ["P1", "verification needed"])
        self.assertEqual(gh_api.call_args[0][0], "repos/o/r/issues/7")

    def test_no_labels_key_is_no_labels(self):
        with patch.object(lc, "gh_api", return_value={}):
            self.assertEqual(lc.fetch_label_names("o/r", 7, "tok"), [])

    def test_a_non_object_response_raises_runtime_error(self):
        for value in (None, [], "x"):
            with self.subTest(value=value):
                with patch.object(lc, "gh_api", return_value=value):
                    with self.assertRaises(RuntimeError):
                        lc.fetch_label_names("o/r", 7, "tok")

    def test_an_api_error_propagates(self):
        with patch.object(lc, "gh_api", side_effect=_http_error(500)):
            with self.assertRaises(urllib.error.HTTPError):
                lc.fetch_label_names("o/r", 7, "tok")


class TestAddLabels(unittest.TestCase):
    def test_posts_the_labels(self):
        with patch.object(lc, "gh_api") as gh_api:
            lc.add_labels("o/r", 7, ["ci", "p1"], "tok")
        gh_api.assert_called_once_with(
            "repos/o/r/issues/7/labels", token="tok", method="POST", body={"labels": ["ci", "p1"]}
        )

    def test_an_api_error_propagates(self):
        with patch.object(lc, "gh_api", side_effect=_http_error(422)):
            with self.assertRaises(urllib.error.HTTPError):
                lc.add_labels("o/r", 7, ["ci"], "tok")


# ---------------------------------------------------------------------------
# remove_labels
# ---------------------------------------------------------------------------


def _fail_on(label_fragment: str):
    """A gh_api stand-in that records paths and raises a 500 for one label."""
    paths: list[str] = []

    def fake_api(path, token, method="GET", body=None):
        paths.append(path)
        if label_fragment in path:
            raise _http_error(500)

    return fake_api, paths


class TestRemoveLabels(unittest.TestCase):
    def test_calls_delete_for_each_label(self):
        with patch.object(lc, "gh_api") as mock_api:
            result = lc.remove_labels(42, ["p2", "p3"], "owner/repo", "tok")
        paths = [c[0][0] for c in mock_api.call_args_list]
        self.assertEqual(
            paths, ["repos/owner/repo/issues/42/labels/p2", "repos/owner/repo/issues/42/labels/p3"]
        )
        self.assertTrue(all(c.kwargs["method"] == "DELETE" for c in mock_api.call_args_list))
        self.assertTrue(result)

    def test_no_calls_when_list_empty(self):
        with patch.object(lc, "gh_api") as mock_api:
            result = lc.remove_labels(1, [], "owner/repo", "tok")
        mock_api.assert_not_called()
        self.assertTrue(result)

    def test_a_404_is_already_gone_not_a_failure(self):
        with patch.object(lc, "gh_api", side_effect=_http_error(404)):
            self.assertTrue(lc.remove_labels(1, ["p2"], "owner/repo", "tok"))

    def test_another_http_error_is_a_failure(self):
        with patch.object(lc, "gh_api", side_effect=_http_error(422)):
            self.assertFalse(lc.remove_labels(1, ["p2"], "owner/repo", "tok"))

    def test_a_url_error_is_a_failure(self):
        with patch.object(lc, "gh_api", side_effect=urllib.error.URLError("network error")):
            self.assertFalse(lc.remove_labels(1, ["p2"], "owner/repo", "tok"))

    def test_continues_after_one_failure(self):
        fake_api, paths = _fail_on("p2")
        with patch.object(lc, "gh_api", side_effect=fake_api):
            result = lc.remove_labels(1, ["p1", "p2", "p3"], "owner/repo", "tok")
        self.assertTrue(any(p.endswith("/p3") for p in paths))
        self.assertFalse(result)

    def test_url_encodes_the_label(self):
        with patch.object(lc, "gh_api") as mock_api:
            lc.remove_labels(5, ["verification needed", "a/b"], "owner/repo", "tok")
        paths = [c[0][0] for c in mock_api.call_args_list]
        self.assertEqual(
            paths,
            [
                "repos/owner/repo/issues/5/labels/verification%20needed",
                "repos/owner/repo/issues/5/labels/a%2Fb",
            ],
        )

    def test_default_reason_says_conflicting(self):
        out = io.StringIO()
        with patch.object(lc, "gh_api"), redirect_stdout(out):
            lc.remove_labels(1, ["p2"], "owner/repo", "tok")
        self.assertIn("Removed conflicting label 'p2' from #1.", out.getvalue())

    def test_custom_reason_replaces_conflicting_in_the_log_line(self):
        out = io.StringIO()
        with patch.object(lc, "gh_api"), redirect_stdout(out):
            lc.remove_labels(1, ["agents"], "owner/repo", "tok", reason="unjustified")
        self.assertIn("Removed unjustified label 'agents' from #1.", out.getvalue())
        self.assertNotIn("conflicting", out.getvalue())


# ---------------------------------------------------------------------------
# write_output
# ---------------------------------------------------------------------------


class TestWriteOutput(unittest.TestCase):
    def test_appends_name_equals_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "output")
            with open(path, "w", encoding="utf-8") as f:
                f.write("earlier=1\n")
            with patch.dict(os.environ, {"GITHUB_OUTPUT": path}):
                lc.write_output("removed", '["p2"]')
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), 'earlier=1\nremoved=["p2"]\n')

    def test_does_nothing_outside_actions(self):
        with patch.dict(os.environ, {"GITHUB_OUTPUT": ""}):
            with patch("builtins.open") as mock_open:
                lc.write_output("removed", "[]")
        mock_open.assert_not_called()


# ---------------------------------------------------------------------------
# Table parsers
# ---------------------------------------------------------------------------


class TestLoadJson(unittest.TestCase):
    def test_parses_json(self):
        self.assertEqual(lc.load_json("X", '{"a": [1]}'), {"a": [1]})

    def test_blank_is_the_default(self):
        for text in ("", "  \n"):
            with self.subTest(text=text):
                self.assertEqual(lc.load_json("X", text, default=[]), [])

    def test_blank_without_a_default_is_required(self):
        with self.assertRaisesRegex(lc.InputError, "X is required"):
            lc.load_json("X", " ")

    def test_invalid_json_names_the_input(self):
        with self.assertRaisesRegex(lc.InputError, "^X is not valid JSON"):
            lc.load_json("X", "[p1, p2]")

    def test_input_error_is_a_value_error(self):
        self.assertTrue(issubclass(lc.InputError, ValueError))


class TestParseLabelList(unittest.TestCase):
    def test_lowercases_and_strips(self):
        self.assertEqual(
            lc.parse_label_list("L", '["Orchestrating", " verified "]'),
            frozenset({"orchestrating", "verified"}),
        )

    def test_duplicates_collapse(self):
        self.assertEqual(lc.parse_label_list("L", '["a", "A"]'), frozenset({"a"}))

    def test_blank_is_empty_unless_required(self):
        self.assertEqual(lc.parse_label_list("L", ""), frozenset())
        with self.assertRaises(lc.InputError):
            lc.parse_label_list("L", "", required=True)

    def test_required_refuses_an_empty_array(self):
        with self.assertRaisesRegex(lc.InputError, "at least one"):
            lc.parse_label_list("L", "[]", required=True)

    def test_refuses_a_non_array(self):
        for text in ('"p1"', '{"p1": 1}', "1"):
            with self.subTest(text=text):
                with self.assertRaisesRegex(lc.InputError, "^L must be a JSON array"):
                    lc.parse_label_list("L", text)

    def test_refuses_a_non_string_or_blank_entry(self):
        for text in ("[1]", "[null]", '[""]', '["  "]', '[["a"]]'):
            with self.subTest(text=text):
                with self.assertRaises(lc.InputError):
                    lc.parse_label_list("L", text)


class TestParseLabelSets(unittest.TestCase):
    def test_lowercases_each_set(self):
        self.assertEqual(
            lc.parse_label_sets("S", '[["P1", "p2"], ["Verified", "verification needed"]]'),
            [frozenset({"p1", "p2"}), frozenset({"verified", "verification needed"})],
        )

    def test_blank_is_no_sets(self):
        self.assertEqual(lc.parse_label_sets("S", ""), [])

    def test_refuses_a_set_of_fewer_than_two_labels(self):
        for text in ("[[]]", '[["p1"]]', '[["p1", "P1"]]'):
            with self.subTest(text=text):
                with self.assertRaisesRegex(lc.InputError, "at least two"):
                    lc.parse_label_sets("S", text)

    def test_refuses_the_wrong_shape(self):
        for text in ('["p1", "p2"]', '{"a": ["p1", "p2"]}', '[["p1", 2]]'):
            with self.subTest(text=text):
                with self.assertRaises(lc.InputError):
                    lc.parse_label_sets("S", text)


class TestParsePrefixes(unittest.TestCase):
    def test_lowercases_and_keeps_order(self):
        self.assertEqual(lc.parse_prefixes("P", '["C-A-", "c-r-"]'), ["c-a-", "c-r-"])

    def test_blank_is_none(self):
        self.assertEqual(lc.parse_prefixes("P", ""), [])

    def test_refuses_the_wrong_shape(self):
        for text in ('"c-a-"', '[""]', "[1]"):
            with self.subTest(text=text):
                with self.assertRaises(lc.InputError):
                    lc.parse_prefixes("P", text)


class TestParseSnoozeLabels(unittest.TestCase):
    def test_keeps_the_callers_spelling(self):
        self.assertEqual(
            lc.parse_snooze_labels("N", '{"Snooze 3 Days": 3, " snooze 7 days ": 7}'),
            {"Snooze 3 Days": 3, "snooze 7 days": 7},
        )

    def test_is_required(self):
        for text in ("", "{}"):
            with self.subTest(text=text):
                with self.assertRaises(lc.InputError):
                    lc.parse_snooze_labels("N", text)

    def test_refuses_a_non_object(self):
        with self.assertRaises(lc.InputError):
            lc.parse_snooze_labels("N", '[["snooze 3 days", 3]]')

    def test_refuses_days_that_are_not_a_positive_whole_number(self):
        for days in ("true", "false", "0", "-1", "1.5", '"3"', "null"):
            with self.subTest(days=days):
                with self.assertRaisesRegex(lc.InputError, "whole number of days"):
                    lc.parse_snooze_labels("N", '{"snooze": %s}' % days)

    def test_refuses_a_label_listed_twice_in_another_case(self):
        with self.assertRaisesRegex(lc.InputError, "listed twice"):
            lc.parse_snooze_labels("N", '{"snooze 3 days": 3, "Snooze 3 Days": 3}')

    def test_refuses_a_blank_label(self):
        with self.assertRaises(lc.InputError):
            lc.parse_snooze_labels("N", '{" ": 3}')


# ---------------------------------------------------------------------------
# ExclusiveRules
# ---------------------------------------------------------------------------

_SETS = [
    frozenset({"p1", "p2", "p3"}),
    frozenset({"verification needed", "verified"}),
]


class TestExclusiveRules(unittest.TestCase):
    def setUp(self):
        self.rules = lc.ExclusiveRules(_SETS, ["c-a-", "test-failure"])

    def test_parse_reads_both_tables(self):
        rules = lc.ExclusiveRules.parse("S", '[["P1", "p2"]]', "P", '["C-A-"]')
        self.assertEqual(rules, lc.ExclusiveRules([frozenset({"p1", "p2"})], ["c-a-"]))

    def test_parse_reports_the_bad_tables_name(self):
        with self.assertRaisesRegex(lc.InputError, "^P "):
            lc.ExclusiveRules.parse("S", "[]", "P", "{}")

    def test_is_false_when_empty(self):
        self.assertFalse(lc.ExclusiveRules())
        self.assertTrue(lc.ExclusiveRules(prefixes=["c-a-"]))

    def test_governs_a_set_member_or_a_prefixed_label_case_insensitively(self):
        for label in ("p1", "P2", "Verified", "c-a-opus", "C-A-Opus", "test-failure-archive"):
            with self.subTest(label=label):
                self.assertTrue(self.rules.governs(label))

    def test_does_not_govern_others(self):
        for label in ("bug", "", "c-a", "automated tests", "p4"):
            with self.subTest(label=label):
                self.assertFalse(self.rules.governs(label))

    def test_conflicts_with_the_other_members_of_its_set(self):
        self.assertEqual(self.rules.conflicts("p1", ["p2", "ci", "p3"]), ["p2", "p3"])

    def test_never_conflicts_with_itself_in_any_case(self):
        self.assertEqual(self.rules.conflicts("P1", ["p1", "p2"]), ["p2"])

    def test_keeps_the_current_spelling(self):
        self.assertEqual(self.rules.conflicts("p1", ["P2", "ci"]), ["P2"])

    def test_conflicts_by_prefix(self):
        self.assertEqual(
            self.rules.conflicts("test-failure-archive", ["test-failure", "automated tests"]),
            ["test-failure"],
        )
        self.assertEqual(
            self.rules.conflicts("C-A-Opus", ["c-a-sonnet", "c-r-opus"]), ["c-a-sonnet"]
        )

    def test_a_partial_prefix_is_not_a_member(self):
        self.assertEqual(self.rules.conflicts("c-a", ["c-a-opus"]), [])

    def test_nothing_conflicts_with_an_ungoverned_label(self):
        self.assertEqual(self.rules.conflicts("bug", ["p1", "c-a-opus"]), [])

    def test_no_current_labels_means_no_conflicts(self):
        self.assertEqual(self.rules.conflicts("p1", []), [])

    def test_every_set_the_label_is_in_counts(self):
        rules = lc.ExclusiveRules([frozenset({"a", "b"}), frozenset({"a", "c"})])
        self.assertEqual(rules.conflicts("a", ["b", "c", "d"]), ["b", "c"])

    def test_every_prefix_the_label_has_counts(self):
        rules = lc.ExclusiveRules(prefixes=["x-", "x-y-"])
        self.assertEqual(rules.conflicts("x-y-1", ["x-z", "x-y-2"]), ["x-z", "x-y-2"])

    def test_a_label_in_both_a_set_and_a_prefix_group_is_listed_once(self):
        rules = lc.ExclusiveRules([frozenset({"c-a-opus", "c-a-haiku"})], ["c-a-"])
        self.assertEqual(
            rules.conflicts("c-a-opus", ["c-a-haiku", "c-a-sonnet"]), ["c-a-haiku", "c-a-sonnet"]
        )

    def test_a_label_repeated_in_current_is_listed_once(self):
        self.assertEqual(self.rules.conflicts("p1", ["p2", "p2"]), ["p2"])


if __name__ == "__main__":
    unittest.main()
