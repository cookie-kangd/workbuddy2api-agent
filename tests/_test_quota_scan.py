"""模型额度定时主动查询（wb_quota_scan）的行为约束。

窗口探针（wb_window_probe）只能处理「已经撞到 429 的配额线」：在那之前没人
知道一条线离限额还有多远。这个模块按固定间隔（6 小时）主动发一个最小请求
去看，并遵守一条硬规则：**本地已经有登记的 429 记录的配额线不再主动查询**，
那条线由窗口探针接管。

这里钉住五件事：
  1. 未勾选任何模型时什么都不做（默认是关的）；
  2. 到点才查：成功一次后要等满一个间隔，被等待窗口接管的组合不再出现；
  3. 查到 429 + 重置时刻：立刻记模型冷却，并把窗口登记给探针；之后不再查询；
  4. 网络/解析失败不算查过：短间隔重试，连续失败到上限才推迟到下一个周期；
  5. 记录持久化、账号不可用/冷却中的组合被跳过。

不联网：探针函数被换成桩，账号是合成的。
"""
import atexit
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

_startup_dir = tempfile.TemporaryDirectory(prefix="quota-scan-")
atexit.register(_startup_dir.cleanup)
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts as accounts
import wb_quota_scan as scan
import wb_settings as settings
import wb_window_probe as probe


def reset_state():
    """丢掉模块内缓存，模拟「换一个进程再看这份状态文件」。"""
    with scan._lock:
        scan._state["dir"] = None
        scan._state["scans"] = {}
    with probe._lock:
        probe._state["dir"] = None
        probe._state["windows"] = {}


class FakePool(object):
    """只实现 run_due 用到的两个属性：dir 与 accounts。"""

    def __init__(self, directory, account_list):
        self.dir = directory
        self.accounts = list(account_list)


class QuotaScanTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="quota-scan-state-")
        reset_state()
        patches = [
            mock.patch.object(scan, "SCAN_GAP", 0),
            mock.patch.object(scan, "FIRST_DELAY", 0),
            mock.patch.object(scan, "FIRST_STAGGER", 0),
            mock.patch.object(probe, "PROBE_GAP", 0),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.logs = []

    def log(self, msg):
        self.logs.append(msg)

    def account(self, uid="uid-a", enabled=True, token="token"):
        acc = accounts.Account({"uid": uid, "realm": "intl", "accessToken": token})
        acc.enabled = enabled
        return acc

    # ------------------------------------------------------------ 开关（勾选）

    def test_nothing_is_scanned_when_no_model_is_watched(self):
        self.assertEqual(settings.quota_scan_models(self.dir), [])
        now = time.time()
        acc = self.account()
        with mock.patch.object(probe, "_probe", return_value=(True, None, "")) as fake:
            sent = scan.run_due(FakePool(self.dir, [acc]), log=self.log, now=now)
        self.assertEqual(sent, 0)
        fake.assert_not_called()
        # 没有勾选就不该留下任何状态。
        self.assertFalse(os.path.exists(scan._state_path(self.dir)))

    def test_watch_list_is_cleaned_and_persisted(self):
        self.assertEqual(
            settings.set_quota_scan_models(self.dir, ["glm-5.3", " glm-5.3 ", "", "gpt-6-astra"]),
            ["glm-5.3", "gpt-6-astra"])
        self.assertEqual(settings.quota_scan_models(self.dir), ["glm-5.3", "gpt-6-astra"])
        # 手改过的 settings.json 常写成逗号串，两种形状都要能读回来。
        settings.set_quota_scan_models(self.dir, "glm-5.3, hy3 ;hy3")
        self.assertEqual(settings.quota_scan_models(self.dir), ["glm-5.3", "hy3"])

    # ------------------------------------------------------------ 场景一：正常

    def test_ok_scan_pushes_next_check_one_interval_out(self):
        now = time.time()
        settings.set_quota_scan_models(self.dir, ["glm-5.3"])
        acc = self.account()

        with mock.patch.object(probe, "_probe", return_value=(True, None, "")) as fake:
            sent = scan.run_due(FakePool(self.dir, [acc]), log=self.log, now=now)

        self.assertEqual(sent, 1)
        fake.assert_called_once()
        rows = scan.snapshot(self.dir, now=now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "glm-5.3")
        self.assertEqual(rows[0]["last_result"], "ok")
        self.assertEqual(rows[0]["due_in"], scan.SCAN_INTERVAL)

        # 间隔没到就什么都不查。
        with mock.patch.object(probe, "_probe", return_value=(True, None, "")) as fake:
            sent = scan.run_due(FakePool(self.dir, [acc]), log=self.log,
                                now=now + scan.SCAN_INTERVAL - 60)
        self.assertEqual(sent, 0)
        fake.assert_not_called()

        # 一个间隔之后再查一次。
        with mock.patch.object(probe, "_probe", return_value=(True, None, "")) as fake:
            sent = scan.run_due(FakePool(self.dir, [acc]), log=self.log,
                                now=now + scan.SCAN_INTERVAL + 1)
        self.assertEqual(sent, 1)
        fake.assert_called_once()

    def test_scan_state_survives_a_restart(self):
        now = time.time()
        settings.set_quota_scan_models(self.dir, ["glm-5.3"])
        acc = self.account()
        with mock.patch.object(probe, "_probe", return_value=(True, None, "")):
            scan.run_due(FakePool(self.dir, [acc]), log=self.log, now=now)

        reset_state()
        rows = scan.snapshot(self.dir, now=now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["last_result"], "ok")
        # 重启不该让整张表重来一遍：间隔没到依然不查。
        with mock.patch.object(probe, "_probe", return_value=(True, None, "")) as fake:
            sent = scan.run_due(FakePool(self.dir, [acc]), log=self.log, now=now + 60)
        self.assertEqual(sent, 0)
        fake.assert_not_called()

    # ------------------------------------------------- 场景二：已经登记的 429

    def test_window_record_removes_the_line_from_the_scan_queue(self):
        now = time.time()
        settings.set_quota_scan_models(self.dir, ["glm-5.3"])
        acc = self.account()
        with mock.patch.object(probe, "_probe", return_value=(True, None, "")):
            self.assertEqual(scan.run_due(FakePool(self.dir, [acc]), log=self.log,
                                          now=now), 1)

        # 这条配额线被真实请求撞到 429，窗口探针接管了它。
        self.assertTrue(probe.schedule(self.dir, acc.uid, "glm-5.3", now + 300, now=now))
        with mock.patch.object(probe, "_probe", return_value=(True, None, "")) as fake:
            sent = scan.run_due(FakePool(self.dir, [acc]), log=self.log,
                                now=now + scan.SCAN_INTERVAL + 1)
        self.assertEqual(sent, 0)
        fake.assert_not_called()
        rows = scan.snapshot(self.dir, now=now + scan.SCAN_INTERVAL + 1)
        self.assertTrue(rows[0]["pending"])

        # 窗口消失（探针把新一轮抢了回来）之后，轮询恢复。
        probe.drop(self.dir, acc.uid, "glm-5.3")
        with mock.patch.object(probe, "_probe", return_value=(True, None, "")) as fake:
            sent = scan.run_due(FakePool(self.dir, [acc]), log=self.log,
                                now=now + scan.SCAN_INTERVAL + 1)
        self.assertEqual(sent, 1)
        fake.assert_called_once()

    def test_cooling_model_is_never_probed_twice(self):
        now = time.time()
        settings.set_quota_scan_models(self.dir, ["glm-5.3"])
        acc = self.account()
        # 上游给过一次没有重置时刻的 429：模型冷却就此留下，查也白查。
        acc.note_error("HTTP 429", model="glm-5.3", until=now + 600)
        with mock.patch.object(probe, "_probe", return_value=(True, None, "")) as fake:
            sent = scan.run_due(FakePool(self.dir, [acc]), log=self.log, now=now)
        self.assertEqual(sent, 0)
        fake.assert_not_called()

    # ------------------------------------------- 场景三：一查就撞到限额（429）

    def test_throttled_scan_cools_the_model_and_hands_over_the_window(self):
        now = time.time()
        settings.set_quota_scan_models(self.dir, ["glm-5.3"])
        acc = self.account()
        reset_at = now + 4 * 3600

        with mock.patch.object(probe, "_probe", return_value=(False, reset_at, "6004")):
            sent = scan.run_due(FakePool(self.dir, [acc]), log=self.log, now=now)

        self.assertEqual(sent, 1)
        # 记了模型冷却：真实请求不会再撞这条已满的线。
        self.assertGreater(acc.throttle_wait("glm-5.3"), 0)
        # 窗口登记出去了：到点由探针抢占新一轮计时。
        windows = probe.snapshot(self.dir, now=now)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["model"], "glm-5.3")
        rows = scan.snapshot(self.dir, now=now)
        self.assertEqual(rows[0]["last_result"], "throttled")

        # 已经有 429 记录 -> 不再主动查询。
        with mock.patch.object(probe, "_probe", return_value=(True, None, "")) as fake:
            sent = scan.run_due(FakePool(self.dir, [acc]), log=self.log,
                                now=now + scan.SCAN_INTERVAL + 1)
        self.assertEqual(sent, 0)
        fake.assert_not_called()

    # ------------------------------------------------------ 场景四：查询失败

    def test_failed_scan_retries_soon_and_keeps_the_interval_intact(self):
        now = time.time()
        settings.set_quota_scan_models(self.dir, ["glm-5.3"])
        acc = self.account()

        with mock.patch.object(probe, "_probe", return_value=(False, None, "timeout")):
            sent = scan.run_due(FakePool(self.dir, [acc]), log=self.log, now=now)

        self.assertEqual(sent, 1)
        rows = scan.snapshot(self.dir, now=now)
        self.assertEqual(rows[0]["last_result"], "error")
        # 失败不算查过：下一次是这个延迟之后，而不是一个完整间隔。
        self.assertEqual(rows[0]["due_in"], scan.RETRY_DELAY)
        self.assertEqual(rows[0]["last_scan"], 0)

        with mock.patch.object(probe, "_probe", return_value=(False, None, "timeout")) as fake:
            sent = scan.run_due(FakePool(self.dir, [acc]), log=self.log,
                                now=now + scan.RETRY_DELAY + 1)
        self.assertEqual(sent, 1)
        fake.assert_called_once()

    def test_repeated_failure_defers_to_the_next_interval(self):
        now = time.time()
        settings.set_quota_scan_models(self.dir, ["glm-5.3"])
        acc = self.account()
        stamp = now
        for _ in range(scan.MAX_ATTEMPTS - 1):
            with mock.patch.object(probe, "_probe", return_value=(False, None, "boom")):
                scan.run_due(FakePool(self.dir, [acc]), log=self.log, now=stamp)
            stamp += scan.RETRY_DELAY + 1
        self.assertEqual(scan._row(self.dir, acc.uid, "glm-5.3")["last_scan"], 0)

        with mock.patch.object(probe, "_probe", return_value=(False, None, "boom")):
            scan.run_due(FakePool(self.dir, [acc]), log=self.log, now=stamp)
        rows = scan.snapshot(self.dir, now=stamp)
        self.assertGreater(rows[0]["last_scan"], 0)
        self.assertEqual(rows[0]["due_in"], scan.SCAN_INTERVAL)

    # ------------------------------------------------------------ 账号与选择

    def test_unusable_accounts_are_skipped(self):
        now = time.time()
        settings.set_quota_scan_models(self.dir, ["glm-5.3"])
        disabled = self.account(uid="uid-off", enabled=False)
        tokenless = self.account(uid="uid-notoken", token="")
        self.assertEqual(tokenless.access_token, "")

        pool = FakePool(self.dir, [disabled, tokenless])
        with mock.patch.object(probe, "_probe", return_value=(True, None, "")) as fake:
            scan.run_due(pool, log=self.log, now=now)
        fake.assert_not_called()

    def test_account_removed_from_pool_keeps_its_record(self):
        now = time.time()
        settings.set_quota_scan_models(self.dir, ["glm-5.3"])
        acc = self.account()
        with mock.patch.object(probe, "_probe", return_value=(True, None, "")):
            self.assertEqual(scan.run_due(FakePool(self.dir, [acc]), log=self.log,
                                          now=now), 1)
        with mock.patch.object(probe, "_probe", return_value=(True, None, "")) as fake:
            sent = scan.run_due(FakePool(self.dir, []), log=self.log,
                                now=now + scan.SCAN_INTERVAL + 1)
        self.assertEqual(sent, 0)
        fake.assert_not_called()
        # 账号回来之后接着查，间隔不会被重置。
        with mock.patch.object(probe, "_probe", return_value=(True, None, "")):
            sent = scan.run_due(FakePool(self.dir, [acc]), log=self.log,
                                now=now + scan.SCAN_INTERVAL + 1)
        self.assertEqual(sent, 1)

    def test_first_checks_are_staggered(self):
        """一次勾上一堆模型，不能在同一瞬间把所有请求都打上去。"""
        now = time.time()
        settings.set_quota_scan_models(self.dir, ["m1", "m2", "m3"])
        with mock.patch.object(scan, "FIRST_DELAY", 5.0), \
             mock.patch.object(scan, "FIRST_STAGGER", 15.0):
            targets = scan.due_targets(FakePool(self.dir, [self.account()]), now=now)
            rows = scan.snapshot(self.dir, now=now)
        self.assertEqual(sorted(row["due_in"] for row in rows), [5, 20, 35])
        # 排在最前面的还在 5 秒之后，所以这一轮一个都不查。
        self.assertEqual(targets, [])

    def test_batch_limit_is_respected(self):
        now = time.time()
        uid_a = self.account(uid="uid-a")
        uid_b = self.account(uid="uid-b")
        settings.set_quota_scan_models(self.dir, ["m1", "m2"])
        with mock.patch.object(probe, "_probe", return_value=(True, None, "")) as fake:
            sent = scan.run_due(FakePool(self.dir, [uid_a, uid_b]), log=self.log,
                                now=now, limit=3)
        self.assertEqual(sent, 3)
        self.assertEqual(fake.call_count, 3)

    def test_corrupt_state_file_is_ignored(self):
        with open(scan._state_path(self.dir), "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        reset_state()
        self.assertEqual(scan.snapshot(self.dir), [])
        settings.set_quota_scan_models(self.dir, ["glm-5.3"])
        with mock.patch.object(probe, "_probe", return_value=(True, None, "")):
            self.assertEqual(
                scan.run_due(FakePool(self.dir, [self.account()]), log=self.log), 1)

    def test_state_file_with_bom_is_still_read(self):
        now = time.time()
        settings.set_quota_scan_models(self.dir, ["glm-5.3"])
        with mock.patch.object(probe, "_probe", return_value=(True, None, "")):
            scan.run_due(FakePool(self.dir, [self.account()]), log=self.log, now=now)
        path = scan._state_path(self.dir)
        with open(path, "rb") as fh:
            raw = fh.read()
        with open(path, "wb") as fh:
            fh.write(b"\xef\xbb\xbf" + raw)
        reset_state()
        rows = scan.snapshot(self.dir, now=now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["last_result"], "ok")

    def test_state_file_shape(self):
        now = time.time()
        settings.set_quota_scan_models(self.dir, ["glm-5.3"])
        with mock.patch.object(probe, "_probe", return_value=(True, None, "")):
            scan.run_due(FakePool(self.dir, [self.account()]), log=self.log, now=now)
        with open(scan._state_path(self.dir), encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual(payload["version"], scan.STATE_VERSION)
        self.assertEqual(len(payload["scans"]), 1)
        self.assertEqual(payload["scans"][0]["model"], "glm-5.3")


if __name__ == "__main__":
    unittest.main(verbosity=2)
