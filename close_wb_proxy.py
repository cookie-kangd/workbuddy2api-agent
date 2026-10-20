#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy proxy - stop the service without opening the dashboard.

Double-clicking close-wb-proxy.bat used to be impossible: since the silent
launch the service is a pythonw process with no console window, so closing
any window does nothing to it. The only way to stop it was the dashboard
power button (POST /shutdown). This script does exactly what that button
does, from the command line:

    1. log in to the panel (same password the browser asks for),
    2. POST /shutdown,
    3. poll /health until the port stops answering.

The reason for the health poll is that a container or a service manager
may restart the process right away; the button reports that honestly and
so does this script. Nothing here kills a process by PID - the service is
asked to exit, which is what lets it flush its last usage row.

Usage:
    python close_wb_proxy.py [--port 8788] [--password admin] [--yes]

Password resolution, in order: --password, the WB_PANEL_PASSWORD
environment variable, then the default "admin" (the same order
console_tail.py uses). The port defaults to 8788, or $PORT if set.

Only the Python standard library is used, so the bundled runtime in the
portable package runs this without installing anything.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_PORT = 8788
DEFAULT_PASSWORD = "admin"
# How long to keep polling /health after the shutdown request. The panel
# button polls 10 times at 600ms; this script uses the same shape but a
# slightly longer window, because a cold start is not the case here - the
# service is already running and answers fast.
POLL_ATTEMPTS = 15
POLL_INTERVAL = 0.6
# 收尾倒计时：双击 .bat 时给用户一眼看清结果的时间，然后窗口自己关掉。
# 由 --countdown 覆盖（0 = 立即退出，留给已经在脚本里调用的人）。
DEFAULT_COUNTDOWN = 3

# 本地回环请求绝不走代理：环境里若有全局 HTTP(S)_PROXY，请求会被代理转发，
# 服务退出时看到的就是代理层的 502 报错，而不是干净的「服务已关闭」。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post(url, payload, headers=None, timeout=10):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    with _OPENER.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(url, timeout=5):
    with _OPENER.open(url, timeout=timeout) as resp:
        return resp.status, resp.read()


def countdown(seconds):
    """Print the result, then close the window after `seconds`.

    Double-clicking close-wb-proxy.bat must not need a keypress: the console
    stays long enough to read the verdict and disappears on its own. The
    countdown runs even when stdin is closed (a .bat launch), so it never
    waits for input that will not come.
    """
    if seconds <= 0:
        return
    try:
        for left in range(int(seconds), 0, -1):
            # \r 让倒计时停在原地，不刷一屏；flush 否则管道里看不到。
            print("\r[close] 窗口将在 %d 秒后自动关闭...   " % left,
                  end="", flush=True)
            time.sleep(1)
        print("\r[close] 再见。                                  ")
    except (KeyboardInterrupt, OSError):
        # Ctrl+C 或窗口被提前关掉：静默退出，不报堆栈。
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Stop a running WorkBuddy proxy the way the dashboard "
                    "power button does.")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("PORT") or DEFAULT_PORT),
                        help="port the service listens on (default 8788)")
    parser.add_argument("--password", default=os.environ.get(
        "WB_PANEL_PASSWORD", "") or DEFAULT_PASSWORD,
        help="panel password; also read from WB_PANEL_PASSWORD")
    parser.add_argument("--countdown", type=int, default=DEFAULT_COUNTDOWN,
                        help="seconds to show the result before the window "
                             "closes itself (default %(default)s, 0 = now)")
    args = parser.parse_args(argv)

    base = "http://127.0.0.1:%d" % args.port

    # Is anything running at all? Answer before asking for a password, so a
    # second run of the script does not prompt for credentials it cannot use.
    try:
        _get(base + "/health")
    except urllib.error.HTTPError as exc:
        # 401/403 means the service is up but the route is gated; either way
        # it is running, which is all this check cares about.
        if exc.code not in (401, 403):
            print("[close] port %d answered %d; expected the proxy."
                  % (args.port, exc.code))
            return 1
    except Exception:
        print("[close] no service is listening on port %d - nothing to stop."
              % args.port)
        countdown(args.countdown)
        return 0

    # Panel session, exactly like the browser login form.
    try:
        session = _post(base + "/panel/login", {"password": args.password})
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 429):
            print("[close] panel rejected the password (%d)." % exc.code)
        else:
            print("[close] login failed: %d %s" % (exc.code, exc.reason))
        print("        pass the right one with --password <pw> or set "
              "WB_PANEL_PASSWORD.")
        countdown(args.countdown)
        return 1
    except Exception as exc:
        print("[close] could not reach the panel: %s" % exc)
        countdown(args.countdown)
        return 1

    token = session.get("token") or ""
    if not token:
        print("[close] the panel did not hand back a session token.")
        countdown(args.countdown)
        return 1

    # The power button. The service answers first and exits about 0.4s later,
    # so the request itself succeeds even though the process is on its way out.
    try:
        reply = _post(base + "/shutdown", {},
                      headers={"X-Panel-Token": token}, timeout=10)
    except Exception as exc:
        # 进程退出会掐断连接（ConnectionResetError / BadStatusLine），这不是
        # 失败：真正停没停由下面的 /health 判定。
        print("[close] shutdown sent (connection closed early: %s)." % exc)
        countdown(args.countdown)
    else:
        print("[close] shutdown accepted: %s"
              % ("ok" if reply.get("ok") else "see the dashboard"))

    # Same honesty the button has: only claim success once the port is quiet.
    for attempt in range(POLL_ATTEMPTS):
        time.sleep(POLL_INTERVAL)
        try:
            _get(base + "/health")
        except Exception:
            print("[close] stopped. Port %d is free." % args.port)
            print("        start it again with start-wb-proxy.bat.")
            countdown(args.countdown)
            return 0
    print("[close] the shutdown request was sent but the service is still "
          "answering on port %d." % args.port)
    print("        it may be supervised by a container or service manager; "
          "stop it there.")
    countdown(args.countdown)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
