"""Claude Desktop one-click setup, client restart, and the role-route alias.

Three things this pins, none of which the generic agents suite covers:

  * the Claude Desktop profile is written in the shape Claude Desktop itself
    accepts (the layout CC Switch writes: deploymentMode=3p in both config
    files, a configLibrary profile plus the _meta.json index that lists it),
  * a client that reads its config once at startup can be restarted by the
    gateway, matched by executable path rather than by image name,
  * the `claude-<role>-<n>` ids the profile is forced to advertise resolve
    back to the model the operator picked.

    python tests/_test_claude_desktop.py
"""

import json
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Before wb_agents is imported: restarting a client kills processes, and this
# suite's restart test replaces the process table with a fake. Without the kill
# switch, an unstubbed call could reach the operator's real Codex / Claude
# Desktop instead of the fake table.
os.environ["WB_AGENTS_NO_RESTART"] = "1"

import wb_agents as A  # noqa: E402

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


print("[1] Claude Desktop paths follow the injected home")

home = tempfile.mkdtemp(prefix="wb-claude-desktop-")
state = tempfile.mkdtemp(prefix="wb-claude-state-")
paths = A._claude_desktop_paths(home)
check("config lives under the injected home",
      paths["config"].startswith(home) and paths["config3p"].startswith(home),
      paths)
check("profile lives in the 3p configLibrary",
      os.path.basename(os.path.dirname(paths["profile"]))
      == A.CLAUDE_DESKTOP_LIBRARY_DIR, paths["profile"])
check("profile file is named after the profile id",
      os.path.basename(paths["profile"]) == A.CLAUDE_DESKTOP_PROFILE_ID + ".json",
      paths["profile"])
check("_meta.json sits next to the profile",
      os.path.dirname(paths["meta"]) == os.path.dirname(paths["profile"]),
      paths["meta"])

print()
print("[2] apply writes the profile Claude Desktop accepts")

result = A.integrate(state, "claude-desktop", "http://127.0.0.1:8788/v1",
                     "sk-cd-test", model="deepseek-v4.1-flash", home=home)
written = {entry["path"] for entry in result["files"]}
check("all four files are written", len(written) == 4, result["files"])

normal = read_json(paths["config"])
threep = read_json(paths["config3p"])
check("normal config switches to 3p deployment",
      normal.get("deploymentMode") == "3p", normal)
check("3p config switches to 3p deployment",
      threep.get("deploymentMode") == "3p", threep)

profile = read_json(paths["profile"])
check("inferenceProvider is gateway", profile.get("inferenceProvider") == "gateway", profile)
check("gateway base url keeps no /v1 suffix",
      profile.get("inferenceGatewayBaseUrl") == "http://127.0.0.1:8788", profile)
check("api key is written", profile.get("inferenceGatewayApiKey") == "sk-cd-test", profile)
check("auth scheme is bearer",
      profile.get("inferenceGatewayAuthScheme") == "bearer", profile)
check("deployment chooser is disabled",
      profile.get("disableDeploymentModeChooser") is True, profile)
check("cowork egress is opened for the gateway",
      profile.get("coworkEgressAllowedHosts") == ["*"], profile)
check("every advertised model id is Claude-Desktop-safe",
      all(str(m).startswith("claude-") for m in profile.get("inferenceModels") or []),
      profile.get("inferenceModels"))
check("the raw upstream model id is never advertised",
      "deepseek-v4.1-flash" not in json.dumps(profile), profile)

meta = read_json(paths["meta"])
check("library index lists our profile",
      {"id": A.CLAUDE_DESKTOP_PROFILE_ID, "name": A.CLAUDE_DESKTOP_PROFILE_NAME}
      in meta.get("entries") or [], meta)
check("library index makes it current",
      meta.get("appliedId") == A.CLAUDE_DESKTOP_PROFILE_ID, meta)

print()
print("[3] re-apply keeps unrelated keys and never duplicates the index entry")

normal["userSetting"] = 42
threep["enterpriseConfig"] = {"keep": True}
meta["entries"].append({"id": "other-profile", "name": "Someone Else"})
with open(paths["config"], "w", encoding="utf-8") as fh:
    json.dump(normal, fh)
with open(paths["config3p"], "w", encoding="utf-8") as fh:
    json.dump(threep, fh)
with open(paths["meta"], "w", encoding="utf-8") as fh:
    json.dump(meta, fh)

A.integrate(state, "claude-desktop", "http://127.0.0.1:8788/v1",
            "sk-cd-test-2", model="glm-5.3", home=home)
normal2 = read_json(paths["config"])
threep2 = read_json(paths["config3p"])
meta2 = read_json(paths["meta"])
check("an unrelated key in the normal config survives",
      normal2.get("userSetting") == 42, normal2)
check("an unrelated key in the 3p config survives",
      threep2.get("enterpriseConfig") == {"keep": True}, threep2)
