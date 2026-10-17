"""Third-party upstream selection, isolation and client synchronization.

The third-party card selects exactly one local client. The WorkBuddy
connection-info card must remain WorkBuddy-only, while the selected client's
card must show the third-party upstream and the optional default model.
Returning through the WorkBuddy card disables the third-party exit.
"""
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import wb_agents as agents  # noqa: E402
import wb_proxy as proxy  # noqa: E402
import wb_upstream_thirdparty as tp  # noqa: E402

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


home = tempfile.mkdtemp(prefix="wb-upstream-agents-home-")
accounts = tempfile.mkdtemp(prefix="wb-upstream-agents-acct-")
orig_accounts = proxy.ACCOUNTS_DIR
orig_api_key = proxy.API_KEY
orig_resolve_home = agents._resolve_home
try:
    proxy.ACCOUNTS_DIR = accounts
    proxy.API_KEY = "sk-gateway"
    agents._resolve_home = lambda injected=None: (
        os.path.abspath(injected) if injected else home)

    agents.integrate(accounts, "codex", "http://127.0.0.1:8788/v1",
                     "sk-gateway", model="deepseek-v4.1-flash",
                     models=[{"id": "deepseek-v4.1-flash"}], home=home)
    open_dir = os.path.join(home, ".config", "opencode")
    os.makedirs(open_dir, exist_ok=True)
    with open(os.path.join(open_dir, "opencode.json"), "w", encoding="utf-8") as fh:
        json.dump({"$schema": "https://opencode.ai/config.json"}, fh)

    with open(tp.config_path(accounts), "w", encoding="utf-8") as fh:
        json.dump({
            "enabled": True,
            "base_url": "https://developer.amd.com.cn/radeon/api/v1",
            "api_key": "sk-amd-test",
            "client_id": "codex",
            "default_model": "AMD-Second",
            "models": ["DeepSeek-V4.1-Flash", "AMD-Second"],
            "models_at": time.time(),
        }, fh)
    tp._cache["at"] = 0
    tp._cache["models"] = None

    h = proxy.Handler.__new__(proxy.Handler)
    h._panel_ok = lambda: True
    h._agents_client_allowed = lambda: True
    h._agents_base_url_hint = lambda: "http://127.0.0.1:8788/v1"
    h._agents_maybe_restart = lambda wb, cid, wanted: {"requested": False}
    h._anthropic_messages_enabled = lambda: True
    h._json = lambda code, obj: (code, obj)
    h._error = lambda code, msg, typ="": (code, {"error": msg})

    print("[1] WorkBuddy connection info and third-party models stay separate")
    workbuddy_ids = [m["id"] for m in h._agents_models_for("intl")]
    thirdparty_ids = [m["id"] for m in h._agents_models_thirdparty()]
    check("the WorkBuddy picker does not contain third-party-only models",
          "AMD-Second" not in workbuddy_ids, workbuddy_ids[:8])
    check("the third-party picker contains the selected endpoint's models",
          thirdparty_ids == ["DeepSeek-V4.1-Flash", "AMD-Second"],
          thirdparty_ids)

    print()
    print("[2] only the selected configured client is re-applied")
    sync = h._agents_sync_client_configs(
        restart=False, client_ids=["codex"], default_model="AMD-Second",
        models=h._agents_models_thirdparty())
    state = agents.integration_state(accounts)
    codex_state = state.get("codex") or {}
    codex_catalog = json.load(open(
        os.path.join(home, ".codex", "wb-proxy-model-catalog.json"), encoding="utf-8"))
    slugs = [m.get("slug") for m in codex_catalog.get("models", [])]
    check("the selected client is reported",
          [r.get("client") for r in sync.get("results", []) if r.get("client")]
          == ["codex"], sync)
    check("the selected client uses the selected default model",
          codex_state.get("model") == "AMD-Second", codex_state)
    check("the selected client catalog contains only the third-party list",
          slugs == ["DeepSeek-V4.1-Flash", "AMD-Second"], slugs)
    check("the unselected unconfigured client was not touched",
          "opencode" not in state, sorted(state))

    print()
    print("[3] a selected installed client is one-click configured")
    sync2 = h._agents_sync_client_configs(
        restart=False, client_ids=["opencode"], default_model=None,
        models=h._agents_models_thirdparty())
    state = agents.integration_state(accounts)
    open_state = state.get("opencode") or {}
    check("the newly selected client is now recorded",
          "opencode" in state, sorted(state))
    check("the first third-party model is used when no default is chosen",
          open_state.get("model") == "DeepSeek-V4.1-Flash", open_state)
    check("the selected-client sync reports the new client",
          [r.get("client") for r in sync2.get("results", []) if r.get("client")]
          == ["opencode"], sync2)

    print()
    print("[4] the agent card exposes the third-party source")
    code, agents_view = proxy.Handler._get_agents(h)
    applied = (next(c for c in agents_view.get("clients", [])
                    if c.get("id") == "codex").get("applied") or {})
    check("agent overview returns HTTP 200", code == 200, (code, agents_view))
    check("the selected agent shows the AMD upstream",
          applied.get("upstream") == "AMD", applied)
    check("the selected agent shows the third-party address",
          applied.get("upstream_base_url")
          == "https://developer.amd.com.cn/radeon/api/v1", applied)
    check("the selected agent shows the selected default model",
          applied.get("upstream_model") == "AMD-Second", applied)

    print()
    print("[5] WorkBuddy re-apply disables the third-party exit")
    code, applied_result = proxy.Handler._handle_agents_apply(h, {
        "client": "codex",
        "base_url": "http://127.0.0.1:8788/v1",
        "key_id": "",
        "model": "deepseek-v4.1-flash",
        "models": workbuddy_ids,
        "restart": False,
    })
    check("WorkBuddy apply returns HTTP 200", code == 200, (code, applied_result))
    check("WorkBuddy apply reports that the third-party exit was disabled",
          applied_result.get("thirdparty_disabled") is True, applied_result)
    check("the third-party profile is now off", tp.is_enabled(accounts) is False)
    tp_code, tp_view = proxy.Handler._get_thirdparty(h)
    check("the third-party status endpoint still answers after the switch",
          tp_code == 200 and tp_view.get("enabled") is False,
          (tp_code, tp_view))

    print()
    print("[6] enabling the third-party card requires a client and uses it")
    sync_calls = []

    class FakeTP:
        def save(self, accounts_dir, profile):
            return {"enabled": True, "base_url": profile.get("base_url"),
                    "api_key": profile.get("api_key"),
                    "client_id": profile.get("client_id"),
                    "default_model": profile.get("default_model")}

        def load(self, accounts_dir):
            return {"enabled": True,
                    "base_url": "https://developer.amd.com.cn/radeon/api/v1",
                    "api_key": "sk-amd-test", "client_id": "codex",
                    "default_model": "AMD-Second"}

        def probe_models(self, accounts_dir):
            return ["DeepSeek-V4.1-Flash", "AMD-Second"], ""

        def model_ids(self, accounts_dir):
            return ["DeepSeek-V4.1-Flash", "AMD-Second"]

        def describe(self, accounts_dir):
            return {"enabled": True,
                    "base_url": "https://developer.amd.com.cn/radeon/api/v1",
                    "api_key_set": True, "client_id": "codex",
                    "default_model": "AMD-Second", "vendor": "AMD",
                    "models": ["DeepSeek-V4.1-Flash", "AMD-Second"]}

    orig_tp_module = proxy.thirdparty_module
    proxy.thirdparty_module = lambda: FakeTP()
    try:
        code, missing = proxy.Handler._handle_thirdparty_save(h, {
            "base_url": "https://developer.amd.com.cn/radeon/api/v1",
            "api_key": "sk-amd-test",
        })
        check("an enable without a selected client is rejected",
              code == 400, (code, missing))

        h._agents_sync_client_configs = lambda **kwargs: (
            sync_calls.append(kwargs) or
            {"available": True, "results": [{"client": "codex"}]})
        code, saved = proxy.Handler._handle_thirdparty_save(h, {
            "base_url": "https://developer.amd.com.cn/radeon/api/v1",
            "api_key": "sk-amd-test",
            "client_id": "codex",
            "default_model": "AMD-Second",
        })
        check("an enable with a selected client succeeds",
              code == 200 and saved.get("agents", {}).get("results"),
              (code, saved))
        check("the selected client and default model reach the sync",
              sync_calls and sync_calls[-1].get("client_ids") == ["codex"]
              and sync_calls[-1].get("default_model") == "AMD-Second",
              sync_calls)
    finally:
        proxy.thirdparty_module = orig_tp_module

    print()
    print("[7] native web tools are removed on the third-party path")
    stripped = proxy.strip_native_web_tools({
        "tools": [
            {"type": "web_search"},
            {"type": "web_search_preview"},
            {"type": "function", "function": {"name": "keep_me"}},
        ],
        "tool_choice": {"type": "web_search"},
    })
    check("native web_search is removed",
          all(t.get("type") != "web_search" for t in stripped.get("tools", [])),
          stripped)
    check("ordinary function tools are kept",
          any(t.get("function", {}).get("name") == "keep_me"
              for t in stripped.get("tools", [])), stripped)
    check("a dangling tool_choice is removed",
          "tool_choice" not in stripped, stripped)
    stripped_empty = proxy.strip_native_web_tools({
        "tools": [{"type": "web_search"}],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    })
    check("tool_choice/parallel_tool_calls are removed with the last tool",
          "tools" not in stripped_empty
          and "tool_choice" not in stripped_empty
          and "parallel_tool_calls" not in stripped_empty,
          stripped_empty)

    print()
    print("[8] panel layout keeps WorkBuddy info and third-party controls separate")
    dashboard = open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()
    check("the connection card is labelled WorkBuddy-specific",
          "WorkBuddy 连接信息" in dashboard)
    check("the third-party card is above the local-client grid",
          dashboard.index('id="tpAgentSelect"')
          < dashboard.index('id="agentCardGrid"'),
          (dashboard.index('id="tpAgentSelect"'),
           dashboard.index('id="agentCardGrid"')))
    check("the panel has an agent selector and a default-model selector",
          'id="tpAgentSelect"' in dashboard and 'id="tpDefaultModel"' in dashboard)
    check("the panel rejects enable without a selected agent",
          "请选择使用第三方上游的智能体" in dashboard)
    check("the panel auto-refreshes models after URL and key input",
          "tpScheduleAutoProbe" in dashboard
          and "addEventListener('input', tpScheduleAutoProbe)" in dashboard)
finally:
    proxy.ACCOUNTS_DIR = orig_accounts
    proxy.API_KEY = orig_api_key
    agents._resolve_home = orig_resolve_home
    shutil.rmtree(home, ignore_errors=True)
    shutil.rmtree(accounts, ignore_errors=True)

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
