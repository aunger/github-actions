#!/usr/bin/env python3
"""Unit tests for github_api.py."""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))
import github_api


def respond(payload: bytes, link: str | None = None) -> MagicMock:
    response = MagicMock()
    response.__enter__.return_value.read.return_value = payload
    response.__enter__.return_value.headers = {"Link": link} if link is not None else {}
    return response


class GhApiTest(unittest.TestCase):
    @patch("urllib.request.urlopen")
    def test_get_sends_the_github_headers_and_a_timeout(self, mock_urlopen):
        mock_urlopen.return_value = respond(b'{"a": 1}')
        self.assertEqual({"a": 1}, github_api.gh_api("repos/o/r", "tok"))
        request = mock_urlopen.call_args.args[0]
        self.assertEqual("https://api.github.com/repos/o/r", request.full_url)
        self.assertEqual("Bearer tok", request.get_header("Authorization"))
        self.assertEqual("GET", request.get_method())
        self.assertEqual(github_api.TIMEOUT_SECONDS, mock_urlopen.call_args.kwargs["timeout"])

    @patch("urllib.request.urlopen")
    def test_an_empty_token_sends_no_authorization(self, mock_urlopen):
        mock_urlopen.return_value = respond(b"[]")
        github_api.gh_api("repos/o/r/pulls", "")
        self.assertIsNone(mock_urlopen.call_args.args[0].get_header("Authorization"))

    @patch("urllib.request.urlopen")
    def test_a_body_is_sent_as_json(self, mock_urlopen):
        mock_urlopen.return_value = respond(b"{}")
        github_api.gh_api("repos/o/r/issues/1", "tok", method="PATCH", body={"labels": ["x"]})
        request, data = mock_urlopen.call_args.args
        self.assertEqual("PATCH", request.get_method())
        self.assertEqual("application/json", request.get_header("Content-type"))
        self.assertEqual({"labels": ["x"]}, json.loads(data))

    @patch("urllib.request.urlopen")
    def test_an_empty_response_returns_none(self, mock_urlopen):
        mock_urlopen.return_value = respond(b"")
        self.assertIsNone(github_api.gh_api("repos/o/r/x", "tok", method="DELETE"))


    @patch("urllib.request.urlopen")
    def test_a_write_need_not_read_its_response(self, mock_urlopen):
        response = respond(b"{trunc")
        mock_urlopen.return_value = response
        self.assertIsNone(
            github_api.gh_api(
                "repos/o/r/issues/1", "tok", method="PATCH", body={}, read_response=False
            )
        )
        response.__enter__.return_value.read.assert_not_called()


class GhApiPageTest(unittest.TestCase):
    @patch("urllib.request.urlopen")
    def test_a_next_link_means_another_page(self, mock_urlopen):
        mock_urlopen.return_value = respond(
            b"[1]",
            '<https://api.github.com/x?page=3>; rel="next", '
            '<https://api.github.com/x?page=9>; rel="last"',
        )
        self.assertEqual(([1], True), github_api.gh_api_page("x?page=2", "tok"))
        self.assertEqual("GET", mock_urlopen.call_args.args[0].get_method())

    @patch("urllib.request.urlopen")
    def test_no_next_link_means_the_last_page(self, mock_urlopen):
        mock_urlopen.return_value = respond(
            b"[1]",
            '<https://api.github.com/x?page=1>; rel="first", '
            '<https://api.github.com/x?page=8>; rel="prev"',
        )
        self.assertEqual(([1], False), github_api.gh_api_page("x?page=9", "tok"))

    @patch("urllib.request.urlopen")
    def test_no_link_header_means_a_single_page(self, mock_urlopen):
        mock_urlopen.return_value = respond(b"[]")
        self.assertEqual(([], False), github_api.gh_api_page("x", "tok"))


if __name__ == "__main__":
    unittest.main()
