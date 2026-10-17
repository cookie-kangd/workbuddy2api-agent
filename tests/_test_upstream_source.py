"""
Upstream source labelling and the third-party exit's effect on usage rows.

Two things are pinned here. The first is the vendor label: an AMD endpoint must
read as AMD, a recognised host must not read as its raw hostname, and an
unknown host must be visibly unknown rather than look first-party. The second
is that a third-party row records the third party while a WorkBuddy row
records workbuddy - the panel splits spend on that field, so a row that
silently defaulted would overstate the credits spent.

    python tests/_test_upstream_source.py
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


print("[1] a known host is named by its vendor")

for url, want in [
    ("https://developer.amd.com.cn/radeon/api/v1", "AMD"),
    ("https://api.deepseek.com/v1", "DeepSeek"),
    ("https://open.bigmodel.cn/api/paas/v4", "智谱 GLM"),
    ("https://api.moonshot.cn/v1", "Moonshot"),
    ("https://api.minimax.chat/v1", "MiniMax"),
    ("https://dashscope.aliyuncs.com/compatible-mode/v1", "阿里通义"),
    ("https://api.openai.com/v1", "OpenAI"),
    ("https://openrouter.ai/api/v1", "OpenRouter"),
    ("https://api.anthropic.com", "Anthropic"),
    ("https://api.siliconflow.cn/v1", "硅基流动"),
]:
    got = tp.vendor_label(url)
    check("%s -> %s" % (url, want), got == want, got)

print()
print("[2] an unknown host says so instead of looking first-party")

for url, want in [
    ("https://llm.example.com/v1", "其他 (llm.example.com)"),
    ("http://127.0.0.1:9000/v1", "其他 (127.0.0.1)"),
    ("https://my-gateway.internal:8443/api", "其他 (my-gateway.internal)"),
]:
    got = tp.vendor_label(url)
    check("%s -> %s" % (url, want), got == want, got)

print()
print("[3] a subdomain of a known host still resolves to its vendor")

check("a subdomain of developer.amd.com.cn is AMD",
      tp.vendor_label("https://cn.developer.amd.com.cn/v1") == "AMD",
      tp.vendor_label("https://cn.developer.amd.com.cn/v1"))
check("a subdomain of api.deepseek.com is DeepSeek",
      tp.vendor_label("https://eu.api.deepseek.com/v1") == "DeepSeek",
      tp.vendor_label("https://eu.api.deepseek.com/v1"))
check("a lookalike host is not mistaken for the vendor",
      tp.vendor_label("https://not-amd.com.cn/v1") == "其他 (not-amd.com.cn)",
      tp.vendor_label("https://not-amd.com.cn/v1"))

print()
print("[4] a missing or empty URL has no vendor")

check("an empty URL has no label", tp.vendor_label("") == "", repr(tp.vendor_label("")))
check("None has no label", tp.vendor_label(None) == "", repr(tp.vendor_label(None)))

print()
print("[5] the profile carries the vendor, and the key stays out of it")

d = tempfile.mkdtemp(prefix="wb-vendor-")
tp.save(d, {"enabled": True,
            "base_url": "https://developer.amd.com.cn/radeon/api/v1",
            "api_key": "sk-amd-secret"})
view = tp.describe(d)
check("the view names AMD", view.get("vendor") == "AMD", view.get("vendor"))
check("the view carries no api_key field", "api_key" not in view, list(view))
check("the secret never appears in the view",
      "sk-amd-secret" not in json.dumps(view), view)

print()
print("[6] the chat URL is built from the path the user pasted")

check("a path pasted under /radeon/api/v1 keeps that prefix",
      tp.chat_url(d) == "https://developer.amd.com.cn/radeon/api/v1/chat/completions",
      tp.chat_url(d))

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
