"""Pinned-account routes: /u/<ref>/v1/... (issue #455)."""
import email
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _isolated_dirs import isolated_data_dirs  # noqa: E402

_TMP = isolated_data_dirs("wb-pinned-")

import wb_accounts  # noqa: E402
import wb_proxy as P  # noqa: E402


def make_account(uid, nickname="", realm="intl", enabled=True):
    return wb_accounts.Account({
        "uid": uid, "accessToken": "t-" + uid, "realm": realm,
        "nickname": nickname, "enabled": enabled,
    })


class DispatchRequest(P.Handler):
    """Drive the real do_GET over an in-memory socket (same shape as the
    panel-route suite)."""

    def __init__(self, path, command="GET"):
        self.path = path
        self.command = command
        self.request_version = "HTTP/1.1"
        self.close_connection = True
        self.headers = email.message_from_string("")
        self.rfile = io.BytesIO(b"")
        self.wfile = io.BytesIO()
        self.status = None
        self.client_address = ("127.0.0.1", 0)

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, key, value):
        pass

    def end_headers(self):
        pass

    def log_message(self, fmt, *args):
        pass

    def dispatch(self):
        getattr(self, "do_" + self.command)()
        raw = self.wfile.getvalue().decode("utf-8")
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = None
        return self.status, payload, raw


class PinnedRouteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pinned-")
        self.addCleanup(self._tmp.cleanup)
        self.directory = self._tmp.name
        self.pool = wb_accounts.AccountPool(self.directory)
        self.pool.accounts = [make_account("abcdef123456", "alpha"),
                              make_account("abcdef999999", "beta")]
        P.reset_restriction_state()
        self.patches = [
            mock.patch.multiple(
                P, POOL=self.pool, API_KEY=None, ACCOUNTS_DIR=self.directory,
                CURRENT_REALM="intl",
                fetch_models=lambda realm=None: [
                    ("m1", {"name": "M1"}), ("m2", {"name": "M2"})]),
            mock.patch.object(P.wb_modelsdev, "refresh_async",
                              lambda *a, **k: None),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self._reset_pin)

    def _reset_pin(self):
        P._PINNED.ref = None
        P._PINNED.account = None

    def dispatch(self, path):
        return DispatchRequest(path).dispatch()

    def test_exact_uid_and_nickname(self):
        status, payload, _ = self.dispatch("/u/abcdef123456/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual([m["id"] for m in payload["data"]], ["m1", "m2"])
        status, _payload, _ = self.dispatch("/u/alpha/v1/models")
        self.assertEqual(status, 200)

    def test_unique_prefix(self):
        status, _payload, _ = self.dispatch("/u/abcdef123/v1/models")
        self.assertEqual(status, 200)

    def test_ambiguous_prefix_is_reported(self):
        status, payload, _ = self.dispatch("/u/abcdef/v1/models")
        self.assertEqual(status, 409)
        self.assertIn("is ambiguous", payload["error"]["message"])

    def test_unknown_is_reported(self):
        status, payload, _ = self.dispatch("/u/nope/v1/models")
        self.assertEqual(status, 404)
        self.assertIn("was not found", payload["error"]["message"])

    def test_legacy_path_is_unchanged(self):
        status, payload, _ = self.dispatch("/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["data"]), 2)

    def test_query_is_preserved(self):
        status, _payload, _ = self.dispatch("/u/abcdef123456/v1/models?x=1")
        self.assertEqual(status, 200)

    def test_keepalive_does_not_leak_the_pin(self):
        self.dispatch("/u/abcdef123456/v1/models")
        self.assertEqual(P.pinned_ref(), "abcdef123456")
        self.dispatch("/v1/models")
        self.assertIsNone(P.pinned_ref())

    def test_models_filter_disabled_for_the_pinned_account(self):
        self.pool.accounts[0].disabled_models = frozenset({"m1"})
        status, payload, _ = self.dispatch("/u/abcdef123456/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual([m["id"] for m in payload["data"]], ["m2"])

    def test_pinned_account_must_be_ready(self):
        account = self.pool.accounts[0]
        account.enabled = False
        P._PINNED.ref = "abcdef123456"
        P._PINNED.account = account
        with self.assertRaises(P.PinnedAccountError) as raised:
            P.open_upstream({"model": "gpt-6.1-sol",
                             "messages": [{"role": "user", "content": "hi"}]},
                            target_realm="intl")
        self.assertEqual(raised.exception.status, 503)

    def test_pinned_403_does_not_rotate(self):
        account = self.pool.accounts[0]
        P._PINNED.ref = "abcdef123456"
        P._PINNED.account = account
        calls = []

        def fake_urlopen(req, timeout=None, proxy=None):
            calls.append(req)
            raise urllib.error.HTTPError(
                req.full_url, 403, "Forbidden", {},
                io.BytesIO(b'{"code":11140,"msg":"request illegal"}'))

        with mock.patch.object(P.wb_accounts, "urlopen", fake_urlopen):
            with self.assertRaises(P.ContentRejected):
                P.open_upstream({"model": "gpt-6.1-sol",
                                 "messages": [{"role": "user", "content": "hi"}]},
                                target_realm="intl")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
