"""The include_usage frame on /v1/chat/completions must be a trailing one.

OpenAI's contract for `stream_options.include_usage` is a final chunk with an
empty `choices` list carrying the usage block, sent after the finish chunk and
before `[DONE]`. Some upstreams instead put usage on a chunk that still carries
choices, which strict clients reject and downstream gateways cannot bill: the
gateway that fronts this proxy dropped every streaming usage row (no prompt,
completion or cache-hit counts) while the aggregated body still showed them.

The suite runs a real gateway process-in-thread against a mock upstream that
reproduces that in-band ordering, then asserts the client-facing stream:

  - exactly one frame carries usage, it has `choices: []`, and it sits after
    the finish frame and before `[DONE]`;
  - cached tokens survive the hoist (`prompt_tokens_details.cached_tokens`);
  - the usage row written to usage.jsonl keeps the same counts.

Run with: python tests/_test_chat_stream_usage_frame.py
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

import _lifecycle as life  # noqa: E402  (shared spare-port pick)
from _isolated_dirs import isolated_data_dirs  # noqa: E402  (own data root)

_TMP = isolated_data_dirs("wb-chat-stream-usage-")
ACCOUNTS = os.environ["ACCOUNTS_DIR"]
USAGE_DIR = os.environ["WB_PROXY_USAGE_DIR"]
os.environ["HOME"] = _TMP.name
os.environ["USERPROFILE"] = _TMP.name
os.environ["WB_PROXY_KEY"] = "TESTKEY"

MODEL = "deepseek-v4.1-flash"


def jwt(uid="acct-stream-usage-1"):
    def seg(obj):
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return seg({"alg": "none", "typ": "JWT"}) + "." + seg(
        {"uid": uid, "exp": int(time.time()) + 86400 * 30}) + ".x"


with open(os.path.join(ACCOUNTS, "acct1.json"), "w", encoding="utf-8") as fh:
    json.dump({
        "uid": "acct-stream-usage-1",
        "accessToken": jwt(),
        "refreshToken": "",
        "expiresAt": int(time.time()) + 86400 * 30,
        "realm": "intl",
        "domain": "www.workbuddy.ai",
        "platform": "CLI",
        "product": "workbuddy",
        "enabled": True,
    }, fh)

# The in-band ordering that real upstreams emit: the usage block rides on a
# chunk that still carries choices, and finish_reason arrives on its own later
# chunk without usage.
IN_BAND_CHUNKS = [
    {"id": "up-1", "model": MODEL,
     "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]},
    {"id": "up-1", "model": MODEL,
     "choices": [{"index": 0, "delta": {"content": "Hello"}}]},
    {"id": "up-1", "model": MODEL,
     "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}],
     "usage": {"prompt_tokens": 37, "completion_tokens": 16, "total_tokens": 53,
               "prompt_tokens_details": {"cached_tokens": 5},
               "cached_tokens": 5}},
    {"id": "up-1", "model": MODEL,
     "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
]

UPSTREAM_PORT = life.free_port()
MOCK = {"chunks": IN_BAND_CHUNKS, "requests": []}


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
        MOCK["requests"].append({"path": self.path, "body": body})
        if self.path.endswith("/chat/completions"):
            payload = sse(MOCK["chunks"])
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

import wb_identity  # noqa: E402
import wb_proxy as P  # noqa: E402

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


def request(method, path, body=None, headers=None, timeout=30):
    url = "http://127.0.0.1:%d%s" % (GATEWAY_PORT, path)
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def wait_ready():
    for _ in range(60):
        try:
            code, _h, _b = request("GET", "/health", timeout=2)
            if code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def parse_frames(raw):
    """Every data frame of a chat SSE stream, [DONE] included as the sentinel."""
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

    print("[1] streaming chat: usage arrives in one trailing choices-less frame")
    code, _headers, body = request(
        "POST", "/v1/chat/completions",
        {"model": MODEL, "stream": True,
         "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer TESTKEY"})
    frames = parse_frames(body)
    check("200 with a parsed SSE stream", code == 200 and frames, (code, body[:200]))
    check("upstream received the request", bool(MOCK["requests"]),
          MOCK["requests"])

    done_at = len(frames) - 1
    check("[DONE] is the last frame", frames and frames[-1] == "[DONE]",
          frames[-1] if frames else None)

    usage_frames = [i for i, f in enumerate(frames)
                    if isinstance(f, dict) and isinstance(f.get("usage"), dict)]
    check("exactly one frame carries usage", len(usage_frames) == 1,
          [(i, frames[i].get("usage")) for i in usage_frames])
    usage_at = usage_frames[0] if usage_frames else -1
    usage_frame = frames[usage_at] if usage_frames else {}

    check("the usage frame has no choices", usage_frame.get("choices") == [],
          usage_frame.get("choices"))
    check("the usage frame is a chat.completion.chunk",
          usage_frame.get("object") == "chat.completion.chunk", usage_frame.get("object"))
    check("it is the last frame before [DONE]", usage_at == done_at - 1,
          "usage_at=%d done_at=%d" % (usage_at, done_at))

    finish_at = [i for i, f in enumerate(frames)
                 if isinstance(f, dict)
                 and any((c or {}).get("finish_reason") for c in (f.get("choices") or []))]
    check("exactly one finish frame, before the usage frame",
          len(finish_at) == 1 and usage_at > finish_at[0], (finish_at, usage_at))
    check("the usage frame reuses the stream's unique id",
          usage_frame.get("id") == frames[finish_at[0]].get("id")
          and usage_frame.get("id") != "chatcmpl-wb",
          usage_frame.get("id"))

    usage = usage_frame.get("usage") or {}
    check("prompt tokens survive the hoist", usage.get("prompt_tokens") == 37, usage)
    check("completion tokens survive the hoist",
          usage.get("completion_tokens") == 16, usage)
    check("cached tokens survive the hoist",
          (usage.get("prompt_tokens_details") or {}).get("cached_tokens") == 5, usage)

    check("no choice-bearing frame carries usage",
          all(not (isinstance(f, dict) and f.get("choices") and isinstance(f.get("usage"), dict))
              for f in frames), frames)

    print()
    print("[2] the usage row keeps the same counts")
    log = os.path.join(USAGE_DIR, "usage.jsonl")
    rows = []
    for _ in range(40):
        if os.path.exists(log):
            with open(log, encoding="utf-8") as fh:
                rows = [json.loads(line) for line in fh if line.strip()]
            if rows:
                break
        time.sleep(0.25)
    check("a usage row was written", bool(rows), rows)
    row = rows[-1] if rows else {}
    check("row reports the streamed prompt tokens", row.get("prompt_tokens") == 37, row)
    check("row reports the streamed cached tokens", row.get("cached_tokens") == 5, row)
    check("row is not flagged usage_missing", not row.get("usage_missing"), row)
finally:
    gateway.shutdown()
    upstream.shutdown()

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
