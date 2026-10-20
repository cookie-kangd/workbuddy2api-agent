"""DSH 一键配置写进 profile 补丁层：模型输入模态 + 保留用户配置。

issue #…：DSH 的 pi-ai 适配器只认模型条目里的 `input`（`inputModalities`），
不认 `supportsVision`，也不读 `/v1/models` 的能力字段。一键配置过去只写
`~/.dsh/settings.yaml`（当前 DSH 只在首次启动导入一次，之后改名
`settings.yaml.imported` 不再读），于是桌面版 profile 的
`~/.dsh/profiles/*/cordis.patch.yml` 里没有 `input`，贴图就被拒：
`Model "<id>" does not support image input.`

这个套件锁住修复后的契约：

  - `wb_agents.dsh_profile_patches()`   找到各 profile 的 patch 层
  - `wb_agents._dsh_files()`            patch 存在时写 patch（settings.yaml 仅在已存在时保留）
  - `wb_agents.dsh_patch_upsert()`      只改 llm-pi-ai / agent-default-model，别处原样保留
  - `wb_agents._apply_dsh()`            vision 模型写 `input: [text, image]`，纯文本模型不写
  - `wb_agents.restore()`               恢复 patch 原始字节

    python tests/_test_dsh_profile_patch.py
"""

import http.server
import json
import os
import shutil
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_agents as A

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


TMPDIRS = []


def temp_dir(prefix):
    d = tempfile.mkdtemp(prefix=prefix)
    TMPDIRS.append(d)
    return d


PATCH = """# my own comment
- id: agent-default-model
  name: "@deepseek-ai/dsh-agent-default-model"
  config:
    provider: old
    model: old
    reasoningEffort: max
- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      qwen-token-plan-cn:
        apiKeyEnv: QWEN_KEY
        models:
          - id: qwen3.8-max
            contextWindow: 1000000
- id: compaction-basic
  name: "@deepseek-ai/dsh-compaction-basic"
  config:
    thresholdRatio: 0.6
"""

MODELS = [
    {"id": "m-vision", "context_window": 1000000,
     "supports_vision": True},
    {"id": "m-text", "context_window": 256000},
]


def write_patch(home, text, profile="desktop"):
    directory = os.path.join(home, ".dsh", "profiles", profile)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "cordis.patch.yml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def model_block(text, model_id):
    """The lines of one wb-proxy model entry (until the next `- id:`)."""
    lines = text.split("\n")
    start = None
    for idx, line in enumerate(lines):
        if line.strip() == "- id: " + model_id:
            start = idx
            break
    if start is None:
        return []
    out = []
    for line in lines[start + 1:]:
        if line.strip().startswith("- id: "):
            break
        out.append(line)
    return out


# ---------------------------------------------------------------------------
print("[1] dsh_profile_patches / _dsh_files：有 profile 就写 profile 补丁层")
# ---------------------------------------------------------------------------

home_plain = temp_dir("wb-dsh-patch-plain-")
check("没有 profiles 目录时列表为空",
      A.dsh_profile_patches(home_plain) == [], A.dsh_profile_patches(home_plain))
files_plain = A._dsh_files(home_plain)
check("没有 profile 时沿用 legacy settings.yaml",
      files_plain.get("settings") == os.path.join(
          home_plain, ".dsh", "settings.yaml"), files_plain)
check("credentials 始终在目标里", "credentials" in files_plain, files_plain)

home_p = temp_dir("wb-dsh-patch-home-")
patch_p = write_patch(home_p, PATCH)
patches = A.dsh_profile_patches(home_p)
check("找到 desktop 的 cordis.patch.yml", patches == [patch_p], patches)
files_p = A._dsh_files(home_p)
check("profile patch 是写入目标", files_p.get("patch-0") == patch_p, files_p)
check("settings.yaml 不存在时不再创建", "settings" not in files_p, files_p)

home_both = temp_dir("wb-dsh-patch-both-")
write_patch(home_both, PATCH)
settings_both = os.path.join(home_both, ".dsh", "settings.yaml")
with open(settings_both, "w", encoding="utf-8") as fh:
    fh.write("")
files_both = A._dsh_files(home_both)
check("settings.yaml 已存在时一并保留",
      files_both.get("settings") == settings_both, files_both)

# ---------------------------------------------------------------------------
print()
print("[2] dsh_patch_upsert：只改自己那两个条目")
# ---------------------------------------------------------------------------

out = A.dsh_patch_upsert(PATCH, {"displayName": "WB Proxy",
                                 "models": [{"id": "m-vision"}]},
                         "m-vision")
check("注释原样保留", "# my own comment" in out)
check("其他 provider 原样保留",
      "qwen-token-plan-cn:" in out and "apiKeyEnv: QWEN_KEY" in out)
