"""一键切换所有出站身份：面板工具栏那个按钮背后的那条路由。

    python tests/_test_product_switch.py

按钮提交时不带 uid、也不带 realm，全量切换靠的就是服务端这条分支。它的语义没有别
处钉着，而错了的表现很安静：只切了一个账号、或者一个都没切，面板都提示成功。

Run with: python tests/_test_product_switch.py
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from _isolated_dirs import isolated_data_dirs  # noqa: E402

# Its own accounts/ before wb_proxy reads ACCOUNTS_DIR at import.
_TMP = isolated_data_dirs("wb-product-switch-")

import wb_accounts as A  # noqa: E402
import wb_identity  # noqa: E402
import wb_proxy as proxy  # noqa: E402

UIDS = ("intl-aaa-1", "intl-bbb-2", "cn-ccc-3")


class FakeRequest(object):
    # 该路由现在经 _json_panel 出站（手机号脱敏）；替身只接管最底层的 _json，
    # 让真实的脱敏逻辑照常执行——否则这条用例就不再代表线上行为。
    _json_panel = proxy.Handler._json_panel
    """Just enough of the handler for the route under test."""

    _route_accounts_product = proxy.Handler._route_accounts_product

    def __init__(self, payload):
        self.payload = payload
        self.answering = []

    def _error(self, status, message, kind=""):
        self.answering.append(("error", status, message))
        return status, message

    def _json(self, status, payload):
        self.answering.append(("json", status, payload))
        return status, payload

    def call(self):
        return self._route_accounts_product(self.payload)

    def answered(self):
        return self.answering[-1]


class ProductSwitchTests(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.join(_TMP.name, "accounts")
        os.makedirs(self.dir, exist_ok=True)
        for name in os.listdir(self.dir):
            path = os.path.join(self.dir, name)
            if os.path.isfile(path):
                os.unlink(path)
        self.pool = A.AccountPool(self.dir, log=lambda _m: None)
        for index, uid in enumerate(UIDS):
            self.pool.add(A.Account({
                "uid": uid,
                "nickname": uid,
                "realm": "cn" if index == 2 else "intl",
                "accessToken": "tok-" + uid,
                "product": "workbuddy",
            }))
        proxy.ACCOUNTS_DIR = self.dir
        proxy.POOL = self.pool
        # The route logs every switch; that is the point of the log, but 8 tests
        # of it bury the suite's own output.
        real_log = proxy.log
        proxy.log = lambda *a, **k: None
        self.addCleanup(setattr, proxy, "log", real_log)

    def products(self):
        return dict((a.uid, a.product) for a in self.pool.accounts)

    def reload(self):
        pool = A.AccountPool(self.dir, log=lambda _m: None)
        pool.load()
        return dict((a.uid, a.product) for a in pool.accounts)

    def test_no_uid_switches_every_account(self):
        """工具栏按钮走的就是这条：不带 uid、不带 realm。"""
        status, payload = FakeRequest({"product": "cli"}).call()
        self.assertEqual(status, 200)
        self.assertEqual(payload["product"], "cli")
        self.assertEqual(payload["changed"],
                         [uid[:8] for uid in UIDS])
        self.assertEqual(set(self.products().values()), {"cli"})

    def test_realm_narrows_the_batch(self):
        status, payload = FakeRequest({"product": "vscode", "realm": "intl"}).call()
        self.assertEqual(status, 200)
        self.assertEqual(sorted(payload["changed"]), ["intl-aaa", "intl-bbb"])
        self.assertEqual(self.products()["cn-ccc-3"], "workbuddy")

    def test_realm_all_means_everyone(self):
        status, payload = FakeRequest({"product": "vscode", "realm": "all"}).call()
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["changed"]), 3)
        self.assertEqual(set(self.products().values()), {"vscode"})

    def test_a_single_uid_still_switches_one_account(self):
        """行内那三个按钮（WB / VSC / CLI）不能被批量分支抢走。"""
        status, _payload = FakeRequest({"product": "cli", "uid": "intl-bbb-2"}).call()
        self.assertEqual(status, 200)
        self.assertEqual(self.products()["intl-bbb-2"], "cli")
        self.assertEqual(self.products()["intl-aaa-1"], "workbuddy")
        self.assertEqual(self.products()["cn-ccc-3"], "workbuddy")

    def test_an_unknown_product_is_rejected(self):
        status, _payload = FakeRequest({"product": "vsc"}).call()
        self.assertEqual(status, 400)
        self.assertEqual(set(self.products().values()), {"workbuddy"})

    def test_the_switch_survives_a_restart(self):
        """set_product() 只改记忆体；这条路由必须当场落盘。"""
        FakeRequest({"product": "cli"}).call()
        self.assertEqual(set(self.reload().values()), {"cli"})

    def test_switching_to_the_same_product_reports_no_change(self):
        FakeRequest({"product": "cli"}).call()
        status, payload = FakeRequest({"product": "cli"}).call()
        self.assertEqual(status, 200)
        self.assertEqual(payload["changed"], [])

    def test_every_valid_product_is_accepted(self):
        for product in wb_identity.VALID_PRODUCTS:
            status, payload = FakeRequest({"product": product}).call()
            self.assertEqual(status, 200, product)
            self.assertEqual(payload["product"], product)


class SwitchAccount(object):
    """Just enough Account for _try_switch_product."""

    def __init__(self, product):
        self.uid = "u-auto-switch"
        self.product = product

    def set_product(self, target):
        if target == self.product:
            return False
        self.product = target
        return True


class AutoSwitchTargetTests(unittest.TestCase):
    """The panel-configured allowed identities bound the 429 auto-switch."""

    def setUp(self):
        self.dir = os.path.join(_TMP.name, "auto-switch-accounts")
        os.makedirs(self.dir, exist_ok=True)
        try:
            os.remove(os.path.join(self.dir, "settings.json"))
        except OSError:
            pass
        self._old_dir = proxy.ACCOUNTS_DIR
        proxy.ACCOUNTS_DIR = self.dir
        proxy._SWITCH_LOG.clear()

    def tearDown(self):
        proxy.ACCOUNTS_DIR = self._old_dir
        proxy._SWITCH_LOG.clear()

    def test_default_allows_all_three_in_order(self):
        account = SwitchAccount("workbuddy")
        self.assertTrue(proxy._try_switch_product(account, "m"))
        self.assertEqual(account.product, "vscode")
        self.assertTrue(proxy._try_switch_product(account, "m"))
        self.assertEqual(account.product, "cli")
        self.assertTrue(proxy._try_switch_product(account, "m"))
        self.assertEqual(account.product, "workbuddy")

    def test_only_configured_targets_are_used(self):
        proxy.wb_settings.set_auto_switch_products(self.dir, ["vscode"])
        account = SwitchAccount("workbuddy")
        self.assertTrue(proxy._try_switch_product(account, "m"))
        self.assertEqual(account.product, "vscode")
        self.assertFalse(proxy._try_switch_product(account, "m"),
                         "vscode is the only allowed target and it is current")

    def test_disallowed_targets_are_skipped(self):
        proxy.wb_settings.set_auto_switch_products(self.dir, ["cli"])
        account = SwitchAccount("workbuddy")
        self.assertTrue(proxy._try_switch_product(account, "m"))
        self.assertEqual(account.product, "cli")

    def test_an_empty_allow_list_disables_the_switch(self):
        proxy.wb_settings.set_auto_switch_products(self.dir, [])
        account = SwitchAccount("workbuddy")
        self.assertFalse(proxy._try_switch_product(account, "m"))
        self.assertEqual(account.product, "workbuddy")


class PrivateDataTests(unittest.TestCase):
    """X-Private-Data: auto keeps per-identity defaults, true/false overrides."""

    def setUp(self):
        self.dir = os.path.join(_TMP.name, "private-data")
        os.makedirs(self.dir, exist_ok=True)
        try:
            os.remove(os.path.join(self.dir, "settings.json"))
        except OSError:
            pass
        self._old_dir = proxy.ACCOUNTS_DIR
        proxy.ACCOUNTS_DIR = self.dir

    def tearDown(self):
        proxy.ACCOUNTS_DIR = self._old_dir

    def test_auto_keeps_each_identity_default(self):
        self.assertEqual(proxy.wb_settings.identity_private_data(self.dir), "auto")
        self.assertEqual(
            wb_identity.build_identity_headers(
                "workbuddy", "intl", "u", "t").get("X-Private-Data"), "true")
        self.assertNotIn(
            "X-Private-Data",
            wb_identity.build_identity_headers("vscode", "cn", "u", "t"))
        self.assertEqual(
            wb_identity.build_identity_headers(
                "cli", "intl", "u", "t").get("X-Private-Data"), "false")
        self.assertEqual(
            wb_identity.build_identity_headers(
                "cli", "intl", "u", "t",
                enterprise_id="ent").get("X-Private-Data"), "true")

    def test_explicit_values_override_every_identity(self):
        for product in ("workbuddy", "vscode", "cli"):
            self.assertEqual(
                wb_identity.build_identity_headers(
                    product, "cn", "u", "t",
                    private_data="false").get("X-Private-Data"), "false")
            self.assertEqual(
                wb_identity.build_identity_headers(
                    product, "cn", "u", "t",
                    private_data="true").get("X-Private-Data"), "true")

    def test_account_headers_read_the_setting(self):
        proxy.wb_settings.set_identity_private_data(self.dir, "false")
        account = A.Account(
            {"uid": "u", "accessToken": "t", "realm": "intl",
             "product": "workbuddy"},
            os.path.join(self.dir, "u.json"))
        self.assertEqual(
            account.headers(purpose="chat").get("X-Private-Data"), "false")

    def test_invalid_value_is_rejected(self):
        with self.assertRaises(ValueError):
            proxy.wb_settings.set_identity_private_data(self.dir, "yes")
        self.assertEqual(proxy.wb_settings.identity_private_data(self.dir), "auto")


if __name__ == "__main__":
    unittest.main()
