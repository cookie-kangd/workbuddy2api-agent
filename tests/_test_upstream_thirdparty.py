"""
Third-party upstream: the switch, the URL normaliser and the restore path.

The point of the feature is that turning it off undoes turning it on, so the
assertions here spend most of their time on that: a disabled profile answers
None for a chat URL, a saved profile survives a restart (it is read from disk,
not kept in memory), and an endpoint whose URL moved never serves the previous
endpoint's model list.

    python tests/_test_upstream_thirdparty.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

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


print("[1] an unconfigured deployment is simply off")

d = tempfile.mkdtemp(prefix="wb-tp-")
check("defaults to disabled", tp.load(d)["enabled"] is False, tp.load(d))
check("no chat URL while off", tp.chat_url(d) is None, tp.chat_url(d))
check("no models while off", tp.model_ids(d) == [], None)

print()
print("[2] the URL normaliser never doubles a path")

for raw, want in [
    ("https://api.example.com", "https://api.example.com/v1/chat/completions"),
    ("https://api.example.com/v1", "https://api.example.com/v1/chat/completions"),
    ("https://api.example.com/v1/", "https://api.example.com/v1/chat/completions"),
    ("https://api.example.com/v1/chat/completions",
     "https://api.example.com/v1/chat/completions"),
    ("http://127.0.0.1:9000/v1", "http://127.0.0.1:9000/v1/chat/completions"),
]:
    got = tp.normalise_chat_url(raw)
    check("%s -> %s" % (raw, want), got == want, got)
check("a non-http URL is refused",
      tp.normalise_chat_url("ftp://x/v1") is None, tp.normalise_chat_url("ftp://x/v1"))
check("an empty URL is refused", tp.normalise_chat_url("") is None, None)

print()
print("[3] enabling, then disabling, restores the WorkBuddy exit")

tp.save(d, {"enabled": True,
            "base_url": "https://api.example.com/v1", "api_key": "sk-test"})
check("enabled after save", tp.is_enabled(d) is True, tp.load(d))
check("chat URL points at the endpoint",
      tp.chat_url(d) == "https://api.example.com/v1/chat/completions",
      tp.chat_url(d))
check("the key is stored", tp.load(d)["api_key"] == "sk-test", None)

tp.save(d, {"enabled": False,
            "base_url": "https://api.example.com/v1", "api_key": "sk-test"})
check("disabled after saving disabled", tp.is_enabled(d) is False, None)
check("chat URL is gone while off", tp.chat_url(d) is None, tp.chat_url(d))
check("the profile is kept so re-enabling is one click",
      tp.load(d)["base_url"] == "https://api.example.com/v1", tp.load(d))
check("the key is kept too", tp.load(d)["api_key"] == "sk-test", None)

print()
print("[4] the profile survives a restart (it is on disk)")

# Section [3] ended with it switched off, so re-enable first: the point of this
# section is that a profile saved before the restart is still there after it.
tp.save(d, {"enabled": True,
            "base_url": "https://api.example.com/v1", "api_key": "sk-test"})

# Re-importing mimics a fresh process; the module keeps no in-memory state.
import importlib  # noqa: E402
tp2 = importlib.reload(tp)
check("still enabled after reload", tp2.is_enabled(d) is True, tp2.load(d))
check("chat URL survives the reload",
      tp2.chat_url(d) == "https://api.example.com/v1/chat/completions",
      tp2.chat_url(d))

tp3 = importlib.reload(tp)   # leave the module as the tests found it
check("final state is enabled", tp3.is_enabled(d) is True, None)

print()
print("[5] a moved endpoint never serves the previous endpoint's models")

# Pretend a model list was cached for endpoint A.
with open(tp.config_path(d), encoding="utf-8") as fh:
    data = json.load(fh)
data["models"] = ["model-a-1", "model-a-2"]
data["models_at"] = 1e18
with open(tp.config_path(d), "w", encoding="utf-8", newline="\n") as fh:
    fh.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")

# Saving a *different* URL must drop that list, not keep advertising it.
tp.save(d, {"enabled": True, "base_url": "https://api.another.com/v1",
            "api_key": "sk-test"})
fresh = tp.load(d)
check("the stale model list is dropped",
      fresh["models"] == [] and fresh["models_at"] == 0, fresh)

# ...while re-saving the same endpoint keeps it (it still describes it).
tp.save(d, {"enabled": True, "base_url": "https://api.another.com/v1",
            "api_key": "sk-test"})
check("re-saving the same endpoint keeps the (empty) list",
      tp.load(d)["models"] == [], tp.load(d))

print()
print("[6] the panel view never carries the key")

tp.save(d, {"enabled": True, "base_url": "https://api.example.com/v1",
            "api_key": "sk-super-secret"})
view = tp.describe(d)
check("no api_key in the view", "api_key" not in view, list(view))
check("api_key_set is reported", view["api_key_set"] is True, view)
check("the key value never appears in the rendered view",
      "sk-super-secret" not in json.dumps(view), view)

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
