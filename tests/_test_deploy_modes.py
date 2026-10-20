"""quick-deploy.sh 的部署模式（#512）。

脚本本身需要 Docker 才能跑，这里做源码级断言：模式解析、端口绑定、公网提示
与文档一致性。任何一条失配都意味着「一键部署到公网」会静默回到监听所有网卡。

    python tests/_test_deploy_modes.py
"""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


class DeployModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = read("quick-deploy.sh")
        cls.readme = read("README.md")
        cls.compose = read("docker-compose.yml")

    def test_default_stays_lan(self):
        self.assertIn('DEPLOY_MODE="${WB_DEPLOY_MODE:-lan}"', self.script)

    def test_only_three_modes_are_accepted(self):
        self.assertIn("local|lan|public) ;;", self.script)
        self.assertIn("WB_DEPLOY_MODE 只能是 local / lan / public", self.script)

    def test_loopback_modes_bind_the_host_port_to_localhost(self):
        self.assertIn('PORT_BINDING="127.0.0.1:8788:8788"', self.script)
        self.assertIn('PORT_BINDING="8788:8788"', self.script)
        # 容器内仍要监听 0.0.0.0，否则 Docker 的端口转发连不进来。本仓库自带
        # docker-compose.yml（脚本克隆源码后用它本地构建，不生成 compose），
        # 所以这条断言落在 compose 上而不是脚本里。
        self.assertIn("- HOST=0.0.0.0", self.compose)

    def test_the_binding_reaches_the_compose_that_is_actually_used(self):
        """非 lan 模式必须把绑定写进真正被 docker compose 读到的那个文件。

        本仓库自带 docker-compose.yml（发布只出 Windows 便携包，镜像一律本地
        构建），所以脚本不会生成 compose，而是在克隆后按模式改写它的 ports。
        只改这一行，其余配置与源码自带的保持一致。
        """
        self.assertIn('"8788:8788"', self.compose)
        self.assertIn('PORT_BINDING', self.script)
        self.assertIn("docker-compose.yml", self.script)
        # 改写的目标与上游一致：把 8788 从「全部网卡」换成按模式算出的绑定。
        self.assertIn('8788:8788', self.script)
        self.assertIn("已按 ${DEPLOY_MODE} 模式把端口绑定改为", self.script)

    def test_upgrade_does_not_rewrite_an_existing_compose(self):
        self.assertIn("已有部署不会自动改写端口映射", self.script)

    def test_public_mode_explains_the_reverse_proxy_requirement(self):
        self.assertIn("请用 Nginx / Caddy / Cloudflare Tunnel 反代", self.script)
        self.assertIn("WB_DEPLOY_MODE=public bash", self.readme)


if __name__ == "__main__":
    unittest.main()