check("其他条目原样保留",
      "- id: compaction-basic" in out and "thresholdRatio: 0.6" in out)
check("默认路由换成网关", 'provider: "wb-proxy"' in out
      and 'model: "m-vision"' in out, out)
check("用户设的 reasoningEffort 不丢", "reasoningEffort: max" in out, out)
check("幂等：再跑一次字节不变", A.dsh_patch_upsert(
    out, {"displayName": "WB Proxy", "models": [{"id": "m-vision"}]},
    "m-vision") == out)
missing = A.dsh_patch_upsert(
    "- id: ui-chat\n  config:\n    enabled: true\n",
    {"displayName": "WB Proxy", "models": [{"id": "m-vision"}]}, "m-vision")
check("缺条目时补出 llm-pi-ai 与 agent-default-model",
      "- id: llm-pi-ai" in missing and "- id: agent-default-model" in missing,
      missing)
check("补出来的条目保留原有条目",
      "- id: ui-chat" in missing and "enabled: true" in missing, missing)
check("空 patch（[]）被替换成块状条目",
      A.dsh_patch_upsert("[]\n", {"displayName": "WB Proxy", "models": []},
                         None).startswith("- id: llm-pi-ai"))

# ---------------------------------------------------------------------------
print()
print("[3] _apply_dsh：vision 模型写 input，纯文本模型不写")
# ---------------------------------------------------------------------------

acct = temp_dir("wb-dsh-patch-acct-")
home3 = temp_dir("wb-dsh-patch-home3-")
patch3 = write_patch(home3, PATCH)
A.integrate(acct, "dsh", "http://gw:1", "k-patch", model="m-vision",
            models=MODELS, home=home3)
text3 = read(patch3)
vision_block = "\n".join(model_block(text3, "m-vision"))
text_block = "\n".join(model_block(text3, "m-text"))
check("vision 模型写入 input: [text, image]",
      "input:" in vision_block and "- image" in vision_block
      and "- text" in vision_block, vision_block)
check("纯文本模型不写 input", "input:" not in text_block, text_block)
check("不再写 DSH 不认的 supportsVision", "supportsVision" not in text3)
check("上下文窗口照旧写入", "contextWindow: 1000000" in vision_block,
      vision_block)
check("settings.yaml 没有被顺手创建",
      not os.path.exists(os.path.join(home3, ".dsh", "settings.yaml")))
with open(os.path.join(home3, ".dsh", ".credentials.yaml"),
          encoding="utf-8") as fh:
    cred = fh.read()
check("凭据写进 .credentials.yaml", "WB_PROXY_API_KEY: k-patch" in cred, cred)

before = read(patch3)
A.integrate(acct, "dsh", "http://gw:1", "k-patch", model="m-vision",
            models=MODELS, home=home3)
check("重复 apply 幂等（patch 字节不变）", read(patch3) == before)

# ---------------------------------------------------------------------------
print()
print("[4] 限流冷却等待：wb-proxy 的 retryPolicy 放宽到能跨过 ~60s 冷却")
# ---------------------------------------------------------------------------


def provider_block(text, name):
    """A provider entry's lines inside `llm-pi-ai` providers: (dedented)."""
    lines = text.split("\n")
    start = None
    for idx, line in enumerate(lines):
        if line.strip() == name + ":":
            start = idx
            break
    if start is None:
        return ""
    indent = len(lines[start]) - len(lines[start].lstrip())
    out = []
    for line in lines[start + 1:]:
        if not line.strip():
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        out.append(line.strip())
    return "\n".join(out)


def cumulative_retry_ms(policy):
    """Total backoff DSH spends before giving up (local backoff, no hint)."""
    total = 0
    delay = policy["backoff"]["initialDelayMs"]
    for _ in range(policy["maxRetries"]):
        total += delay
        delay = min(delay * 2, policy["backoff"]["maxDelayMs"])
    return total


policy = A.DSH_RETRY_POLICY
check("重试策略是 DSH 认识的 normal 模式", policy.get("mode") == "normal",
      policy)
check("退避参数自洽（起点 <= 封顶，抖动在 0..1）",
      0 < policy["backoff"]["initialDelayMs"]
      <= policy["backoff"]["maxDelayMs"], policy)
check("最多重试 8 次", policy.get("maxRetries") == 8, policy)
check("不覆写 retryableCodes（沿用 DSH 默认的可重试错误码）",
      "retryableCodes" not in policy, policy)
check("重试总时长 >= 120s，覆盖一次 ~60s 冷却",
      cumulative_retry_ms(policy) >= 120000, cumulative_retry_ms(policy))

