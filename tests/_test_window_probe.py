"""模型额度窗口探针（wb_window_probe）的行为约束。

上游的免费额度窗口按「首次请求」起算：429 给出一个重置时刻，到点之后如果
没有任何请求，新一轮窗口不会开始，额度一直空转。这个模块把重置时刻持久化
下来，到点主动补一个最小请求，把新窗口的计时起点钉在那一刻。

这里钉住四件事：
  1. 登记/去重/持久化，以及「开关关掉就什么都不做」；
  2. 到点执行的四种结果分支：探针成功、上游给了更晚的重置点、上游还没放开、
     连续失败到上限后放弃；
  3. 只清该模型的冷却，账号级 cooldown_until 不被顺手清掉；
  4. 429 响应体的重置时刻解析：线上返回的是中文体，只认英文会拿不到时间。

不联网：探针函数被换成桩，账号是合成的。
"""
import atexit
import calendar
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

_startup_dir = tempfile.TemporaryDirectory(prefix="window-probe-")
atexit.register(_startup_dir.cleanup)
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts as accounts
import wb_settings as settings
import wb_window_probe as probe


def reset_state():
    """丢掉模块内缓存，模拟「换一个进程再看这份状态文件」。"""
    with probe._lock:
        probe._state["dir"] = None
        probe._state["windows"] = {}


class FakePool(object):
    """只实现 run_due 用到的两个属性：dir 与 get(uid)。"""

    def __init__(self, directory, account_list):
        self.dir = directory
        self._accounts = list(account_list)

    def get(self, uid):
        for account in self._accounts:
            if account.uid == uid:
                return account
        return None


class WindowProbeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="window-state-")
        reset_state()
        self.gap = mock.patch.object(probe, "PROBE_GAP", 0)
        self.gap.start()
        self.addCleanup(self.gap.stop)

    def account(self, uid="uid-a"):
        return accounts.Account({"uid": uid, "realm": "intl", "accessToken": "token"})

    # ------------------------------------------------------------ 登记与持久化

    def test_schedule_dedupes_and_persists(self):
        now = time.time()
        self.assertTrue(probe.schedule(self.dir, "uid-a", "glm-5.3", now + 600, now=now))
        # 同一个窗口会被连着撞好几次 429：时间没变就不该重写状态文件。
        self.assertFalse(probe.schedule(self.dir, "uid-a", "glm-5.3", now + 600.5, now=now))
        # 时间变了说明上游开了新的一轮。
        self.assertTrue(probe.schedule(self.dir, "uid-a", "glm-5.3", now + 900, now=now))

        rows = probe.snapshot(self.dir, now=now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "glm-5.3")
        self.assertEqual(rows[0]["uid8"], "uid-a")
        self.assertEqual(rows[0]["reset_in"], 900)

        # 已经过去的重置时刻不登记（否则到点立刻发一次无意义的探针）。
        self.assertFalse(probe.schedule(self.dir, "uid-a", "old", now - 10, now=now))
        # 残缺参数一律拒绝，不能把空 uid 写成一条记录。
        self.assertFalse(probe.schedule(self.dir, "", "m", now + 60, now=now))
        self.assertFalse(probe.schedule(self.dir, "uid-a", "", now + 60, now=now))

        # 换一个进程视角重读：记录必须还在（内存冷却表是易失的，窗口不是）。
        reset_state()
        rows = probe.snapshot(self.dir, now=now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "glm-5.3")

    def test_seconds_until_due_tracks_soonest_window(self):
        now = time.time()
        self.assertIsNone(probe.seconds_until_due(self.dir, now=now))
        probe.schedule(self.dir, "uid-a", "m1", now + 300, now=now)
        probe.schedule(self.dir, "uid-b", "m2", now + 120, now=now)
        self.assertAlmostEqual(probe.seconds_until_due(self.dir, now=now), 120, delta=1)
        # 到点之后返回 0，而不是负数（调度器据此立刻唤醒）。
        self.assertEqual(probe.seconds_until_due(self.dir, now=now + 500), 0.0)

    def test_corrupt_state_file_is_ignored(self):
        with open(probe._state_path(self.dir), "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        reset_state()
        self.assertEqual(probe.snapshot(self.dir), [])
        # 坏文件不该让登记路径跟着挂掉。
        self.assertTrue(probe.schedule(self.dir, "uid-a", "m", time.time() + 60))

    def test_state_file_with_bom_is_still_read(self):
        """记事本一类编辑器会给文件加 BOM，而 json.load 遇到 BOM 直接抛异常——
        那会让整张窗口表静默消失、探针再也不触发。必须以 utf-8-sig 读回来。"""
        now = time.time()
        probe.schedule(self.dir, "uid-a", "m", now + 300, now=now)
        path = probe._state_path(self.dir)
        with open(path, "rb") as fh:
            raw = fh.read()
        with open(path, "wb") as fh:
            fh.write(b"\xef\xbb\xbf" + raw)
        reset_state()
        rows = probe.snapshot(self.dir, now=now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "m")

    def test_pool_load_skips_non_account_files(self):
        """窗口记录与面板设置和账号放在同一目录：加载池子时不能被当成
        「损坏的账号」解析，否则每次启动都刷一条无意义的 error 日志。"""
        for name, body in (("settings.json", '{"api_keys": []}'),
                           ("active_realm.json", '{"realm": "intl"}'),
                           ("model_windows.json", '{"windows": []}')):
            with open(os.path.join(self.dir, name), "w", encoding="utf-8") as fh:
                fh.write(body)
        logged = []
        pool = accounts.AccountPool(self.dir, log=logged.append)
        pool.load()
        self.assertEqual(pool.accounts, [])
        self.assertEqual(logged, [])

    # ------------------------------------------------------------ 开关

    def test_disabled_switch_blocks_schedule_and_run(self):
        now = time.time()
        settings.set_window_probe(self.dir, False)
        self.assertFalse(probe.schedule(self.dir, "uid-a", "m", now + 60, now=now))
        self.assertEqual(probe.snapshot(self.dir, now=now), [])

        settings.set_window_probe(self.dir, True)
        probe.schedule(self.dir, "uid-a", "m", now + 60, now=now)
        # 开关关掉之后，已经登记过的窗口也不该再发探针。
        settings.set_window_probe(self.dir, False)
        with mock.patch.object(probe, "_probe") as fake:
            sent = probe.run_due(FakePool(self.dir, [self.account()]), now=now + 61)
        self.assertEqual(sent, 0)
        fake.assert_not_called()
        # 关闭只是不发请求，记录保留：重新打开后仍能补上。
        self.assertEqual(len(probe.snapshot(self.dir, now=now)), 1)

    def test_switch_defaults_on(self):
        self.assertTrue(settings.window_probe(self.dir))
        settings.set_window_probe(self.dir, False)
        self.assertFalse(settings.window_probe(self.dir))

    # ------------------------------------------------------------ 到点执行

    def test_successful_probe_clears_model_cooldown_only(self):
        now = time.time()
        account = self.account()
        account.note_error("HTTP 429", model="glm-5.3", until=now + 600)
        # 账号级冷却来自另一次网络故障，探针不该把它一起清掉。
        with account._throttle_lock:
            account.cooldown_until = now + 300
        probe.schedule(self.dir, account.uid, "glm-5.3", now + 60, now=now)

        with mock.patch.object(probe, "_probe", return_value=(True, None, "")) as fake:
            sent = probe.run_due(FakePool(self.dir, [account]), now=now + 61)

        self.assertEqual(sent, 1)
        fake.assert_called_once()
        # 模型冷却解除、窗口记录撤销、账号级冷却原样保留。
        self.assertEqual(account.model_cooldowns, {})
        self.assertGreater(account.cooldown_until, now)
        self.assertEqual(probe.snapshot(self.dir, now=now + 61), [])

    def test_fresh_reset_reschedules_window(self):
        now = time.time()
        account = self.account()
        probe.schedule(self.dir, account.uid, "m", now + 60, now=now)
        new_reset = now + 61 + 24 * 3600

        with mock.patch.object(probe, "_probe", return_value=(False, new_reset, "429")):
            sent = probe.run_due(FakePool(self.dir, [account]), now=now + 61)

        self.assertEqual(sent, 1)
        rows = probe.snapshot(self.dir, now=now + 61)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["reset_at"], new_reset, delta=1)
        self.assertEqual(rows[0]["attempts"], 0)

    def test_unreset_window_retries_with_backoff(self):
        now = time.time()
        account = self.account()
        probe.schedule(self.dir, account.uid, "m", now + 60, now=now)

        with mock.patch.object(probe, "_probe", return_value=(False, None, "HTTP 500")):
            probe.run_due(FakePool(self.dir, [account]), now=now + 61)

        # 重试时刻是拿真实时钟算的，所以这里也用真实时钟读快照。
        rows = probe.snapshot(self.dir)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["attempts"], 1)
        self.assertAlmostEqual(rows[0]["reset_in"], probe.RETRY_DELAY, delta=3)

    def test_gives_up_after_max_attempts(self):
        now = time.time()
        account = self.account()
        probe.schedule(self.dir, account.uid, "m", now + 60, now=now)

        with mock.patch.object(probe, "_probe", return_value=(False, None, "boom")):
            for step in range(probe.MAX_ATTEMPTS):
                probe.run_due(FakePool(self.dir, [account]), now=now + 61 + step * 1000)

        # 连续失败到上限后撤销记录，而不是永远骚扰上游；下一次 429 会重新登记。
        self.assertEqual(probe.snapshot(self.dir, now=now + 999999), [])

    def test_missing_account_drops_window(self):
        now = time.time()
        probe.schedule(self.dir, "ghost", "m", now + 60, now=now)
        sent = probe.run_due(FakePool(self.dir, []), now=now + 61)
        self.assertEqual(sent, 0)
        self.assertEqual(probe.snapshot(self.dir, now=now + 61), [])

    def test_disabled_account_is_skipped_but_kept(self):
        now = time.time()
        account = self.account()
        account.enabled = False
        probe.schedule(self.dir, account.uid, "m", now + 60, now=now)

        with mock.patch.object(probe, "_probe") as fake:
            sent = probe.run_due(FakePool(self.dir, [account]), now=now + 61)

        self.assertEqual(sent, 0)
        fake.assert_not_called()
        self.assertEqual(len(probe.snapshot(self.dir, now=now + 61)), 1)

    def test_drop_removes_window(self):
        now = time.time()
        probe.schedule(self.dir, "uid-a", "m", now + 60, now=now)
        self.assertTrue(probe.drop(self.dir, "uid-a", "m"))
        self.assertFalse(probe.drop(self.dir, "uid-a", "m"))
        self.assertEqual(probe.snapshot(self.dir, now=now), [])

    # ------------------------------------------------------------ 探针请求本身

    def test_probe_reads_reset_from_chinese_429_body(self):
        """线上 429 是中文体：只认英文 'reset at' 会永远解析不出重置时刻。"""
        account = self.account()
        body = json.dumps({
            "code": 6004,
            "msg": ("您的使用量已超出频率限制，将在 2026-10-03 16:06:21 UTC+8 重置，"
                    "您也可以切换其他模型继续使用。"),
        }, ensure_ascii=False).encode("utf-8")
        err = urllib.error.HTTPError("https://example.invalid", 429, "429", {},
                                     io.BytesIO(body))
        with mock.patch.object(accounts, "urlopen", side_effect=err):
            ok, reset_at, _detail = probe._probe(account, "hy4-preview-f")

        self.assertFalse(ok)
        want = (calendar.timegm(time.strptime("2026-10-03 16:06:21", "%Y-%m-%d %H:%M:%S"))
                - 8 * 3600)
        self.assertEqual(reset_at, want)

    def test_probe_reads_reset_from_escaped_429_body(self):
        """转发层可能把中文转成 \\uXXXX：兜底路径必须仍然认得出时间戳。"""
        account = self.account()
        body = json.dumps({
            "code": 6004,
            "msg": "您的使用量已超出频率限制，将在 2026-10-03 16:06:21 UTC+8 重置。",
        }, ensure_ascii=True).encode("utf-8")
        err = urllib.error.HTTPError("https://example.invalid", 429, "429", {},
                                     io.BytesIO(body))
        with mock.patch.object(accounts, "urlopen", side_effect=err):
            _ok, reset_at, _detail = probe._probe(account, "hy4-preview-f")
        want = (calendar.timegm(time.strptime("2026-10-03 16:06:21", "%Y-%m-%d %H:%M:%S"))
                - 8 * 3600)
        self.assertEqual(reset_at, want)

    def test_probe_reports_success_without_reset(self):
        account = self.account()
        resp = mock.MagicMock()
        resp.read.return_value = b"data: [DONE]\n\n"
        resp.__enter__ = lambda self_: self_
        resp.__exit__ = lambda self_, *exc: False
        with mock.patch.object(accounts, "urlopen", return_value=resp):
            ok, reset_at, detail = probe._probe(account, "m")
        self.assertTrue(ok)
        self.assertIsNone(reset_at)
        self.assertEqual(detail, "")

    # ------------------------------------------------------------ 调度器联动

    def test_scheduler_wait_shrinks_for_due_window(self):
        import wb_scheduler

        scheduler = wb_scheduler.Scheduler(FakePool(self.dir, []))
        self.assertEqual(scheduler._next_wait(), 30.0)

        now = time.time()
        probe.schedule(self.dir, "uid-a", "m", now + 5, now=now)
        self.assertLessEqual(scheduler._next_wait(), 6.0)

        # 到点后立刻唤醒，但不允许忙等（下限 1 秒）。schedule() 不接受过去的
        # 时刻，所以直接把记录改到过去，模拟「已经到点」。
        with probe._lock:
            probe._state["windows"][probe._key("uid-a", "m")]["reset_at"] = time.time() - 1
        self.assertEqual(scheduler._next_wait(), 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