check("someone else's library entry survives",
      {"id": "other-profile", "name": "Someone Else"} in meta2.get("entries"), meta2)
check("our library entry is not duplicated",
      [e.get("id") for e in meta2.get("entries")].count(A.CLAUDE_DESKTOP_PROFILE_ID) == 1,
      meta2)
check("the key is refreshed",
      read_json(paths["profile"])["inferenceGatewayApiKey"] == "sk-cd-test-2",
      read_json(paths["profile"]))

print()
print("[4] the integration record keeps the key id, never the key")

record = A.integration_state(state).get("claude-desktop") or {}
check("the record names the chosen model",
      record.get("model") == "glm-5.3", record)
with open(os.path.join(state, "integration-state.json"), encoding="utf-8") as fh:
    raw_state = fh.read()
check("no api key is written into the state file", "sk-cd-test" not in raw_state,
      raw_state[:200])

print()
print("[5] restore puts every touched file back")

# A clean home: every file we touch is one we created, so restore has to take
# all of them back out again (and prune the directories it made).
fresh_home = tempfile.mkdtemp(prefix="wb-claude-restore-")
fresh_state = tempfile.mkdtemp(prefix="wb-claude-restore-state-")
fresh_paths = A._claude_desktop_paths(fresh_home)
A.integrate(fresh_state, "claude-desktop", "http://127.0.0.1:8788",
            "sk-cd-restore", model="glm-5.3", home=fresh_home)
check("apply created the profile",
      os.path.isfile(fresh_paths["profile"]), fresh_paths["profile"])
A.restore(fresh_state, "claude-desktop", home=fresh_home)
check("config file is gone again", not os.path.exists(fresh_paths["config"]))
check("3p config file is gone again", not os.path.exists(fresh_paths["config3p"]))
check("profile file is gone again", not os.path.exists(fresh_paths["profile"]))
check("library index is gone again", not os.path.exists(fresh_paths["meta"]))
check("the directories we created are pruned",
      not os.path.exists(os.path.dirname(os.path.dirname(fresh_paths["profile"]))),
      os.path.dirname(fresh_paths["profile"]))

print()
print("[5b] a hand-written profile is restored byte for byte")

hand_home = tempfile.mkdtemp(prefix="wb-claude-hand-")
hand_state = tempfile.mkdtemp(prefix="wb-claude-hand-state-")
hand_paths = A._claude_desktop_paths(hand_home)
os.makedirs(os.path.dirname(hand_paths["profile"]), exist_ok=True)
original_profile = '{"inferenceProvider": "custom", "note": "mine"}\n'
with open(hand_paths["profile"], "w", encoding="utf-8", newline="") as fh:
    fh.write(original_profile)
A.integrate(hand_state, "claude-desktop", "http://127.0.0.1:8788",
            "sk-cd-hand", model="glm-5.3", home=hand_home)
A.restore(hand_state, "claude-desktop", home=hand_home)
with open(hand_paths["profile"], encoding="utf-8", newline="") as fh:
    restored = fh.read()
check("the original profile comes back byte for byte",
      restored == original_profile, restored)

print()
print("[6] processes are matched by executable path, not by image name")

SNAP = [
    # The unrelated ChatGPT desktop app: same image name, different package.
    {"pid": 10, "ppid": 1, "name": "ChatGPT.exe",
     "exe": r"C:\Users\x\AppData\Local\Programs\ChatGPT\ChatGPT.exe"},
    # The Codex desktop app and the app-server it spawns.
    {"pid": 20, "ppid": 1, "name": "ChatGPT.exe",
     "exe": r"C:\Program Files\WindowsApps\OpenAI.Codex_1.2.3.0_x64__abc123\app\ChatGPT.exe"},
    {"pid": 21, "ppid": 20, "name": "ChatGPT.exe",
     "exe": r"C:\Program Files\WindowsApps\OpenAI.Codex_1.2.3.0_x64__abc123\app\ChatGPT.exe"},
    {"pid": 22, "ppid": 20, "name": "codex.exe",
     "exe": r"C:\Users\x\AppData\Local\OpenAI\Codex\bin\deadbeef\codex.exe"},
]
codex_pids = [p["pid"] for p in A.client_processes("codex", snapshot=SNAP)]
check("the Codex app and its app-server are picked up",
      codex_pids == [20, 21, 22], codex_pids)
check("the unrelated ChatGPT app is left alone", 10 not in codex_pids, codex_pids)
check("a client without a process rule reports nothing",
      A.client_processes("dsh", snapshot=SNAP) == [], None)

check("the WindowsApps path yields a Start-menu id",
      A._windows_package_launch_id(SNAP[1]["exe"]) == "OpenAI.Codex_abc123!App",
      A._windows_package_launch_id(SNAP[1]["exe"]))
check("a plain path yields no Start-menu id",
      A._windows_package_launch_id(SNAP[0]["exe"]) is None, None)

print()
print("[7] restart stops the tree and relaunches once")

