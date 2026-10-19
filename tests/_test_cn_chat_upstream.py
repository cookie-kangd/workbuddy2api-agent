"""国内版对话上游可覆盖：WB_CN_CHAT_UPSTREAM。

    python tests/_test_cn_chat_upstream.py

背景：copilot.tencent.com 从海外出口（代理软件全局模式等）连接时 TLS 握手被复位，
国内版的对话、刷新 token、模型目录全部失败；www.workbuddy.cn 提供同一套接口且海外可达。
这里钉住：
  - 没设 / 设成空串时与改动前完全一致（三处都还是 copilot.tencent.com）；
  - 设了合法地址时，对话端点、REALM_CONFIGS、成长任务三处一起换，X-Domain 兜底值不变；
  - 非 http(s) 的值被忽略并回落默认。
环境变量只在导入时读一次，所以每种情形各起一个子进程导入模块。
"""
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROBE = r"""
import json, sys
sys.path.insert(0, %r)
import wb_identity, wb_accounts, wb_tasks
print(json.dumps({
    "desktop": list(wb_identity.endpoint_for("cn", "workbuddy")),
    "cli": list(wb_identity.endpoint_for("cn", "cli")),
    "vscode": list(wb_identity.endpoint_for("cn", "vscode")),
    "intl": list(wb_identity.endpoint_for("intl", "workbuddy")),
    "realm_upstream": wb_accounts.get_realm_config("cn")["chat_upstream"],
    "realm_domain": wb_accounts.get_realm_config("cn")["domain"],
    "tasks": wb_tasks.CHAT_BASE,
    "ignored": wb_identity.CN_CHAT_BASE_IGNORED,
}))
""" % ROOT


def probe(value):
    env = dict(os.environ)
    env.pop("WB_CN_CHAT_UPSTREAM", None)
    if value is not None:
        env["WB_CN_CHAT_UPSTREAM"] = value
    done = subprocess.run([sys.executable, "-c", PROBE], cwd=ROOT, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if done.returncode != 0:
        raise AssertionError(done.stderr.decode("utf-8", "replace"))
    return json.loads(done.stdout.decode("utf-8").strip().splitlines()[-1])


LEGACY = "https://copilot.tencent.com"


class CnChatUpstreamTest(unittest.TestCase):
    def assert_default(self, got):
        self.assertEqual(got["desktop"], [LEGACY, "copilot.tencent.com"])
        self.assertEqual(got["cli"], [LEGACY, "copilot.tencent.com"])
        self.assertEqual(got["realm_upstream"], LEGACY)
        self.assertEqual(got["tasks"], LEGACY)

    def test_unset_keeps_old_behaviour(self):
        got = probe(None)
        self.assert_default(got)
        self.assertEqual(got["ignored"], "")
        self.assert_default(probe("   "))

    def test_override_moves_every_cn_chat_site(self):
        got = probe("https://www.workbuddy.cn/")
        base = "https://www.workbuddy.cn"
        # 只换"往哪发"：X-Domain 兜底值与 REALM_CONFIGS 的 domain 都不动。
        self.assertEqual(got["desktop"], [base, "copilot.tencent.com"])
        self.assertEqual(got["cli"], [base, "copilot.tencent.com"])
        self.assertEqual(got["realm_upstream"], base)
        self.assertEqual(got["realm_domain"], "copilot.tencent.com")
        self.assertEqual(got["tasks"], base)
        # VSCode 身分本来就走 workbuddy.cn，国际版不受影响。
        self.assertEqual(got["vscode"], ["https://www.workbuddy.cn", "www.workbuddy.cn"])
        self.assertEqual(got["intl"], ["https://www.workbuddy.ai", "www.workbuddy.ai"])

    def test_invalid_value_is_ignored(self):
        for raw in ("www.workbuddy.cn", "ftp://www.workbuddy.cn", "https://"):
            got = probe(raw)
            self.assert_default(got)
            self.assertEqual(got["ignored"], raw)


if __name__ == "__main__":
    unittest.main()
