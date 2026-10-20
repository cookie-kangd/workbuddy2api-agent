"""Realm x model entitlement marks (#505).

The sidecar is one-way: a 11158 refusal marks the model, a success clears it,
and 11102/11134 are never stored. Marks expire so a stale label cannot
survive an upstream change.

    python tests/_test_entitlements.py
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import wb_entitlements as E


class EntitlementTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wb-entitlements-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_mark_round_trips(self):
        E.mark(self.dir, "intl", "gpt-6-astra", 11158, "personal_free_user",
               now=1000.0)
        ent = E.entitlement_for(self.dir, "intl", "gpt-6-astra", now=1001.0)
        self.assertEqual(ent["code"], 11158)
        self.assertEqual(ent["userType"], "personal_free_user")
        self.assertEqual(ent["source"], "runtime")

    def test_a_success_clears_only_that_model(self):
        E.mark(self.dir, "intl", "gpt-6-astra", now=1000.0)
        E.mark(self.dir, "intl", "gpt-5.5", now=1000.0)
        self.assertTrue(E.clear(self.dir, "intl", "gpt-6-astra"))
        self.assertIsNone(E.entitlement_for(self.dir, "intl", "gpt-6-astra", now=1001.0))
        self.assertIsNotNone(E.entitlement_for(self.dir, "intl", "gpt-5.5", now=1001.0))
        self.assertFalse(E.clear(self.dir, "intl", "gpt-6-astra"))

    def test_expired_marks_disappear(self):
        E.mark(self.dir, "intl", "gpt-6-astra", now=1000.0)
        self.assertIsNone(E.entitlement_for(self.dir, "intl", "gpt-6-astra",
                                            now=1000.0 + E.TTL + 1))

    def test_snapshot_is_realm_scoped(self):
        E.mark(self.dir, "intl", "gpt-6-astra", now=1000.0)
        E.mark(self.dir, "cn", "gpt-5.5", now=1000.0)
        intl = E.snapshot(self.dir, "intl", now=1001.0)
        self.assertIn("gpt-6-astra", intl["intl"])
        self.assertNotIn("cn", intl)

    def test_user_type_is_read_from_the_field_or_the_message(self):
        self.assertEqual(
            E.user_type_from_detail(json.dumps({
                "code": 11158, "userType": "personal_free_user"})),
            "personal_free_user")
        self.assertEqual(
            E.user_type_from_detail(json.dumps({
                "code": 11158,
                "msg": "model [x] is not allowed for user type [personal_free_user]"})),
            "personal_free_user")

    def test_the_gateway_marks_clears_and_serves_the_field(self):
        with open(os.path.join(ROOT, "wb_proxy.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("wb_entitlements.mark(", src)
        self.assertIn("wb_entitlements.clear(", src)
        self.assertIn('m["entitlement"] = ent', src)

    def test_a_corrupt_file_is_ignored(self):
        with open(E.path(self.dir), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(E.snapshot(self.dir), {})
        E.mark(self.dir, "intl", "m", now=1000.0)
        self.assertIsNotNone(E.entitlement_for(self.dir, "intl", "m", now=1001.0))


if __name__ == "__main__":
    unittest.main()
