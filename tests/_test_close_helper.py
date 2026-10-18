"""close_wb_proxy.py: command-line close service, same road as the panel button."""

import importlib.util, io, json, os, socket, sys, threading, unittest, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_spec = importlib.util.spec_from_file_location(
    "close_wb_proxy", os.path.join(ROOT, "close_wb_proxy.py"))
cwp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cwp)


class FakeGateway(BaseHTTPRequestHandler):
    password = "admin"
    calls = []
    alive = True

    def log_message(self, fmt, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            if not type(self).alive:
                self.close_connection = True
                return
            return self._json(200, {"ok": True})
        self._json(404, {"error": "no such route"})

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if path == "/panel/login":
            if payload.get("password") != type(self).password:
                return self._json(401, {"error": "bad password"})
            type(self).calls.append(("login", payload))
            return self._json(200, {"token": "sess-token"})
        if path == "/shutdown":
            token = self.headers.get("X-Panel-Token") or ""
            type(self).calls.append(("shutdown", {"token": token}))
            # The real service exits about 0.4s after answering, so the next
            # /health finds nothing. Flip the flag so the poll in the script
            # sees the same thing it sees in production.
            type(self).alive = False
            return self._json(200, {"ok": True, "shutting_down": True})
        self._json(404, {"error": "no such route"})


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, srv.server_address[1]


def run_close(argv):
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        code = cwp.main(argv)
    finally:
        sys.stdout = old
    return code, buf.getvalue()


class ScriptTests(unittest.TestCase):
    def setUp(self):
        FakeGateway.calls = []
        FakeGateway.password = "admin"
        FakeGateway.alive = True

    def test_it_uses_the_panel_endpoints_not_a_pid_kill(self):
        srv, port = serve(FakeGateway)
        try:
            code, out = run_close(["--port", str(port), "--yes"])
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(code, 0, out)
        kinds = [k for k, _ in FakeGateway.calls]
        self.assertEqual(kinds[:2], ["login", "shutdown"], FakeGateway.calls)
        token = dict(FakeGateway.calls)["shutdown"]["token"]
        self.assertEqual(token, "sess-token")
        self.assertIn("stopped", out)

    def test_password_order(self):
        srv, port = serve(FakeGateway)
        try:
            FakeGateway.password = "s3cr3t"
            code, out = run_close(["--port", str(port), "--yes"])
            self.assertEqual(code, 1, out)
            self.assertNotIn("shutdown", [k for k, _ in FakeGateway.calls])
            FakeGateway.calls = []
            os.environ["WB_PANEL_PASSWORD"] = "s3cr3t"
            try:
                code, out = run_close(["--port", str(port), "--yes"])
            finally:
                os.environ.pop("WB_PANEL_PASSWORD", None)
            self.assertEqual(code, 0, out)
            FakeGateway.calls = []
            code, out = run_close(["--port", str(port), "--yes",
                                   "--password", "s3cr3t"])
            self.assertEqual(code, 0, out)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_nothing_running_is_not_an_error(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
        s.close()
        code, out = run_close(["--port", str(free_port), "--yes"])
        self.assertEqual(code, 0, out)
        self.assertIn("nothing to stop", out)
        self.assertEqual(FakeGateway.calls, [])

    def test_wrong_password_leaves_the_service_alone(self):
        srv, port = serve(FakeGateway)
        try:
            code, out = run_close(["--port", str(port), "--yes",
                                   "--password", "nope"])
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(code, 1, out)
        self.assertEqual(FakeGateway.calls, [])

    def test_a_supervised_restart_is_reported_not_hidden(self):
        class StaysUp(FakeGateway):
            def do_GET(self):
                return self._json(200, {"ok": True})

        srv, port = serve(StaysUp)
        cwp.POLL_ATTEMPTS, cwp.POLL_INTERVAL = 3, 0.05
        try:
            code, out = run_close(["--port", str(port), "--yes"])
        finally:
            cwp.POLL_ATTEMPTS, cwp.POLL_INTERVAL = 15, 0.6
            srv.shutdown()
            srv.server_close()
        self.assertEqual(code, 1, out)
        self.assertIn("still answering", out)

    def test_local_loopback_never_uses_a_proxy(self):
        """A machine-wide HTTP(S)_PROXY must not swallow the loopback call.

        build_opener(ProxyHandler({})) is the documented "no proxy for any
        URL" form; without it a proxy in the environment would answer for the
        service and the script would report the proxy's 502 as a failure.
        urllib drops the handler when its proxy table is empty, so the
        contract is pinned on the source line rather than on handler
        introspection.
        """
        with io.open(os.path.join(ROOT, "close_wb_proxy.py"),
                     encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("ProxyHandler({})", src,
                      "the opener must be built with an empty ProxyHandler")
        self.assertIn("_OPENER", src)
        # 每个请求都必须走这个 opener，不能有裸 urllib.request.urlopen。
        self.assertNotIn("urllib.request.urlopen(", src.replace(
            "_OPENER.open(", ""))

    def test_packaging_contract(self):
        sys.path.insert(0, os.path.join(ROOT, "release"))
        import release_tools as tools
        listed = tools.read_manifest()
        self.assertIn("close-wb-proxy.bat", listed)
        self.assertIn("close_wb_proxy.py", listed)

    def test_the_bat_launcher_is_ascii_and_crlf(self):
        with io.open(os.path.join(ROOT, "close-wb-proxy.bat"), "rb") as fh:
            raw = fh.read()
        raw.decode("ascii")
        self.assertEqual(raw.count(b"\n") - raw.count(b"\r\n"), 0,
                         "bat must use CRLF line endings")


if __name__ == "__main__":
    unittest.main(verbosity=2)
