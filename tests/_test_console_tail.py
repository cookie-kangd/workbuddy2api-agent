"""console_tail.py 的登录参数（#427）。

面板按钮改成浏览器新窗口后，这个命令行查看器仍由启动脚本使用。它以前把
面板口令硬编码成 admin，用户改过密码后窗口只会反复登录失败；现在必须能
从 --password 或 WB_PANEL_PASSWORD 取到真实口令。

Run with: python tests/_test_console_tail.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import console_tail


class ParseArgsTests(unittest.TestCase):
    def test_password_and_token_are_accepted(self):
        console_tail.parse_args([
            "console_tail.py", "--port", "9000", "--host", "127.0.0.1",
            "--password", "s3cret", "--token", "panel-token",
        ])
        self.assertEqual(console_tail.PORT, 9000)
        self.assertEqual(console_tail.PASSWORD, "s3cret")
        self.assertEqual(console_tail.TOKEN, "panel-token")

    def test_unknown_arguments_do_not_raise(self):
        console_tail.parse_args(["console_tail.py", "--nope", "x"])


if __name__ == "__main__":
    unittest.main()
