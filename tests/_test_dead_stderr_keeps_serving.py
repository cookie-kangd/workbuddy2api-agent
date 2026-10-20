"""启动它的终端被回收后，被记日志的端点必须还能答（stderr 裸写事故回归）。

    python tests/_test_dead_stderr_keeps_serving.py

`send_response()` 会先走 log_request -> log_message -> log()。log() 曾经裸写
sys.stderr：一旦 stderr 的读端消失（启动网关的那个终端/管道被回收），写就抛
OSError，而位置在**响应头发出之前**——客户端只看到连接被空关，一条访问日志也不留。
最难发现的是 `log_message` 静默白名单里的端点照常被答，于是进程活着、端口在听、
`/health` 绿，业务接口 100% 不可用（2026-10-19 线上事故实测指纹）。

本套件把子进程 stderr 管道的读端关掉来复现那一种状态，再断言：
免日志的三个端点与被记日志的三个端点**都必须拿到 HTTP 状态行**，且进程仍在。
判据是"有没有响应"，不是"响应对不对"——无凭据的网关对 /v1/models 返回 502 也算通过。

用一个临时 accounts/usage 与备用端口起真服务，不碰网络、不需要凭据、不动真实账号。
"""

import os
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # the gateway lives one level up
PY = os.path.join(ROOT, "python", "python.exe")
if not os.path.exists(PY):
    PY = sys.executable
sys.path.insert(0, HERE)

import _lifecycle as life            # noqa: E402  (spare port + managed process)

PASS = 0
FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  ok   %s" % label)
    else:
        FAIL += 1
        print("  FAIL %s%s" % (label, (" -> " + detail) if detail else ""))


def answered(port, path, timeout=15):
    """(True, status line) when the peer answers at all; (False, why) otherwise."""
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    except Exception as exc:
        return False, "connect failed: %s" % exc
    try:
        sock.sendall(("GET %s HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n" % path)
                     .encode("ascii"))
        data = sock.recv(512)
    except Exception as exc:
        return False, "recv: %s: %s" % (type(exc).__name__, exc)
    finally:
        try:
            sock.close()
        except Exception:
            pass
    if not data:
        return False, "empty reply（连接被空关，正是要抓的形态）"
    head = data.split(b"\r\n", 1)[0].decode("latin-1")
    if not head.startswith("HTTP/"):
        return False, "不是状态行: %r" % head
    return True, head


work = tempfile.mkdtemp(prefix="wb-dead-stderr-")
store = os.path.join(work, "accounts")
usage = os.path.join(work, "usage")
os.makedirs(store, exist_ok=True)
os.makedirs(usage, exist_ok=True)
port = life.free_port()

# stderr 必须是管道：后面要在本进程里关掉它的读端，才复现得出发端 OSError
proc = life.spawn_managed(
    [PY, os.path.join(ROOT, "wb_proxy.py"), "--port", str(port), "--host", "127.0.0.1",
     "--accounts-dir", store],
    cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    env=dict(os.environ, WB_PROXY_USAGE_DIR=usage))


def wait_ready():
    for _ in range(60):
        time.sleep(0.5)
        ok, detail = answered(port, "/health", timeout=2)
        if ok and " 200" in detail:
            return True
        if proc.poll() is not None:
            return False
    return False


try:
    check("备用端口 %d 上的网关已就绪" % port, wait_ready())
    if proc.poll() is None:
        # 关键一步：父进程不再读这个管道
        try:
            proc.stderr.close()
        except Exception:
            pass
        print("  ..   已关闭子进程 stderr 管道的读端（模拟启动终端被回收）")

        for path in ("/health", "/realm", "/panel/status"):
            ok, detail = answered(port, path)
            check("免日志端点 %s 仍有响应" % path, ok, detail)
        for path in ("/v1/models", "/", "/favicon.ico"):
            ok, detail = answered(port, path)
            check("被记日志的端点 %s 仍有响应" % path, ok, detail)
        check("进程没有因为日志写失败而退出", proc.poll() is None,
              "exit=%s" % proc.poll())
finally:
    life.stop_managed(proc)
    import shutil
    shutil.rmtree(work, ignore_errors=True)

print()
print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
