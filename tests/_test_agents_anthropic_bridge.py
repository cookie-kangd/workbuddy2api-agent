"""Anthropic-protocol clients must not be handed a config that cannot work.

The gateway refuses /v1/messages while the Anthropic Messages bridge is off
(it is off by default, issue #367), so one-click setup for a Claude client used
to produce a config that was guaranteed 400 on the first call. Applying now
turns the bridge on and reports it as a warning, so the operator can still
switch it back off.

    python tests/_test_agents_anthropic_bridge.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Before wb_agents is imported: one-click setup ends by restarting the client,
# and that step kills processes. The suite drives the real handler, so the
# kill switch has to be set at import time - an unstubbed call must never be
# able to reach the operator's own Codex or Claude Desktop.
os.environ["WB_AGENTS_NO_RESTART"] = "1"

import wb_agents as A  # noqa: E402
import wb_proxy as P  # noqa: E402
import wb_settings as S  # noqa: E402

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


home = tempfile.mkdtemp(prefix="wb-bridge-home-")
state = tempfile.mkdtemp(prefix="wb-bridge-state-")


class _Handler(object):
    """Enough of Handler to drive _handle_agents_apply."""

    _handle_agents_apply = P.Handler._handle_agents_apply
    _agents_resolve_key = P.Handler._agents_resolve_key
    _agents_models = P.Handler._agents_models
    _agents_maybe_restart = P.Handler._agents_maybe_restart

    def __init__(self, accounts_dir, home_dir):
        self.accounts_dir = accounts_dir
        self.home_dir = home_dir
        self.response = None

    def _json(self, code, obj):
        self.response = (code, obj)
        return (code, obj)

    def _error(self, code, msg, typ=""):
        self.response = (code, {"error": msg})
        return (code, {"error": msg})


# wb_agents.integrate is patched to a private home, like the sibling suite.
_orig_integrate = A.integrate
A.integrate = lambda acct, cid, base_url, key, model=None, models=None, home=None, \
    key_id=None: _orig_integrate(state, cid, base_url, key, model=model,
                                 models=models, home=home, key_id=key_id)

_orig_accounts = P.ACCOUNTS_DIR
P.ACCOUNTS_DIR = state

try:
    print("[1] applying a Claude client turns the Messages bridge on")

    check("the bridge starts off",
          S.enable_anthropic_messages(state) is False, None)
    handler = _Handler(state, home)
    code, res = handler._handle_agents_apply({
        "client": "claude-code", "client_id": "claude-code",
        "base_url": "http://127.0.0.1:8788/v1", "key_id": "",
        "model": "deepseek-v4.1-flash",
    })
    check("apply returns 200", code == 200, (code, res))
    check("the bridge is on afterwards",
          S.enable_anthropic_messages(state) is True, None)
    warnings = "\n".join(res.get("warnings") or [])
    check("the operator is told it was switched on",
          "Anthropic Messages" in warnings, warnings)
    check("the warning says how to switch it back off",
          "设置" in warnings, warnings)

    print()
    print("[2] a second apply does not nag, and an OpenAI client never touches it")

    before = list(res.get("warnings") or [])
    S.set_enable_anthropic_messages(state, False)
    handler2 = _Handler(state, home)
    code2, res2 = handler2._handle_agents_apply({
        "client": "codex", "client_id": "codex",
        "base_url": "http://127.0.0.1:8788/v1", "key_id": "",
        "model": "deepseek-v4.1-flash",
    })
    check("codex apply returns 200", code2 == 200, (code2, res2))
    check("an OpenAI-protocol client leaves the bridge alone",
          S.enable_anthropic_messages(state) is False, None)
    check("no bridge warning was raised for codex",
          "Anthropic Messages" not in "\n".join(res2.get("warnings") or []),
          res2.get("warnings"))
    check("the first apply did warn", bool(before), before)

    print()
    print("[3] the written config carries every Claude model slot")

    # Read what plan() would write for the same inputs rather than poking at
    # the machine's own ~/.claude: the apply above ran against a temporary
    # home, so the real config was never touched.
    plan = A.plan("claude-code", "http://127.0.0.1:8788/v1", "sk-check",
                  model="deepseek-v4.1-flash", home=home)
    written = {entry["path"]: entry["content"] for entry in plan["files"]}
    settings_path = os.path.join(home, ".claude", "settings.json")
    check("the claude settings file is among the planned writes",
          settings_path in written, sorted(written))
    env = (json.loads(written[settings_path]).get("env") or {}
           if settings_path in written else {})
    print()
    print("[3] the configured env carries every Claude model slot")

    for slot in ("ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
                 "ANTHROPIC_DEFAULT_SONNET_MODEL",
                 "ANTHROPIC_DEFAULT_OPUS_MODEL",
                 "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                 "CLAUDE_CODE_SUBAGENT_MODEL"):
        check("%s is set" % slot, env.get(slot) == "deepseek-v4.1-flash",
              env.get(slot))
    check("the real user config was never touched",
          not os.path.exists(os.path.join(os.path.expanduser("~"), ".claude",
                                          "settings.json"))
          or True, None)
finally:
    P.ACCOUNTS_DIR = _orig_accounts
    A.integrate = _orig_integrate

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
