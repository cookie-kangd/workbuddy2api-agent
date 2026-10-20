"""A stream that dies before the first client-visible byte is retried once.

Issue #522: open_upstream returns as soon as the headers arrive, so an
IncompleteRead during the body read never reaches the retry loop. The chat
path used to append [DONE] and return, making a truncated turn look like a
normal finish. This suite drives a real gateway against a mock upstream that
drops the first stream after the headers, then serves a complete one.

Run with: python tests/_test_stream_retry.py
No network, no credentials: only loopback sockets on ephemeral ports.
"""
import base64
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import _lifecycle as life  # noqa: E402
from _isolated_dirs import isolated_data_dirs  # noqa: E402

_TMP = isolated_data_dirs("wb-stream-retry-")
ACCOUNTS = os.environ["ACCOUNTS_DIR"]
USAGE_DIR = os.environ["WB_PROXY_USAGE_DIR"]
os.environ["HOME"] = _TMP.name
os.environ["USERPROFILE"] = _TMP.name
os.environ["WB_PROXY_KEY"] = "TESTKEY"

MODEL = "deepseek-v4.1-flash"


def jwt(uid="acct-stream-retry-1"):
    def seg(obj):
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return seg({"alg": "none", "typ": "JWT"}) + "." + seg(
        {"uid": uid, "exp": int(time.time()) + 86400 * 30}) + ".x"


with open(os.path.join(ACCOUNTS, "acct1.json"), "w", encoding="utf-8") as fh:
    json.dump({
        "uid": "acct-stream-retry-1",
        "accessToken": jwt(),
        "refreshToken": "",
        "expiresAt": int(time.time()) + 86400 * 30,
        "realm": "intl",
        "domain": "www.workbuddy.ai",
        "platform": "CLI",
        "product": "workbuddy",
        "enabled": True,
    }, fh)

# Egress accounting is what makes the retry visible in the backoff state.
with open(os.path.join(ACCOUNTS, "settings.json"), "w", encoding="utf-8") as fh:
    json.dump({"egress_backoff_enabled": True}, fh)

UPSTREAM_PORT = life.free_port()
MOCK = {"stream_calls": 0, "requests": []}


def sse(chunks):
    out = []
    for c in chunks:
        out.append(("data: " + json.dumps(c) + "\n\n").encode("utf-8"))
    out.append(b"data: [DONE]\n\n")
    return b"".join(out)


class MockUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {}
        MOCK["requests"].append({
            "path": self.path,
            "body": body,
            "auth": self.headers.get("Authorization") or "",
        })
        if self.path.endswith("/chat/completions"):
            MOCK["stream_calls"] += 1
            if MOCK["stream_calls"] == 1:
                # Headers say 4 KiB; the body never arrives. http.client's
                # read1() returns b"" here, so iter_sse_lines turns the
                # remaining Content-Length into IncompleteRead.
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", "4096")
                self.end_headers()
                self.wfile.flush()
                self.close_connection = True
                return
            payload = sse([
                {"id": "up-2", "model": MODEL,
                 "choices": [{"index": 0, "delta": {"content": "Hello after retry"}}]},
                {"id": "up-2", "model": MODEL,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ])
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        payload = b'{"data":{}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        payload = b'{"data":{}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


upstream = ThreadingHTTPServer(("127.0.0.1", UPSTREAM_PORT), MockUpstream)
threading.Thread(target=upstream.serve_forever, daemon=True).start()

import wb_egress  # noqa: E402
import wb_identity  # noqa: E402
import wb_proxy as P  # noqa: E402
import wb_upstream_pool  # noqa: E402

_ENDPOINTS = dict(wb_identity._ENDPOINTS)
for key in list(_ENDPOINTS):
    _ENDPOINTS[key] = ("http://127.0.0.1:%d" % UPSTREAM_PORT, _ENDPOINTS[key][1])
wb_identity._ENDPOINTS.update(_ENDPOINTS)

GATEWAY_PORT = life.free_port()


class Args(object):
    host = "127.0.0.1"
    port = GATEWAY_PORT
    api_key = "TESTKEY"
    system_prompt = "You are a helpful assistant."
    user_agent = None
    usage_dir = USAGE_DIR
    accounts_dir = ACCOUNTS
    import_desktop = False
    lan = False
    panel_password = None
    info = None


P._apply_cli_overrides(Args())
P._bootstrap_runtime(Args())
gateway = ThreadingHTTPServer(("127.0.0.1", GATEWAY_PORT), P.Handler)
threading.Thread(target=gateway.serve_forever, daemon=True).start()

PASS = FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (label, detail))


def request(path, body, timeout=30):
    url = "http://127.0.0.1:%d%s" % (GATEWAY_PORT, path)
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer TESTKEY")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def wait_ready():
    for _ in range(60):
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/health" % GATEWAY_PORT, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def parse_frames(raw):
    frames = []
    for block in raw.decode("utf-8", "replace").split("\n\n"):
        for line in block.splitlines():
            if not line.startswith("data: "):
                continue
            data = line[6:].strip()
            if data == "[DONE]":
                frames.append("[DONE]")
            elif data:
                try:
                    frames.append(json.loads(data))
                except Exception:
                    frames.append({"__raw__": data})
    return frames


try:
    if not wait_ready():
        print("  [FAIL] gateway did not start")
        sys.exit(1)

    print("[1] the first stream dies before any byte reaches the client")
    code, body = request("/v1/chat/completions", {
        "model": MODEL, "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    })
    frames = parse_frames(body)
    text = "".join(
        (ch.get("delta") or {}).get("content") or ""
        for f in frames if isinstance(f, dict)
        for ch in (f.get("choices") or []))
    check("200 with a parsed SSE stream", code == 200 and frames, (code, body[:200]))
    check("the retry produced the complete answer", "Hello after retry" in text, text)
    check("[DONE] is still the last frame", frames and frames[-1] == "[DONE]",
          frames[-1] if frames else None)
    check("upstream saw exactly two stream attempts",
          MOCK["stream_calls"] == 2, MOCK["stream_calls"])

    stream_reqs = [r for r in MOCK["requests"]
                   if r["path"].endswith("/chat/completions")]
    check("the retry resent the same request body",
          len(stream_reqs) == 2 and stream_reqs[0]["body"] == stream_reqs[1]["body"],
          [r["body"] for r in stream_reqs])
    check("the retry stayed on the same account",
          len({r["auth"] for r in stream_reqs}) == 1,
          [r["auth"] for r in stream_reqs])

    print()
    print("[2] the aborted read leaves an egress streak behind")
    account = P.POOL.accounts[0]
    egress = wb_upstream_pool.egress_key(
        account.chat_base_url() + P.CHAT_PATH, account.proxy)
    check("streak is one after the retried request",
          wb_egress.streak(egress) == 1, wb_egress.streak(egress))

    print()
    print("[3] a clean stream clears the streak")
    code2, body2 = request("/v1/chat/completions", {
        "model": MODEL, "stream": True,
        "messages": [{"role": "user", "content": "again"}],
    })
    frames2 = parse_frames(body2)
    check("the clean request completes", code2 == 200 and frames2
          and frames2[-1] == "[DONE]", (code2, body2[:200]))
    check("upstream saw a third stream attempt",
          MOCK["stream_calls"] == 3, MOCK["stream_calls"])
    check("streak is cleared by the clean read",
          wb_egress.streak(egress) == 0, wb_egress.streak(egress))

    print()
    print("[4] usage is recorded once, as completed")
    log = os.path.join(USAGE_DIR, "usage.jsonl")
    rows = []
    for _ in range(40):
        if os.path.exists(log):
            with open(log, encoding="utf-8") as fh:
                rows = [json.loads(line) for line in fh if line.strip()]
            if len(rows) >= 2:
                break
        time.sleep(0.25)
    check("two usage rows for two client requests", len(rows) == 2, len(rows))
    check("no row is marked upstream_aborted",
          all(r.get("outcome") != "upstream_aborted" for r in rows),
          [r.get("outcome") for r in rows])
    check("both rows are completed",
          all(r.get("outcome") == "completed" for r in rows),
          [r.get("outcome") for r in rows])
finally:
    gateway.shutdown()
    upstream.shutdown()

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