_orig_snapshot, _orig_terminate, _orig_launch = (
    A.process_snapshot, A._terminate, A.CLIENTS["codex"]["relaunch"])
terminated = []
launched = []
try:
    A.process_snapshot = lambda: SNAP
    # Everything this block can reach is a fake: the table is SNAP, the kill is
    # a recorder and the launcher is a stub. Safe to lift the kill switch here,
    # and it is the only way to exercise the real restart path.
    A.enable_restarts()
    A._terminate = lambda procs, pids: (terminated.append(sorted(pids)),
                                        sorted(pids))[1]
    A.CLIENTS["codex"]["relaunch"] = lambda matches: (
        launched.append([m["pid"] for m in matches]) or {"command": "stub"})
    report = A.restart_client("codex", wait=0)
finally:
    A.process_snapshot, A._terminate = _orig_snapshot, _orig_terminate
    A.CLIENTS["codex"]["relaunch"] = _orig_launch
    A.disable_restarts()

check("the whole app tree is stopped", terminated == [[20, 21, 22]], terminated)
check("the app is relaunched once", launched == [[20, 21, 22]], launched)
check("the report says it worked",
      report.get("started") is True and report.get("ok", True) is not False, report)

# A client that is not running is not an error: there is simply nothing to do.
_orig_snapshot = A.process_snapshot
try:
    A.process_snapshot = lambda: [SNAP[0]]
    A.enable_restarts()
    idle = A.restart_client("codex", wait=0)
finally:
    A.process_snapshot = _orig_snapshot
    A.disable_restarts()
check("an idle client reports nothing to restart",
      idle.get("running") is False and idle.get("started") is False, idle)

print()
print("[7b] the kill switch protects the operator's own processes")

A.disable_restarts()
try:
    blocked = A.restart_client("codex", wait=0)
finally:
    A.enable_restarts()
    A.disable_restarts()
check("a restart is refused while restarts are disabled",
      blocked.get("blocked") is True and blocked.get("stopped") == [], blocked)
check("the refusal says why",
      "WB_AGENTS_NO_RESTART" in str(blocked.get("reason")), blocked)

print()
print("[8] claude-<role>-<n> ids resolve to the configured model")

import wb_proxy as P  # noqa: E402

alias_home = tempfile.mkdtemp(prefix="wb-claude-alias-")
_orig_accounts = P.ACCOUNTS_DIR
try:
    P.ACCOUNTS_DIR = alias_home
    P._claude_route_cache["stamp"] = None
    check("with no integration the alias target is unset",
          P.claude_desktop_route_target() is None, None)
    with open(os.path.join(alias_home, "integration-state.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"claude-desktop": {"model": "glm-5.3"}}, fh)
    P._claude_route_cache["stamp"] = None
    check("the review model is read back",
          P.claude_desktop_route_target() == "glm-5.3", None)

    payload = {"model": "claude-sonnet-5"}
    P._apply_claude_desktop_route_alias(payload)
    check("a role id is rewritten to the configured model",
          payload["model"] == "glm-5.3", payload)
    untouched = {"model": "deepseek-v4.1-flash"}
    P._apply_claude_desktop_route_alias(untouched)
    check("an ordinary model id is left alone",
          untouched["model"] == "deepseek-v4.1-flash", untouched)
finally:
    P.ACCOUNTS_DIR = _orig_accounts
    P._claude_route_cache["stamp"] = None

print()
print()
print("[9] Claude clients get the extra model slots and the catalog path")
# Claude Code also fires a small/fast model and a sub-agent model (background
# summaries, sub-agents). Leaving them unset sends those turns to whatever
# built-in default the client has - a model name this gateway does not serve.
plan_cc = A.plan("claude-code", "http://127.0.0.1:8788/v1", "sk-cc",
                 model="deepseek-v4.1-flash", home=home)
payload_cc = json.loads(plan_cc["files"][0]["content"])
env_cc = payload_cc.get("env") or {}
for slot in ("ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
             "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
             "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
    check("%s points at the chosen model" % slot,
          env_cc.get(slot) == "deepseek-v4.1-flash", env_cc.get(slot))
check("the Anthropic base URL keeps no /v1 suffix",
      env_cc.get("ANTHROPIC_BASE_URL") == "http://127.0.0.1:8788", env_cc)
# A relative model_catalog_json is resolved against the Codex CLI's own working
# directory, so from anywhere but $CODEX_HOME it sees no third-party models.
plan_cx = A.plan("codex", "http://127.0.0.1:8788/v1", "sk-cx",
                 model="deepseek-v4.1-flash", home=home)
toml_text = plan_cx["files"][0]["content"]
match = re.search(r'^model_catalog_json = "(.+)"$', toml_text,
                  re.MULTILINE)
check("model_catalog_json is written", match is not None, toml_text[:200])
if match:
    value = match.group(1)
    check("model_catalog_json is an absolute path",
          os.path.isabs(value), value)

print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