home_r = temp_dir("wb-dsh-retry-home-")
acct_r = temp_dir("wb-dsh-retry-acct-")
patch_r = write_patch(home_r, PATCH)
A.integrate(acct_r, "dsh", "http://gw:1", "k", model="m-vision",
            models=MODELS, home=home_r)
text_r = read(patch_r)
wb_block = provider_block(text_r, "wb-proxy")
qwen_block = provider_block(text_r, "qwen-token-plan-cn")
check("wb-proxy 条目写入 retryPolicy", "retryPolicy:" in wb_block, wb_block)
check("mode / maxRetries 落到 YAML",
      "mode: normal" in wb_block and "maxRetries: 8" in wb_block, wb_block)
check("backoff 三个字段都写",
      "initialDelayMs: 2000" in wb_block
      and "maxDelayMs: 30000" in wb_block
      and "jitterRatio: 0.1" in wb_block, wb_block)
check("其它 provider 不受影响", "retryPolicy" not in qwen_block, qwen_block)
check("retryPolicy 不落到别的 loader 条目",
      text_r.count("retryPolicy:") == 1, text_r.count("retryPolicy:"))
before_r = read(patch_r)
A.integrate(acct_r, "dsh", "http://gw:1", "k", model="m-vision",
            models=MODELS, home=home_r)
check("重复 apply 幂等（含 retryPolicy 的 patch 字节不变）",
      read(patch_r) == before_r)

home_legacy = temp_dir("wb-dsh-retry-legacy-")
acct_legacy = temp_dir("wb-dsh-retry-legacy-acct-")
settings_legacy = os.path.join(home_legacy, ".dsh", "settings.yaml")
os.makedirs(os.path.dirname(settings_legacy), exist_ok=True)
with open(settings_legacy, "w", encoding="utf-8") as fh:
    fh.write("")
A.integrate(acct_legacy, "dsh", "http://gw:1", "k", model="m-vision",
            models=MODELS, home=home_legacy)
legacy_text = read(settings_legacy)
check("legacy settings.yaml 路径也带 retryPolicy",
      '"retryPolicy"' in legacy_text and '"maxRetries": 8' in legacy_text,
      legacy_text)

# ---------------------------------------------------------------------------
print()
print("[5] 多个 profile 都写；restore 还原原始字节")
# ---------------------------------------------------------------------------

home4 = temp_dir("wb-dsh-patch-home4-")
acct4 = temp_dir("wb-dsh-patch-acct4-")
desktop = write_patch(home4, PATCH, "desktop")
web = write_patch(home4, "# web\n[]\n", "web")
A.integrate(acct4, "dsh", "http://gw:1", "k", model="m-vision",
            models=MODELS, home=home4)
check("desktop patch 写入 provider", "wb-proxy:" in read(desktop))
web_text = read(web)
check("web 的空 patch 也写入", "- id: llm-pi-ai" in web_text
      and "[]" not in web_text, web_text)
check("两个 profile 都带 input",
      "- image" in read(desktop) and "- image" in web_text)

A.restore(acct4, "dsh", home=home4)
check("restore 还原 desktop 原始字节", read(desktop) == PATCH)
check("restore 还原 web 原始字节", read(web) == "# web\n[]\n")
check("restore 删除自己创建的凭据",
      not os.path.exists(os.path.join(home4, ".dsh", ".credentials.yaml")))

# ---------------------------------------------------------------------------
print()
print("[6] 配置器 CLI 从 /v1/models 取模型表时也带上识图标记")
# ---------------------------------------------------------------------------


class StubModels(http.server.BaseHTTPRequestHandler):
    payload = {"object": "list", "data": []}

    def do_GET(self):
        body = json.dumps(self.payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StubModels)
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    base = "http://127.0.0.1:%d" % server.server_address[1]
    StubModels.payload = {"data": [
        {"id": "m-vision", "supports_vision": True,
         "capabilities": {"vision": True}},
        {"id": "m-text", "supports_vision": False,
         "capabilities": {"vision": False}},
        {"id": "m-generic", "capabilities": {"vision": True}},
    ]}
    fetched = {m["id"]: m for m in A.fetch_models_from_gateway(base, "k")}
finally:
    server.shutdown()
    server.server_close()
check("supports_vision 透传到 CLI 模型条目",
      fetched.get("m-vision", {}).get("supports_vision") is True,
      fetched.get("m-vision"))
check("纯文本模型不标 vision",
      "supports_vision" not in fetched.get("m-text", {}),
      fetched.get("m-text"))
check("只有 capabilities.vision 时也认",
      fetched.get("m-generic", {}).get("supports_vision") is True,
      fetched.get("m-generic"))

for d in TMPDIRS:
    shutil.rmtree(d, ignore_errors=True)

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
