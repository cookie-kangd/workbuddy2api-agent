"""Third-party upstream -> local client integration synchronization.

The bug this pins is subtle: selecting a third-party upstream changes the
chat route, but clients such as Codex bake their model catalog into their own
config. A re-apply must therefore use the third-party model list, not the
WorkBuddy list that happened to be in the old config or the browser's stale
picker. Enabling the upstream must also synchronize clients automatically:
configured clients are re-applied, and an installed but unconfigured client
gets the same one-click setup.
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

    # A previously configured Codex client with a WorkBuddy model, plus an
    # installed OpenCode config that has never been through this gateway.
    agents.integrate(accounts, "codex", "http://127.0.0.1:8788/v1",
                     "sk-gateway", model="deepseek-v4.1-flash",
                     models=[{"id": "deepseek-v4.1-flash"}],
                     home=home)
    open_dir = os.path.join(home, ".config", "opencode")
    os.makedirs(open_dir, exist_ok=True)
    with open(os.path.join(open_dir, "opencode.json"), "w", encoding="utf-8") as fh:
        json.dump({"$schema": "https://opencode.ai/config.json"}, fh)

    with open(tp.config_path(accounts), "w", encoding="utf-8") as fh:
        json.dump({
            "enabled": True,
            "base_url": "https://amd.example/v1",
            "api_key": "sk-amd-test",
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
    h._json = lambda code, obj: (code, obj)
    h._error = lambda code, msg, typ="": (code, {"error": msg})

    print("[1] a stale panel payload cannot put WorkBuddy models back")
    code, result = proxy.Handler._handle_agents_apply(h, {
        "client": "codex",
        "base_url": "http://127.0.0.1:8788/v1",
        "key_id": "",
        "model": "deepseek-v4.1-flash",
        "models": ["deepseek-v4.1-flash", "workbuddy-only"],
        "restart": False,
    })
    catalog_path = os.path.join(home, ".codex", "wb-proxy-model-catalog.json")
    catalog = json.load(open(catalog_path, encoding="utf-8"))
    slugs = [m.get("slug") for m in catalog.get("models", [])]
    check("apply returns HTTP 200", code == 200, (code, result))
    check("the catalog contains the third-party model",
          "DeepSeek-V4.1-Flash" in slugs, slugs)
    check("the catalog does not contain the stale WorkBuddy model",
          "workbuddy-only" not in slugs, slugs)
    check("the saved model is the third-party id",
          agents.integration_state(accounts)["codex"].get("model")
          == "DeepSeek-V4.1-Flash",
          agents.integration_state(accounts)["codex"].get("model"))

    print()
    print("[2] enabling synchronizes configured and newly detected clients")
    sync = h._agents_sync_client_configs(restart=False)
    clients = {r.get("client") for r in sync.get("results", [])}
    state = agents.integration_state(accounts)
    check("sync reports a client result", bool(sync.get("results")), sync)
    check("both installed clients were synchronized",
          {"codex", "opencode"}.issubset(clients), sorted(clients))
    check("the unconfigured client is now recorded",
          "opencode" in state, sorted(state))
    codex_catalog = json.load(open(catalog_path, encoding="utf-8"))
    codex_slugs = [m.get("slug") for m in codex_catalog.get("models", [])]
    check("the re-applied catalog keeps the AMD models",
          codex_slugs == ["DeepSeek-V4.1-Flash", "AMD-Second"], codex_slugs)

    print()
    print("[3] the picker follows the third-party exit for every realm")
    check("intl picks the third-party list",
          [m["id"] for m in h._agents_models_for("intl")]
          == ["DeepSeek-V4.1-Flash", "AMD-Second"],
          h._agents_models_for("intl"))
    check("cn picks the same third-party list",
          [m["id"] for m in h._agents_models_for("cn")]
          == ["DeepSeek-V4.1-Flash", "AMD-Second"],
          h._agents_models_for("cn"))

    print()
    print("[4] enabling the third-party card invokes the automatic sync")
    sync_calls = []
    disable_calls = []

    class FakeTP:
        def save(self, accounts_dir, profile):
            return {"enabled": True, "base_url": profile.get("base_url"),
                    "api_key": profile.get("api_key")}

        def load(self, accounts_dir):
            return {"enabled": True, "base_url": "https://amd.example/v1",
                    "api_key": "sk-amd-test"}

        def probe_models(self, accounts_dir):
            return ["DeepSeek-V4.1-Flash", "AMD-Second"], ""

        def describe(self, accounts_dir):
            return {"enabled": True, "base_url": "https://amd.example/v1",
                    "api_key_set": True, "models": ["DeepSeek-V4.1-Flash"]}

    orig_tp_module = proxy.thirdparty_module
    proxy.thirdparty_module = lambda: FakeTP()
    try:
        h._agents_sync_client_configs = lambda restart=True: (
            sync_calls.append(restart) or
            {"available": True, "results": [{"client": "codex"}]})
        code, saved = proxy.Handler._handle_thirdparty_save(h, {
            "base_url": "https://amd.example/v1",
            "api_key": "sk-amd-test",
        })
        check("save returns the synced agent report",
              code == 200 and saved.get("agents", {}).get("results"),
              (code, saved))
        check("save requested a restarting sync",
              sync_calls == [True], sync_calls)

        h._agents_reapply_for_realm = lambda restart=None: (
            disable_calls.append(restart) or
            {"available": True, "results": [{"client": "codex"}]})
        code, disabled = proxy.Handler._handle_thirdparty_disable(h, {})
        check("disable returns the restored agent report",
              code == 200 and disabled.get("agents", {}).get("results"),
              (code, disabled))
        check("disable requested a restarting restore",
              disable_calls == [True], disable_calls)
    finally:
        proxy.thirdparty_module = orig_tp_module
finally:
    proxy.ACCOUNTS_DIR = orig_accounts
    proxy.API_KEY = orig_api_key
    agents._resolve_home = orig_resolve_home
    shutil.rmtree(home, ignore_errors=True)
    shutil.rmtree(accounts, ignore_errors=True)

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
