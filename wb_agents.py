"""One-click agent integration for local AI clients.

Detects installed AI clients (Claude Code, Claude Desktop, Codex, OpenCode,
DSH, Crush, ZCode), rewrites their configuration to point at this gateway,
keeps a byte-exact backup of every file it touches, and can restore the
original state with one call. Inspired by EasyCLIProxyAPI's agents mechanism.

It can also restart the clients it configured (Codex, Claude Desktop): those
read their config once at startup, so a rewrite alone does not take effect
until the app is reopened. See ``restart_client``.

The same module is runnable as a standalone configurator, for clients on a
machine that does not run the gateway (the panel's one-click setup only works
from the gateway host itself - a remote browser cannot write another
machine's config files). Run it where the clients are and point it at the
gateway over the network:

    python wb_agents.py list                    # what is installed here
    python wb_agents.py apply --client codex \
        --base-url http://gateway:8788/v1 --key sk-...
    python wb_agents.py restore --client codex

Only the Python standard library is required. The YAML/TOML editing is a
small, deliberately conservative text-level upserter: it never reformats
lines it does not own, so hand-written comments and ordering survive.
"""

import argparse
import base64
import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

PROVIDER_ID = "wb-proxy"
PROVIDER_NAME = "WB Proxy"
CREDENTIAL_REF = "WB_PROXY_API_KEY"

# The Codex desktop app picks its models from a catalog file referenced by
# config.toml's model_catalog_json; without one it hides third-party models
# (the picker is catalog-driven, not a live /v1/models query). We generate it
# in $CODEX_HOME on apply. Schema mirrors what the official app / CC Switch
# write; unknown knobs are filled with conservative defaults.
CODEX_CATALOG_FILE = "wb-proxy-model-catalog.json"
CODEX_BASE_INSTRUCTIONS = (
    "You are Codex, a coding agent. You and the user share the same workspace "
    "and collaborate to achieve the user's goals."
)
CODEX_EFFORT_LABELS = {
    "none": "Disable Thinking",
    "minimal": "Minimal reasoning",
    "low": "Fast responses with lighter reasoning",
    "medium": "Balances speed and reasoning depth for everyday tasks",
    "high": "Greater reasoning depth for complex problems",
    "xhigh": "Extra high reasoning depth for complex problems",
    "max": "Maximum reasoning depth for the hardest problems",
    "ultra": "Ultra reasoning depth",
}

# A config file bigger than this is almost certainly not the hand-edited
# settings file we expect, so refuse rather than risk corrupting user data.
MAX_CONFIG_BYTES = 8 * 1024 * 1024
# How many backups per file are kept; older ones are pruned after each apply.
BACKUP_KEEP = 10

STATE_FILE = "integration-state.json"
BACKUP_DIRNAME = "agent-backups"


class AgentConfigError(Exception):
    """Human-readable failure while probing, applying or restoring a client."""


# ---------------------------------------------------------------------------
# Small file helpers
# ---------------------------------------------------------------------------

def _resolve_home(home=None):
    """Return the home directory base; injectable so tests stay hermetic."""
    if home:
        return os.path.abspath(home)
    return os.path.expanduser("~")


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path):
    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) > MAX_CONFIG_BYTES:
        raise AgentConfigError(
            "refusing to edit %s: file is %d bytes (limit %d)"
            % (path, len(data), MAX_CONFIG_BYTES))
    return data


def _read_text(path):
    """UTF-8 text of an existing file, or "" when the file does not exist."""
    try:
        data = _read_bytes(path)
    except FileNotFoundError:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AgentConfigError("%s is not valid UTF-8: %s" % (path, exc))


# Every file this module writes carries the gateway API key (inline, or via the
# env reference that the sibling credentials file resolves), so a fresh one is
# created owner-only rather than with open()'s 0666 & ~umask - 0644 under the
# usual umask 022. DSH refuses to start when its credentials file is readable
# beyond its owner, and the same 0644 would expose every other client's key.
WRITE_MODE = 0o600


def _target_mode(path):
    """The mode to give `path` on the next write.

    `WRITE_MODE` for a new file, or for one whose current mode is wider than
    owner-only (or does not even let its owner read it back); otherwise the
    file's own mode, so a re-apply preserves a stricter permission the user set
    by hand instead of widening it back - 0600 -> 0600 and 0400 -> 0400, but
    0644 -> 0600.
    """
    try:
        current = os.stat(path).st_mode & 0o777
    except OSError:
        return WRITE_MODE
    if current & 0o077 or not current & 0o400:
        return WRITE_MODE
    return current


def _atomic_write(path, data):
    """tmp + os.replace so a crash cannot leave a half-written config.

    The temp file is created owner-only (see `_target_mode`) rather than with
    the umask's default, and settled on the resolved mode after writing. Any
    failure on the way - including the chmod or the replace - removes the temp
    file, so a retry never trips over a leftover it cannot write to.
    """
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    mode = _target_mode(path)
    try:
        os.unlink(tmp)  # a stale temp from a killed run may not be writable
    except OSError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, WRITE_MODE)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_text(path, text):
    _atomic_write(path, text.encode("utf-8"))


def _atomic_write_bytes(path, data):
    _atomic_write(path, data)


def _prune_empty_parents(path, home=None):
    """Remove now-empty directories left behind by our own config writes.

    Only directories that are empty are removed, and the walk stops at the
    home directory, so a user's own (even empty) tree above the home is never
    touched and nothing that still holds files is ever deleted.
    """
    home = os.path.abspath(home or _resolve_home())
    directory = os.path.dirname(os.path.abspath(path))
    while True:
        if not directory or os.path.abspath(directory) == home:
            return
        if not os.path.abspath(directory).startswith(home + os.sep):
            return
        try:
            os.rmdir(directory)  # fails unless empty
        except OSError:
            return
        directory = os.path.dirname(directory)


def _load_json(path, what):
    """Parse a JSON config file; missing file means an empty document."""
    text = _read_text(path)
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise AgentConfigError("%s is not valid JSON (%s): %s" % (what, path, exc))
    if not isinstance(data, dict):
        raise AgentConfigError("%s must be a JSON object: %s" % (what, path))
    return data


def _dump_json(data):
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def deep_merge(base, patch):
    """Recursively merge patch into base; sibling keys outside patch survive.

    Returns a new dict; the inputs are not mutated.
    """
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return patch
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# Text-level YAML editor (subset; standard library only)
# ---------------------------------------------------------------------------

def _yaml_indent(line):
    return len(line) - len(line.lstrip(" "))


def _yaml_key(line):
    """The mapping key of a block-style `key: ...` line, or None.

    Inline values (anything after the colon) and comments are ignored. Lines
    that are not plain `word: value` mappings (list items, quoted keys) are
    outside the supported subset.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or stripped.startswith("-"):
        return None
    m = re.match(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*)\s*:(?:\s|$)", line.lstrip(" "))
    if not m:
        return None
    return m.group(1)


def _yaml_block_end(lines, start, indent):
    """Index one past the block opened by the line at `start`.

    The block covers every following non-blank, non-comment line indented
    deeper than `indent`.
    """
    idx = start + 1
    while idx < len(lines):
        line = lines[idx]
        if line.strip() and _yaml_indent(line) <= indent:
            break
        idx += 1
    return idx


def _yaml_insert_point(lines, start, end, indent):
    """Last content line inside [start, end), so blank/comment trailers that
    belong to the *next* section stay attached to it."""
    idx = end
    while idx > start and not lines[idx - 1].strip():
        idx -= 1
    return idx if idx > start else end


def _yaml_inline_to_block(lines, idx, key, inline_value):
    """Expand `key: <inline>` into `key:` plus one nested block line.

    Only a plain single-line flow mapping `{k: v, ...}` can be nested under
    the key; anything else is outside the supported subset.
    """
    indent = _yaml_indent(lines[idx])
    value = inline_value.strip()
    if value.startswith("{") and value.endswith("}"):
        inner = value[1:-1].strip()
        out = [" " * indent + key + ":"]
        if inner:
            for part in inner.split(","):
                part = part.strip()
                if part:
                    out.append(" " * (indent + 2) + part)
        lines[idx:idx + 1] = out
        return
    if value == "":
        return
    raise AgentConfigError(
        "cannot nest keys under `%s: %s` - only an inline {..} mapping "
        "can be expanded" % (key, inline_value))


def yaml_upsert_inline(text, key_path, value):
    """Upsert a nested mapping key with an inline-flow (JSON-compatible) value.

    `key_path` is a dotted path (e.g. "llm-pi-ai.providers.wb-proxy"); the
    leaf is written as `wb-proxy: <json value>` at the right indentation.
    Lines outside the touched block are preserved byte for byte. When a key
    of the path is missing it is created; when a parent key currently holds
    an inline `{..}` mapping the mapping is expanded into a block first.
    Structures outside the supported subset raise AgentConfigError.
    """
    keys = [part.strip() for part in str(key_path).split(".") if part.strip()]
    if not keys:
        raise AgentConfigError("yaml_upsert_inline: empty key path")
    lines = text.split("\n")
    lines = _yaml_upsert_at(lines, 0, len(lines), 0, keys,
                            json.dumps(value, ensure_ascii=False))
    return "\n".join(lines)


def _yaml_upsert_at(lines, start, end, indent, keys, value_json):
    """Recursive block scanner for yaml_upsert_inline; returns new lines."""
    key = keys[0]
    idx = start
    while idx < end:
        line = lines[idx]
        if line.strip() and not line.strip().startswith("#") \
                and _yaml_indent(line) == indent and _yaml_key(line) == key:
            after = line.split(":", 1)[1].strip()
            child_end = _yaml_block_end(lines, idx, indent)
            if len(keys) == 1:
                # Replace this leaf line, keeping its indentation.
                lines[idx] = " " * indent + key + ": " + value_json
                return lines
            if after:
                # Parent currently holds an inline value: expand `{..}` into
                # a block so the child has somewhere to live.
                _yaml_inline_to_block(lines, idx, key, after)
                child_end = _yaml_block_end(lines, idx, indent)
                lines = _yaml_upsert_at(lines, idx + 1, child_end,
                                        indent + 2, keys[1:], value_json)
                return lines
            lines = _yaml_upsert_at(lines, idx + 1, child_end,
                                    indent + 2, keys[1:], value_json)
            return lines
        idx += 1
    # Key not found in this block: append it at the block's end.
    insert = _yaml_insert_point(lines, start, end, indent)
    if len(keys) == 1:
        new_lines = [" " * indent + key + ": " + value_json]
    else:
        new_lines = [" " * indent + keys[0] + ":"]
        new_lines.extend(
            _yaml_upsert_at([], 0, 0, indent + 2, keys[1:], value_json))
    lines[insert:insert] = new_lines
    return lines


def yaml_refs_upsert(text, key, value):
    """Upsert `key: value` inside the top-level `refs:` block.

    When the document has no `refs:` block yet, one is appended at the end.
    A `refs:` line that already holds an inline value (e.g. `refs: {}`) is
    outside the supported subset and raises AgentConfigError instead of
    being silently mangled.
    """
    lines = text.split("\n")
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if line.strip() and not line.strip().startswith("#") \
                and _yaml_indent(line) == 0 and _yaml_key(line) == "refs":
            after = line.split(":", 1)[1].strip()
            if after:
                raise AgentConfigError(
                    "cannot upsert into `refs: %s` - only a block-style "
                    "refs: mapping is supported" % after)
            end = _yaml_block_end(lines, idx, 0)
            j = idx + 1
            while j < end:
                if lines[j].strip() and _yaml_indent(lines[j]) == 2 \
                        and _yaml_key(lines[j]) == key:
                    lines[j] = "  %s: %s" % (key, value)
                    return "\n".join(lines)
                j += 1
            insert = _yaml_insert_point(lines, idx + 1, end, 2)
            lines[insert:insert] = ["  %s: %s" % (key, value)]
            return "\n".join(lines)
        idx += 1
    # No refs block yet.
    while lines and not lines[-1].strip():
        lines.pop()
    lines.append("refs:")
    lines.append("  %s: %s" % (key, value))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Text-level TOML editor (subset; standard library only)
# ---------------------------------------------------------------------------

_TOML_TABLE_RE = re.compile(r"^\s*\[([A-Za-z0-9_.-]+)\]\s*(?:#.*)?$")
_TOML_KEY_RE = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=")


def _toml_value(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        raise AgentConfigError("TOML has no null value")
    return json.dumps(value, ensure_ascii=False)


def toml_upsert_top_key(text, key, value):
    """Upsert a top-level TOML key (it must live before the first [table]).

    An existing assignment is replaced in place; a missing one is inserted
    after the last existing top-level assignment, so table bodies are never
    disturbed.
    """
    rendered = "%s = %s" % (key, _toml_value(value))
    lines = text.split("\n")
    idx = 0
    last_top = -1
    while idx < len(lines):
        line = lines[idx]
        if _TOML_TABLE_RE.match(line):
            break
        m = _TOML_KEY_RE.match(line)
        if m:
            last_top = idx
            if m.group(1) == key:
                lines[idx] = rendered
                return "\n".join(lines)
        idx += 1
    lines.insert(last_top + 1, rendered)
    return "\n".join(lines)


def toml_upsert_table(text, table, mapping):
    """Upsert several keys inside [table]; append the table when missing."""
    lines = text.split("\n")
    idx = 0
    while idx < len(lines):
        m = _TOML_TABLE_RE.match(lines[idx])
        if m and m.group(1) == table:
            end = idx + 1
            while end < len(lines) and not _TOML_TABLE_RE.match(lines[end]):
                end += 1
            return "\n".join(
                _toml_upsert_in_range(lines, idx + 1, end, mapping))
        idx += 1
    # Table not present: append it whole at the end of the file.
    while lines and not lines[-1].strip():
        lines.pop()
    if lines:
        lines.append("")
    lines.append("[%s]" % table)
    for key in mapping:
        lines.append("%s = %s" % (key, _toml_value(mapping[key])))
    return "\n".join(lines) + "\n"


def _toml_upsert_in_range(lines, start, end, mapping):
    remaining = dict(mapping)
    last_content = start
    idx = start
    while idx < end:
        stripped = lines[idx].strip()
        if stripped and not stripped.startswith("#"):
            last_content = idx + 1
            m = _TOML_KEY_RE.match(lines[idx])
            if m and m.group(1) in remaining:
                key = m.group(1)
                lines[idx] = "%s = %s" % (key, _toml_value(remaining.pop(key)))
        idx += 1
    insert = last_content
    for key, value in remaining.items():
        lines.insert(insert, "%s = %s" % (key, _toml_value(value)))
        insert += 1
    return lines


# ---------------------------------------------------------------------------
# Text-level dotenv editor
# ---------------------------------------------------------------------------

def env_upsert(text, mapping):
    """Upsert KEY=VALUE lines; comments and unrelated lines are preserved."""
    lines = text.split("\n")
    remaining = dict(mapping)
    for idx, line in enumerate(lines):
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=", line)
        if m and m.group(1) in remaining:
            key = m.group(1)
            lines[idx] = "%s=%s" % (key, remaining.pop(key))
    if remaining:
        while lines and not lines[-1].strip():
            lines.pop()
        for key, value in remaining.items():
            lines.append("%s=%s" % (key, value))
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Backup + state bookkeeping
# ---------------------------------------------------------------------------

def _state_path(accounts_dir):
    return os.path.join(accounts_dir, STATE_FILE)


def _load_state(accounts_dir):
    try:
        with open(_state_path(accounts_dir), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return {}


def _save_state(accounts_dir, state):
    os.makedirs(accounts_dir, exist_ok=True)
    _atomic_write_text(_state_path(accounts_dir), _dump_json(state))


def _backup_dir(accounts_dir, client_id):
    return os.path.join(accounts_dir, BACKUP_DIRNAME, client_id)


def _backup_file(accounts_dir, client_id, path, original):
    """Store the original bytes of `path`; returns the backup file path."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = "%s-%s" % (stamp, os.path.basename(path) or "config")
    directory = _backup_dir(accounts_dir, client_id)
    os.makedirs(directory, exist_ok=True)
    target = os.path.join(directory, name)
    seq = 1
    while os.path.exists(target):
        target = os.path.join(directory, "%s-%d" % (name, seq))
        seq += 1
    _atomic_write_bytes(target, original)
    _prune_backups(directory)
    return target


def _prune_backups(directory, keep=BACKUP_KEEP):
    """Keep only the newest `keep` backups of each basename."""
    by_name = {}
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return
    for name in entries:
        base = re.sub(r"^\d{8}-\d{6}-", "", name)
        by_name.setdefault(base, []).append(name)
    for base, names in by_name.items():
        for stale in names[:-keep] if len(names) > keep else []:
            try:
                os.remove(os.path.join(directory, stale))
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Client registry
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Restarting a client whose config we just rewrote
# ---------------------------------------------------------------------------
# Codex (desktop app) and Claude Desktop read their configuration once, when
# they start. After one-click setup rewrites it, a running instance keeps the
# old file in memory until the user quits and reopens the app by hand - which
# is exactly the step people forget, and the reason "I configured it and
# nothing changed". These helpers find the client's own processes so the panel
# can offer to restart it for them.
#
# Only processes we can positively identify as *this* client are touched. The
# match is by executable path, not by a bare image name: the Codex desktop app
# ships as ChatGPT.exe inside the OpenAI.Codex_<version> package, and there is
# an unrelated ChatGPT desktop app with the same image name.

PROCESS_QUERY_TIMEOUT = 60

#: Kill switch for the irreversible part. The gateway process is the one that
#: should ever restart a client; every test suite sets this before importing
#: the module so an unstubbed call can only ever report "blocked".
_RESTART_DISABLED = os.environ.get(
    "WB_AGENTS_NO_RESTART", "").strip().lower() in ("1", "true", "yes", "on")


def disable_restarts():
    """Refuse every future restart for this process (test suites only)."""
    global _RESTART_DISABLED
    _RESTART_DISABLED = True


def enable_restarts():
    """Undo `disable_restarts` - only a suite that has replaced the process
    table and the launcher with fakes should call this."""
    global _RESTART_DISABLED
    _RESTART_DISABLED = False

# PowerShell is asked to emit the process table as JSON; -EncodedCommand keeps
# the quoting out of the picture entirely. Get-CimInstance is present on every
# supported Windows (unlike wmic, which Microsoft is retiring).
_PS_PROCESS_SNAPSHOT = (
    "$ErrorActionPreference='Stop';"
    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
    "$p=Get-CimInstance Win32_Process|"
    "Select-Object ProcessId,ParentProcessId,Name,ExecutablePath;"
    "ConvertTo-Json -InputObject @($p) -Compress -Depth 3"
)


def _run_powershell(script, timeout=PROCESS_QUERY_TIMEOUT):
    """Run a PowerShell snippet, returning stdout as text."""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise AgentConfigError(
            "powershell failed: %s"
            % proc.stderr.decode("utf-8", "replace").strip()[:200])
    return proc.stdout.decode("utf-8", "replace")


def process_snapshot():
    """Every process on this machine as {pid, ppid, name, exe} records.

    Raises AgentConfigError when the host cannot answer; callers treat that as
    "unknown", never as "nothing is running".
    """
    if os.name == "nt":
        raw = _run_powershell(_PS_PROCESS_SNAPSHOT).strip()
        if not raw:
            return []
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        out = []
        for item in data:
            if not isinstance(item, dict):
                continue
            out.append({
                "pid": int(item.get("ProcessId") or 0),
                "ppid": int(item.get("ParentProcessId") or 0),
                "name": str(item.get("Name") or ""),
                "exe": str(item.get("ExecutablePath") or ""),
            })
        return out
    # POSIX: ps is present on every host the module runs on.
    proc = subprocess.run(["ps", "-eo", "pid=,ppid=,comm=,args="],
                          capture_output=True, timeout=PROCESS_QUERY_TIMEOUT)
    out = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split(None, 3)
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        out.append({
            "pid": int(parts[0]),
            "ppid": int(parts[1]) if parts[1].isdigit() else 0,
            "name": parts[2],
            "exe": parts[3] if len(parts) > 3 else parts[2],
        })
    return out


def _norm_path(value):
    return str(value or "").replace("/", "\\").lower()


def _is_codex_process(proc):
    """True for the Codex desktop app and the app-server it spawns."""
    name = str(proc.get("name") or "").lower()
    exe = _norm_path(proc.get("exe"))
    # The desktop app (and every renderer it forks) lives in the Codex package.
    if name == "chatgpt.exe" and "\\windowsapps\\openai.codex_" in exe:
        return True
    # The app-server / CLI binary the desktop app starts as a child.
    if name == "codex.exe" and "\\openai\\codex\\bin\\" in exe:
        return True
    return False


def _is_claude_desktop_process(proc):
    name = str(proc.get("name") or "").lower()
    exe = _norm_path(proc.get("exe"))
    if not name.startswith("claude"):
        return False
    return ("\\anthropicclaude\\" in exe or "\\claude\\" in exe
            or "\\claude_" in exe)


def client_processes(client_id, home=None, snapshot=None):
    """Running processes belonging to `client_id` (empty when it is not up)."""
    client = _client(client_id)
    matcher = client.get("processes")
    if matcher is None:
        return []
    procs = process_snapshot() if snapshot is None else snapshot
    return [proc for proc in procs if matcher(proc)]


def _descendants(procs, roots):
    """Every process reachable from `roots` through parent links."""
    wanted = {pid for pid in roots}
    changed = True
    while changed:
        changed = False
        for proc in procs:
            if proc["ppid"] in wanted and proc["pid"] not in wanted:
                wanted.add(proc["pid"])
                changed = True
    return wanted


def _windows_package_launch_id(exe):
    """The Start-menu id (AUMID) for an app running out of a WindowsApps package.

    `...\\WindowsApps\\OpenAI.Codex_26.1007.9446.1_x64__2p2nqsd0c76g0\\app\\ChatGPT.exe`
    -> `OpenAI.Codex_2p2nqsd0c76g0!App`. The Application id is read from the
    package manifest when it is readable and assumed to be `App` otherwise.
    """
    raw = str(exe or "").replace("/", "\\")
    marker = "\\WindowsApps\\"
    at = raw.lower().find(marker.lower())
    if at < 0:
        return None
    # Keep the original casing: the package name is part of the id.
    pkg_dir = raw[at + len(marker):].split("\\")[0]
    if "__" not in pkg_dir or "_" not in pkg_dir:
        return None
    name = pkg_dir.split("_", 1)[0]
    publisher_hash = pkg_dir.rsplit("__", 1)[1]
    family = "%s_%s" % (name, publisher_hash)
    app_id = "App"
    try:
        root = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                            "WindowsApps", pkg_dir)
        with open(os.path.join(root, "AppxManifest.xml"), encoding="utf-8") as fh:
            manifest = fh.read()
        found = re.search(r'<Application\b[^>]*\bId="([^"]+)"', manifest)
        if found:
            app_id = found.group(1)
    except Exception:
        pass
    return "%s!%s" % (family, app_id)


def _terminate(procs, pids):
    """Close client roots politely first, then force any surviving tree.

    A desktop app may have unsaved state while it handles shutdown. Sending a
    normal window-close first gives it a chance to flush that state; only a
    stuck process is force-killed. Returns PIDs gone afterwards.
    """
    pids = {int(pid) for pid in pids}
    by_pid = {proc["pid"]: proc for proc in procs if proc.get("pid") in pids}
    roots = sorted(pid for pid, proc in by_pid.items()
                   if proc.get("ppid") not in pids)

    if os.name == "nt":
        if roots:
            # Use the GUI process CloseMainWindow API rather than `taskkill`
            # first, so Codex / Claude Desktop can finish saving its current
            # conversation. This is bounded: PowerShell only requests the close.
            root_csv = ",".join(str(pid) for pid in roots)
            script = (
                "$ids=@(%s); foreach($id in $ids){"
                "try{$p=Get-Process -Id $id -ErrorAction Stop;"
                "if($p.MainWindowHandle -ne 0){[void]$p.CloseMainWindow()}}catch{}}"
            ) % root_csv
            try:
                _run_powershell(script, timeout=10)
            except Exception:
                pass

        deadline = time.time() + 8.0
        alive = set(pids)
        while time.time() < deadline and alive:
            try:
                observed = {proc["pid"] for proc in process_snapshot()}
                alive = pids & observed
            except Exception:
                break
            if alive:
                time.sleep(0.25)

        # Force only surviving roots; /T takes care of their descendants.
        if alive:
            alive_roots = [pid for pid in sorted(alive)
                           if by_pid.get(pid, {}).get("ppid") not in alive]
            for pid in alive_roots or sorted(alive):
                try:
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                   capture_output=True, timeout=30)
                except Exception:
                    pass
    else:
        # POSIX clients should receive TERM first and only then KILL after the
        # same short grace period.
        for pid in roots or sorted(pids):
            try:
                os.kill(pid, 15)
            except Exception:
                pass
        time.sleep(min(8.0, max(0.0, 0.5)))
        try:
            alive = pids & {proc["pid"] for proc in process_snapshot()}
        except Exception:
            alive = set()
        alive_roots = [pid for pid in sorted(alive)
                       if by_pid.get(pid, {}).get("ppid") not in alive]
        for pid in alive_roots:
            try:
                os.kill(pid, 9)
            except Exception:
                pass

    try:
        alive = pids & {proc["pid"] for proc in process_snapshot()}
    except Exception:
        alive = pids
    return sorted(pids - alive)


def restart_client(client_id, home=None, wait=2.0):
    """Quit and relaunch `client_id`, so it re-reads the config we just wrote.

    Returns a report; never raises for "it was not running" - that is a normal
    outcome and the caller shows it as "nothing to restart".
    """
    client = _client(client_id)
    launcher = client.get("relaunch")
    if client.get("processes") is None or launcher is None:
        return {"supported": False, "reason": "%s cannot be restarted by the "
                "gateway" % client["label"], "stopped": [], "started": False}
    # Killing processes is the one irreversible thing this module can do, so a
    # test run must never reach it by accident: the suites drive the real
    # handler, and one that forgot to stub this out would kill the operator's
    # Codex or Claude Desktop - and then rewrite its config.
    if _RESTART_DISABLED:
        return {"supported": True, "running": None, "stopped": [],
                "started": False, "blocked": True,
                "reason": "restarts are disabled by WB_AGENTS_NO_RESTART"}
    try:
        procs = process_snapshot()
    except Exception as exc:
        return {"supported": True, "running": None, "stopped": [],
                "started": False, "error": "cannot read the process table: %s" % exc}
    matches = [proc for proc in procs if client["processes"](proc)]
    if not matches:
        return {"supported": True, "running": False, "stopped": [],
                "started": False, "reason": "%s is not running" % client["label"]}
    # Kill the tree rooted at the outermost matched process so the app's own
    # children (renderers, the app-server) cannot keep the old config alive.
    roots = [proc["pid"] for proc in matches
             if proc["ppid"] not in {other["pid"] for other in matches}]
    targets = _descendants(procs, roots or [proc["pid"] for proc in matches])
    stopped = _terminate(procs, targets)
    if wait:
        time.sleep(wait)
    started = launcher(matches)
    return {
        "supported": True,
        "running": True,
        "stopped": sorted(stopped),
        "started": bool(started),
        "launch": started.get("command") if isinstance(started, dict) else None,
        "error": started.get("error") if isinstance(started, dict) else None,
    }


def _launch_codex(matches):
    """Relaunch the Codex desktop app the same way the Start menu does."""
    for proc in matches:
        if str(proc.get("name") or "").lower() != "chatgpt.exe":
            continue
        aumid = _windows_package_launch_id(proc.get("exe"))
        if not aumid:
            continue
        command = "shell:AppsFolder\\%s" % aumid
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer.exe", command])
            else:
                return {"error": "only Windows can relaunch the Codex app"}
        except Exception as exc:
            return {"error": str(exc)}
        return {"command": command}
    return {"error": "the Codex app executable was not found in this process "
                     "list; reopen it from the Start menu"}


def _launch_claude_desktop(matches):
    exe = next((proc["exe"] for proc in matches if proc.get("exe")), None)
    if not exe or not os.path.exists(exe):
        return {"error": "the Claude Desktop executable was not found"}
    try:
        subprocess.Popen([exe])
    except Exception as exc:
        return {"error": str(exc)}
    return {"command": exe}


def _claude_settings_path(home):
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return os.path.join(override, "settings.json")
    return os.path.join(home, ".claude", "settings.json")


def _apply_claude_code(paths, ctx):
    path = paths["settings"]
    data = _load_json(path, "claude settings")
    env = dict(data.get("env") or {})
    # Claude Code appends /v1 itself when it needs it; keep the bare root.
    env["ANTHROPIC_BASE_URL"] = re.sub(r"/v1/?$", "", ctx["base_url"])
    env["ANTHROPIC_AUTH_TOKEN"] = ctx["api_key"]
    if ctx.get("model"):
        # Every slot Claude Code picks up. ANTHROPIC_MODEL is the main one, but
        # the app also runs a small/fast model and a sub-agent model (background
        # summaries, /compact, sub-agents); leave them unset and those turns go
        # to whatever the built-in default is - which for a third-party gateway
        # is a model name it does not serve. CC Switch sets all of these too.
        for name in ("ANTHROPIC_MODEL",
                     "ANTHROPIC_SMALL_FAST_MODEL",
                     "ANTHROPIC_DEFAULT_SONNET_MODEL",
                     "ANTHROPIC_DEFAULT_OPUS_MODEL",
                     "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                     "CLAUDE_CODE_SUBAGENT_MODEL"):
            env[name] = ctx["model"]
    data["env"] = env
    return {path: _dump_json(data)}


def _codex_home(home):
    return os.environ.get("CODEX_HOME") or os.path.join(home, ".codex")


def _build_codex_model_catalog(models):
    """Render the gateway model list as a Codex desktop-app model catalog.

    The desktop app reads this JSON (via config.toml's model_catalog_json) to
    populate its picker and reasoning controls. Schema mirrors the one the
    official app / CC Switch write; fields we do not know are filled with
    conservative defaults so a missing knob never drops a model.
    """
    entries = []
    for index, entry in enumerate(models or []):
        if not isinstance(entry, dict):
            continue
        mid = str(entry.get("id") or "").strip()
        if not mid:
            continue
        context = entry.get("context_window") or entry.get("context_length")
        try:
            context = int(context or 0)
        except (TypeError, ValueError):
            context = 0
        if context <= 0:
            context = 1000000
        modalities = entry.get("input_modalities")
        if not isinstance(modalities, list) or not modalities:
            modalities = ["text"]
        modalities = [str(m) for m in modalities if str(m).strip()]
        if not modalities:
            modalities = ["text"]
        efforts = entry.get("reasoning_efforts")
        if not isinstance(efforts, list):
            efforts = []
        levels = []
        for effort in efforts:
            effort = str(effort or "").strip()
            if not effort:
                continue
            levels.append({
                "description": CODEX_EFFORT_LABELS.get(
                    effort, effort.replace("_", " ").title()),
                "effort": effort,
            })
        reasoning = bool(entry.get("reasoning")
                         or entry.get("supports_reasoning"))
        if not levels and reasoning:
            levels = [
                {"description": CODEX_EFFORT_LABELS["none"], "effort": "none"},
                {"description": CODEX_EFFORT_LABELS["high"], "effort": "high"},
            ]
        default_effort = str(
            entry.get("reasoning_default_effort")
            or entry.get("reasoning_effort")
            or "high").strip()
        advertised = {level["effort"] for level in levels}
        if default_effort not in advertised and levels:
            default_effort = levels[-1]["effort"]
        display = str(entry.get("name") or entry.get("description") or mid)
        max_out = entry.get("max_output_tokens") or entry.get("max_output")
        try:
            max_out = int(max_out or 0)
        except (TypeError, ValueError):
            max_out = 0
        if max_out <= 0:
            max_out = 64000
        supports_tools = bool(entry.get("supports_tool_calls", True))
        supports_vision = bool(
            entry.get("supports_vision")
            or "image" in modalities
            or entry.get("vision"))
        auto_compact = max(1, int(context * 0.9))
        web_search = "text_and_image" if supports_vision else "text"
        entries.append({
            "additional_speed_tiers": [],
            "apply_patch_tool_type": "freeform",
            "auto_compact_token_limit": auto_compact,
            "availability_nux": None,
            "base_instructions": CODEX_BASE_INSTRUCTIONS,
            "context_window": context,
            "default_reasoning_level": default_effort if levels else "none",
            "default_reasoning_summary": "none",
            "default_verbosity": "low",
            "description": display,
            "display_name": display,
            "effective_context_window_percent": 95,
            "experimental_supported_tools": [],
            "include_apps_usage_instructions": True,
            "include_plugin_usage_instructions": True,
            "include_skills_usage_instructions": True,
            "input_modalities": modalities,
            "max_context_window": context,
            "max_output_tokens": max_out,
            "model_messages": {
                "instructions_template": CODEX_BASE_INSTRUCTIONS,
            },
            "multi_agent_version": "v2",
            "node_repl_auto_review_required": False,
            "node_repl_disabled": False,
            "prefer_websockets": False,
            "priority": 1000 + index,
            "service_tiers": [],
            "shell_type": "shell_command",
            "slug": mid,
            "support_verbosity": False,
            "supported_in_api": True,
            "supported_reasoning_levels": levels,
            "supports_image_detail_original": supports_vision,
            "supports_parallel_tool_calls": supports_tools,
            "supports_reasoning_summaries": bool(levels),
            "supports_search_tool": False,
            "tool_mode": None,
            "truncation_policy": {"limit": 10000, "mode": "bytes"},
            "upgrade": None,
            "use_responses_lite": False,
            "visibility": "list",
            "web_search_tool_type": web_search,
        })
    return {"models": entries}


def _apply_codex(paths, ctx):
    config_path = paths["config"]
    text = _read_text(config_path)
    text = toml_upsert_top_key(text, "model_provider", PROVIDER_ID)
    if ctx.get("model"):
        text = toml_upsert_top_key(text, "model", ctx["model"])
    # The desktop app needs a catalog to surface the gateway's models, and we
    # keep response storage off so third-party traffic is not written upstream.
    # The path is written absolute on purpose: the Codex CLI resolves a
    # relative one against its own working directory, so `codex` started from
    # anywhere but the home directory would silently see no third-party models.
    # CC Switch canonicalises this key the same way.
    text = toml_upsert_top_key(text, "model_catalog_json", paths["catalog"])
    text = toml_upsert_top_key(text, "disable_response_storage", True)
    # The desktop app reads the API key inline from experimental_bearer_token.
    # Do NOT also set env_key: the app then treats env_key as the chosen auth
    # source and drops experimental_bearer_token, so every request leaves with
    # no bearer token and Codex loops between "optimizing conversation" and
    # reconnecting. auth.json is still written below for the Codex CLI.
    # requires_openai_auth off stops Codex from falling back to its OpenAI
    # login for this third-party route.
    text = toml_upsert_table(text, "model_providers." + PROVIDER_ID, {
        "name": PROVIDER_NAME,
        "base_url": ctx["base_url_v1"],
        "wire_api": "responses",
        "experimental_bearer_token": ctx["api_key"],
        "requires_openai_auth": False,
    })
    auth_path = paths["auth"]
    auth = _load_json(auth_path, "codex auth")
    auth = deep_merge(auth, {"OPENAI_API_KEY": ctx["api_key"]})
    catalog_path = paths["catalog"]
    catalog = _dump_json(_build_codex_model_catalog(ctx.get("models")))
    return {config_path: text, auth_path: _dump_json(auth),
            catalog_path: catalog}


def _apply_opencode(paths, ctx):
    path = paths["config"]
    data = _load_json(path, "opencode config")
    if "$schema" not in data:
        data["$schema"] = "https://opencode.ai/config.json"
    models = {}
    for entry in ctx.get("models") or []:
        mid = entry.get("id")
        if mid:
            models[mid] = {}
    provider = {
        "npm": "@ai-sdk/openai-compatible",
        "name": PROVIDER_NAME,
        "options": {
            "baseURL": ctx["base_url_v1"],
            "apiKey": ctx["api_key"],
        },
    }
    if models:
        provider["models"] = models
    data = deep_merge(data, {"provider": {PROVIDER_ID: provider}})
    if ctx.get("model"):
        data["model"] = "%s/%s" % (PROVIDER_ID, ctx["model"])
    return {path: _dump_json(data)}


def dsh_reasoning_levels(raw):
    """{档位: 线上拼写} for DSH, from the gateway's `reasoning_efforts` list.

    DSH 的模型条目用 `reasoningEfforts` 声明可选思考档位（键=选择器里的档位，
    值=请求里实际发的拼写）。本路由不在它的内置目录里，所以不声明就没有选择
    器——`/v1/models` 里的 `reasoning_efforts` 它不读，它只读 id / name /
    contextWindow / maxTokens。网关宣告的档位名就是上游认的拼写，一一照抄即可；
    固定档位的模型（网关只给 reasoning_fixed_effort）没有列表，就不声明：给了
    选择器也不会改变上游实际跑的档位。
    """
    if isinstance(raw, dict):
        pairs = list(raw.items())
    elif isinstance(raw, (list, tuple)):
        pairs = [(lvl, lvl) for lvl in raw]
    else:
        return None
    levels = {}
    for level, wire in pairs:
        name = str(level or "").strip()
        if not name:
            continue
        levels[name] = str(wire or "").strip() or name
    return levels or None


def dsh_default_model(text, provider, model):
    """The `agent-default-model` entry to write, keeping keys we do not own.

    yaml_upsert_inline() replaces that leaf wholesale, so writing a bare
    {provider, model} silently drops whatever the user set beside it - a
    reasoningEffort, for instance, which is how a default thinking level gets
    lost on a re-apply. Read the current inline mapping and merge the route
    over it; a block-style or unparsable value falls back to the bare pair
    rather than guessing.
    """
    out = {"provider": provider, "model": model}
    for line in str(text or "").split("\n"):
        stripped = line.strip()
        if not stripped.startswith("agent-default-model:"):
            continue
        raw = stripped.split(":", 1)[1].strip()
        if raw.startswith("{"):
            try:
                current = json.loads(raw)
            except ValueError:
                current = None
            if isinstance(current, dict):
                out = dict(current)
        break
    out["provider"] = provider
    out["model"] = model
    return out


def _apply_dsh(paths, ctx):
    settings_path = paths["settings"]
    text = _read_text(settings_path)
    models = []
    for entry in ctx.get("models") or []:
        mid = entry.get("id")
        if not mid:
            continue
        item = {"id": mid}
        if entry.get("context_window"):
            item["contextWindow"] = entry["context_window"]
        if entry.get("max_output"):
            try:
                item["maxTokens"] = int(entry["max_output"])
            except (TypeError, ValueError):
                pass
        levels = dsh_reasoning_levels(entry.get("reasoning_efforts"))
        if levels:
            item["reasoningEfforts"] = levels
        if entry.get("supports_vision") or entry.get("supportsVision"):
            item["supportsVision"] = True
        models.append(item)
    provider = {
        "displayName": PROVIDER_NAME,
        "apiKeyEnv": CREDENTIAL_REF,
        "api": "openai-completions",
        "baseURL": ctx["base_url_v1"],
        # wb-proxy 不在 DSH 的内置目录里，推理字段的线上形状必须自己说明：
        # 走标准 reasoning_effort，不发 thinking 块——DeepSeek 系需要的那份
        # thinking / reasoning_content 由网关补（wb_proxy.build_upstream_body）。
        "compat": {"thinkingFormat": "openai", "supportsReasoningEffort": True},
    }
    if models:
        provider["models"] = models
    text = yaml_upsert_inline(
        text, "llm-pi-ai.providers.%s" % PROVIDER_ID, provider)
    if ctx.get("model"):
        text = yaml_upsert_inline(
            text, "agent-default-model",
            dsh_default_model(text, PROVIDER_ID, ctx["model"]))
    credentials_path = paths["credentials"]
    cred_text = _read_text(credentials_path)
    cred_text = yaml_refs_upsert(cred_text, CREDENTIAL_REF, ctx["api_key"])
    return {settings_path: text, credentials_path: cred_text}


def _apply_crush(paths, ctx):
    path = paths["config"]
    data = _load_json(path, "crush config")
    models = []
    for entry in ctx.get("models") or []:
        mid = entry.get("id")
        if not mid:
            continue
        item = {"id": mid, "name": mid}
        if entry.get("context_window"):
            item["context_window"] = entry["context_window"]
        if entry.get("max_output"):
            item["default_max_tokens"] = entry["max_output"]
        models.append(item)
    provider = {
        "name": PROVIDER_NAME,
        "type": "openai",
        "base_url": ctx["base_url_v1"],
        "api_key": ctx["api_key"],
        "models": models,
    }
    data = deep_merge(data, {"providers": {PROVIDER_ID: provider}})
    return {path: _dump_json(data)}


def _apply_zcode(paths, ctx):
    """Add or update a wb-proxy provider rule in ZCode's provider config.

    The rule is addressed by a stable providerId, so re-applying updates that
    one entry instead of stacking new ones, and everything else in the
    document - other providers, their order, the model rules - is merged
    around untouched. Note the file belongs to the ZCode app and is rewritten
    by it while running; configure with ZCode closed (the panel card and the
    CLI both say so) or the app's next save can drop the new entry.
    """
    path = paths["config"]
    data = _load_json(path, "zcode provider config")
    if not data:
        # No ZCode profile on this machine yet: start from the wrapper shape a
        # live 3.x profile writes (schemaVersion + config + the two rule
        # holders), so a fresh install can be configured without running the
        # app first.
        data = {"schemaVersion": 1,
                "config": {"providerOrder": [],
                           "providerConfigRules": {"providerRules": []}}}
    config = data.get("config")
    if not isinstance(config, dict):
        raise AgentConfigError(
            "zcode provider config has no config object: %s" % path)
    holder = config.get("providerConfigRules")
    if not isinstance(holder, dict):
        raise AgentConfigError(
            "zcode provider config has no providerConfigRules: %s" % path)
    rules = holder.get("providerRules")
    if not isinstance(rules, list):
        raise AgentConfigError(
            "zcode providerConfigRules has no providerRules list: %s" % path)

    model_ids = []
    for entry in ctx.get("models") or []:
        mid = entry.get("id") if isinstance(entry, dict) else entry
        mid = str(mid or "").strip()
        if mid and mid not in model_ids:
            model_ids.append(mid)

    rule = {
        "providerId": PROVIDER_ID,
        "providerName": PROVIDER_NAME,
        "config": {
            "group": "standard-personal",
            "access": {"type": "api-key", "apiKey": ctx["api_key"]},
            "api": {"type": "openai-chat-completions",
                    "baseUrl": ctx["base_url_v1"]},
        },
    }
    if model_ids:
        rule["config"]["personalModelIds"] = model_ids
        rule["config"]["modelOrder"] = list(model_ids)

    replaced = False
    for index, existing in enumerate(rules):
        if isinstance(existing, dict) and existing.get("providerId") == PROVIDER_ID:
            rules[index] = deep_merge(existing, rule)
            replaced = True
            break
    if not replaced:
        rules.append(rule)

    order = config.get("providerOrder")
    if not isinstance(order, list):
        order = []
        config["providerOrder"] = order
    if PROVIDER_ID not in order:
        order.append(PROVIDER_ID)
    return {path: _dump_json(data)}


# ---------------------------------------------------------------------------
# Claude Desktop (the desktop app, not Claude Code)
# ---------------------------------------------------------------------------
# Claude Desktop is a different program from Claude Code: it does not read
# ANTHROPIC_BASE_URL out of settings.json, it takes its custom inference
# gateway from a *profile* under its own `configLibrary` directory. The layout
# below mirrors what CC Switch writes, which is the shape Claude Desktop
# itself accepts:
#
#   <Claude>\claude_desktop_config.json      {"deploymentMode": "3p"}
#   <Claude-3p>\claude_desktop_config.json   {"deploymentMode": "3p"}
#   <Claude-3p>\configLibrary\<profile>.json the gateway profile
#   <Claude-3p>\configLibrary\_meta.json     entries + appliedId
#
# Claude Desktop validates the model names inside that profile against a role
# whitelist (sonnet / opus / haiku / fable), and a name it does not recognise
# makes it reject the whole group. So the profile may only advertise
# `claude-<role>-<n>` ids; the gateway maps those back to a real model (see
# the claude-route alias in wb_proxy). Writing the raw upstream model id here
# would break the picker.

CLAUDE_DESKTOP_PROFILE_ID = "00000000-0000-4000-8000-000000157210"
CLAUDE_DESKTOP_PROFILE_NAME = "WB Proxy"
CLAUDE_DESKTOP_CONFIG_FILE = "claude_desktop_config.json"
CLAUDE_DESKTOP_LIBRARY_DIR = "configLibrary"

#: The role ids Claude Desktop accepts, in picker order. The `wb` suffix is
#: deliberate: these must not collide with Anthropic's own model names, or a
#: user cannot tell whether a picker entry is the gateway's or the official
#: model - and every entry here maps to the same upstream model anyway.
CLAUDE_DESKTOP_ROUTES = (
    ("claude-sonnet-wb", "Sonnet"),
    ("claude-opus-wb", "Opus"),
    ("claude-haiku-wb", "Haiku"),
    ("claude-fable-wb", "Fable"),
)


def _claude_desktop_dirs(home):
    """(<Claude dir>, <Claude-3p dir>) for this platform."""
    override = os.environ.get("WB_CLAUDE_DESKTOP_DIR")
    override3p = os.environ.get("WB_CLAUDE_DESKTOP_3P_DIR")
    if override and override3p:
        return override, override3p
    if os.name == "nt":
        # Follow the injected home (as every other client here does) rather
        # than %LOCALAPPDATA%: for the real user they are the same directory,
        # and a caller that points `home` somewhere else - a test, or the
        # standalone configurator - gets a hermetic answer.
        base = os.path.join(home, "AppData", "Local")
        normal = os.path.join(base, "Claude")
        threep = os.path.join(base, "Claude-3p")
        # Both installs read the same local folders. The Microsoft Store (MSIX)
        # build normally redirects its data into the package sandbox, but only
        # when filesystem virtualization is on; a Claude that has already run
        # with virtualization off (the common case on current builds - its own
        # log says "Filesystem not virtualized") keeps its config right here in
        # %LOCALAPPDATA%\Claude-3p. So: trust a local folder that already
        # exists, and only look inside the MSIX container when neither does.
        if not os.path.isdir(normal) and not os.path.isdir(threep):
            packaged = _msix_claude_dirs(base)
            if packaged:
                return packaged
        return normal, threep
    if sys.platform == "darwin":
        base = os.path.join(home, "Library", "Application Support")
        return os.path.join(base, "Claude"), os.path.join(base, "Claude-3p")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return os.path.join(base, "Claude"), os.path.join(base, "Claude-3p")


def _msix_claude_dirs(local_app_data):
    """Claude's data dirs inside a Microsoft Store package sandbox, or None.

    Only reached when no local Claude/Claude-3p folder exists, i.e. a Store
    build whose filesystem *is* virtualized. Such an install keeps its data in
    Packages\\<PackageFamily>\\LocalCache\\{Roaming,Local}\\Claude[-3p]; the
    family name carries a publisher hash, so match on the package name and
    prefer whichever side the app already created.
    """
    packages = os.path.join(local_app_data, "Packages")
    try:
        entries = os.listdir(packages)
    except OSError:
        return None
    family = None
    for name in entries:
        if name.lower().startswith("anthropicpbc.claude"):
            family = name
            break
    if not family:
        return None
    root = os.path.join(packages, family, "LocalCache")
    roaming = os.path.join(root, "Roaming", "Claude")
    local = os.path.join(root, "Local", "Claude")
    # The 1P config normally sits under Roaming, the 3P config under Local;
    # fall back to whichever side the app actually created so a re-detection
    # stays stable. The caller creates parents before writing.
    normal = roaming if os.path.isdir(roaming) else local
    threep = os.path.join(root, "Local", "Claude-3p")
    return normal, threep


def _claude_desktop_paths(home):
    normal_dir, threep_dir = _claude_desktop_dirs(home)
    library = os.path.join(threep_dir, CLAUDE_DESKTOP_LIBRARY_DIR)
    return {
        "config": os.path.join(normal_dir, CLAUDE_DESKTOP_CONFIG_FILE),
        "config3p": os.path.join(threep_dir, CLAUDE_DESKTOP_CONFIG_FILE),
        "profile": os.path.join(library, CLAUDE_DESKTOP_PROFILE_ID + ".json"),
        "meta": os.path.join(library, "_meta.json"),
    }


def _claude_desktop_deployment_mode(path):
    data = _load_json(path, "claude desktop config")
    data["deploymentMode"] = "3p"
    return _dump_json(data)


def _claude_desktop_profile(ctx):
    """The gateway profile Claude Desktop reads out of configLibrary."""
    root = re.sub(r"/v1/?$", "", str(ctx.get("base_url") or ""))
    # Merge into whatever profile is already on disk instead of replacing it
    # wholesale: CC Switch writes the same profile as a patch whose clear scope
    # is exactly the "floor" keys below, and seeds the two policy keys only
    # when absent. A blanket overwrite drops keys Claude Desktop itself owns
    # (and, on a running app, gets rolled back the next time it saves - which
    # is how the gateway profile disappears again).
    return _claude_desktop_profile_for(ctx.get("profile_current"))


#: The profile keys Claude Desktop reads for a custom gateway. Everything else
#: in the file is left as the user / the app set it. Mirrors CC Switch's
#: DESKTOP_PROFILE_FLOOR.
CLAUDE_DESKTOP_PROFILE_FLOOR = (
    "inferenceProvider",
    "inferenceGatewayBaseUrl",
    "inferenceGatewayApiKey",
    "inferenceGatewayAuthScheme",
    "inferenceModels",
    "modelDiscoveryEnabled",
)

#: Written only when missing, so the user can tighten them by hand.
CLAUDE_DESKTOP_PROFILE_SEED = ("disableDeploymentModeChooser",
                               "coworkEgressAllowedHosts")


def _claude_desktop_profile_for(ctx, current=None):
    """Build the profile object for `ctx`, merging over `current` (a dict)."""
    root = re.sub(r"/v1/?$", "", str(ctx.get("base_url") or ""))
    existing = current if isinstance(current, dict) else {}
    profile = dict(existing)
    for key in CLAUDE_DESKTOP_PROFILE_FLOOR:
        profile.pop(key, None)
    profile.update({
        "inferenceProvider": "gateway",
        "inferenceGatewayBaseUrl": root,
        "inferenceGatewayApiKey": ctx.get("api_key") or "",
        "inferenceGatewayAuthScheme": "bearer",
        "inferenceModels": _claude_desktop_inference_models(ctx),
    })
    for key in CLAUDE_DESKTOP_PROFILE_SEED:
        profile.setdefault(key, True if key == "disableDeploymentModeChooser"
                           else ["*"])
    return profile


def _claude_desktop_inference_models(ctx):
    """The profile's model list: role ids plus the label the picker shows.

    Claude Desktop only accepts `claude-<role>-<something>` ids, so the real
    model name cannot be the id itself. Every model the gateway offers is
    advertised (the schema allows 200), the id is a safe role-shaped slug built
    from the model name, and the real name travels in `labelOverride` - the
    same trick CC Switch uses, and what ends up on screen.
    """
    out = []
    seen = set()
    for entry in ctx.get("models") or []:
        mid = (entry.get("id") if isinstance(entry, dict) else entry) or ""
        mid = str(mid).strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append({"name": _claude_route_slug(mid, len(out)),
                    "labelOverride": mid})
    if out:
        return out
    # Nothing was offered (a picker-less apply): fall back to the single model
    # this integration was configured with, under the first role slot.
    model = str(ctx.get("model") or "").strip()
    if not model:
        return [{"name": route_id, "labelOverride": role}
                for route_id, role in CLAUDE_DESKTOP_ROUTES]
    return [{"name": "claude-sonnet-wb", "labelOverride": model}]


def _claude_route_slug(model, index):
    """A Claude-Desktop-safe id that still names the upstream model.

    The id has to start with `claude-<role>-` (the app rejects the whole group
    otherwise). The rest is ours, so the model name is carried verbatim after
    an `=` marker: the gateway splits on it and gets the exact model back,
    dots and all, instead of guessing from a lossy normalisation.
    """
    role = CLAUDE_DESKTOP_ROUTES[index % len(CLAUDE_DESKTOP_ROUTES)][0]
    return "%s=%s" % (role, model)
    return out


def _claude_desktop_installed(home):
    """True when Claude Desktop is present in either the exe or Store form.

    The exe build and a non-virtualized Store build both live in
    %LOCALAPPDATA%\\Claude[-3p]; a fully virtualized Store build lives in the
    package sandbox. Detect any of them, plus a `claude` / `claude-desktop`
    binary on PATH.
    """
    normal, threep = _claude_desktop_dirs(home)
    if os.path.isdir(normal) or os.path.isdir(threep):
        return True
    if os.path.isfile(os.path.join(normal, CLAUDE_DESKTOP_CONFIG_FILE)):
        return True
    if os.path.isfile(os.path.join(threep, CLAUDE_DESKTOP_CONFIG_FILE)):
        return True
    base = os.path.join(home, "AppData", "Local")
    if _msix_claude_dirs(base) is not None:
        return True
    return bool(shutil.which("claude") or shutil.which("claude-desktop"))


def _claude_desktop_meta(path):
    """Register our profile in configLibrary/_meta.json and make it current."""
    data = _load_json(path, "claude desktop library index")
    entries = data.get("entries")
    if not isinstance(entries, list):
        entries = []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("id") == CLAUDE_DESKTOP_PROFILE_ID:
            entry["name"] = CLAUDE_DESKTOP_PROFILE_NAME
            break
    else:
        entries.append({"id": CLAUDE_DESKTOP_PROFILE_ID,
                        "name": CLAUDE_DESKTOP_PROFILE_NAME})
    data["entries"] = entries
    data["appliedId"] = CLAUDE_DESKTOP_PROFILE_ID
    return _dump_json(data)


def _apply_claude_desktop(paths, ctx):
    """Write the 3P gateway profile the way Claude Desktop expects to read it.

    Files we create ourselves (the profile and the library index) are merged
    over their current bytes; the two `claude_desktop_config.json` files are
    only touched when they already exist, because creating a bare 1P config in
    a fresh `Claude\\` folder is how a Store install ends up with an empty
    directory it never looks at.
    """
    out = {}
    profile_path = paths["profile"]
    current = None
    if os.path.isfile(profile_path):
        try:
            current = json.loads(_read_text(profile_path))
        except Exception:
            current = None
    out[profile_path] = _dump_json(_claude_desktop_profile_for(ctx, current))
    out[paths["meta"]] = _claude_desktop_meta(paths["meta"])
    for key in ("config3p", "config"):
        path = paths[key]
        if os.path.isfile(path):
            out[path] = _claude_desktop_deployment_mode(path)
    return out


CLIENTS = {
    "claude-code": {
        "id": "claude-code",
        "label": "Claude Code",
        "desc": "Anthropic CLI; points ANTHROPIC_BASE_URL at this gateway.",
        "protocol": "anthropic",
        "files": lambda home: {
            "settings": _claude_settings_path(home),
        },
        "probe": lambda home: (
            os.path.isdir(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(home, ".claude"))
            or os.path.isfile(_claude_settings_path(home))
            or bool(shutil.which("claude"))
        ),
        "apply": _apply_claude_code,
    },
    "claude-desktop": {
        "id": "claude-desktop",
        "label": "Claude Desktop",
        "desc": "Claude 桌面版；写入 Claude-3p 的 configLibrary 网关 profile"
                "（deploymentMode=3p）。",
        "protocol": "anthropic",
        "files": _claude_desktop_paths,
        "probe": _claude_desktop_installed,
        "apply": _apply_claude_desktop,
        "processes": _is_claude_desktop_process,
        "relaunch": _launch_claude_desktop,
    },
    "codex": {
        "id": "codex",
        "label": "Codex CLI / App",
        "desc": "OpenAI Codex (CLI and desktop app); adds a wb-proxy model "
                "provider plus the model catalog the app picker needs.",
        "protocol": "openai",
        "files": lambda home: {
            "config": os.path.join(_codex_home(home), "config.toml"),
            "auth": os.path.join(_codex_home(home), "auth.json"),
            "catalog": os.path.join(_codex_home(home), CODEX_CATALOG_FILE),
        },
        "probe": lambda home: (
            os.path.isdir(_codex_home(home))
            or os.path.isfile(os.path.join(_codex_home(home), "config.toml"))
            or bool(shutil.which("codex"))
        ),
        "apply": _apply_codex,
        "processes": _is_codex_process,
        "relaunch": _launch_codex,
    },
    "opencode": {
        "id": "opencode",
        "label": "OpenCode",
        "desc": "OpenCode; registers an openai-compatible provider.",
        "protocol": "openai",
        "files": lambda home: {
            "config": os.path.join(home, ".config", "opencode", "opencode.json"),
        },
        "probe": lambda home: (
            os.path.isdir(os.path.join(home, ".config", "opencode"))
            or os.path.isfile(os.path.join(home, ".config", "opencode", "opencode.json"))
            or bool(shutil.which("opencode"))
        ),
        "apply": _apply_opencode,
    },
    "dsh": {
        "id": "dsh",
        "label": "DeepSeek Harness",
        "desc": "DSH; adds a wb-proxy provider to settings.yaml.",
        "protocol": "openai",
        "files": lambda home: {
            "settings": os.path.join(home, ".dsh", "settings.yaml"),
            "credentials": os.path.join(home, ".dsh", ".credentials.yaml"),
        },
        "probe": lambda home: (
            os.path.isdir(os.path.join(home, ".dsh"))
            or os.path.isfile(os.path.join(home, ".dsh", "settings.yaml"))
            or bool(shutil.which("dsh"))
        ),
        "apply": _apply_dsh,
    },
    "crush": {
        "id": "crush",
        "label": "Crush",
        "desc": "Charm Crush; adds a wb-proxy provider entry.",
        "protocol": "openai",
        "files": lambda home: {
            "config": os.path.join(home, ".config", "crush", "crush.json"),
        },
        "probe": lambda home: (
            os.path.isdir(os.path.join(home, ".config", "crush"))
            or os.path.isfile(os.path.join(home, ".config", "crush", "crush.json"))
            or bool(shutil.which("crush"))
        ),
        "apply": _apply_crush,
    },
    "zcode": {
        "id": "zcode",
        "label": "ZCode",
        "desc": "ZCode; adds a wb-proxy provider to its provider config. "
                "Quit ZCode first - it rewrites that file while running.",
        "protocol": "openai",
        "files": lambda home: {
            "config": os.path.join(home, ".zcode", "v2", "provider_config.json"),
        },
        "probe": lambda home: (
            os.path.isdir(os.path.join(home, ".zcode"))
            or os.path.isfile(os.path.join(home, ".zcode", "v2",
                                           "provider_config.json"))
        ),
        "apply": _apply_zcode,
    },
}


def _client(client_id):
    client = CLIENTS.get(client_id)
    if client is None:
        raise AgentConfigError(
            "unknown client %r (known: %s)"
            % (client_id, ", ".join(sorted(CLIENTS))))
    return client


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def overview(accounts_dir, home=None):
    """Per-client install/apply summary for the panel."""
    home = _resolve_home(home)
    state = _load_state(accounts_dir)
    # One process table for every client: the panel shows which ones are
    # running, and one PowerShell round-trip per card would be wasteful.
    try:
        procs = process_snapshot()
    except Exception:
        procs = None
    out = {}
    for client_id in sorted(CLIENTS):
        client = CLIENTS[client_id]
        paths = client["files"](home)
        existing = [p for p in paths.values() if os.path.exists(p)]
        applied = None
        record = state.get(client_id)
        if isinstance(record, dict):
            external = False
            for entry in record.get("files") or []:
                path = entry.get("path")
                if not path or not os.path.exists(path):
                    external = True
                    continue
                # Compare against the bytes we last wrote, not the backup: a
                # hand edit after apply must read as an external change.
                try:
                    if _sha256_bytes(_read_bytes(path)) != entry.get("sha256"):
                        external = True
                except AgentConfigError:
                    external = True
            applied = {
                "at": record.get("applied_at"),
                "base_url": record.get("base_url"),
                "model": record.get("model"),
                # Which key this client was configured with, so the panel can
                # show the exit it is actually on (the key's bound exit, not
                # the global switch). Never carries the key value itself.
                "key_id": record.get("key_id"),
                "home": record.get("home"),
                "files": len(record.get("files") or []),
                "external_change": external,
            }
        out[client_id] = {
            "id": client_id,
            "label": client["label"],
            "desc": client["desc"],
            "protocol": client["protocol"],
            "installed": bool(client["probe"](home)) or bool(existing),
            # Whether the panel may offer "restart it for me", and whether it
            # is up right now (None = the process table could not be read).
            "restartable": client.get("processes") is not None,
            "running": (None if (procs is None or client.get("processes") is None)
                        else bool([p for p in procs if client["processes"](p)])),
            # "configured" is about this gateway's integration record, not about
            # the client merely having a config file on disk - otherwise every
            # pre-existing config would advertise a restore that cannot run.
            "configured": isinstance(record, dict),
            "config_paths": list(paths.values()),
            "applied": applied,
        }
    return out


def plan(client_id, base_url, api_key, model=None, models=None, home=None):
    """The exact file contents integrate() would write; touches nothing.

    Each entry in ``files`` carries the target ``path``, the new ``content``
    and whether the file already ``existed``, so a caller can preview an
    apply - the CLI's --dry-run - before anything is backed up or written.
    integrate() and the preview share this one builder, so what a dry run
    prints is what a real run writes.
    """
    client = _client(client_id)
    home = _resolve_home(home)
    base_url = str(base_url or "").strip().rstrip("/")
    if not re.match(r"^https?://", base_url):
        raise AgentConfigError("base_url must start with http:// or https://")
    base_url_v1 = base_url if base_url.endswith("/v1") else base_url + "/v1"
    ctx = {
        "base_url": base_url,
        "base_url_v1": base_url_v1,
        "api_key": str(api_key or ""),
        "model": str(model or "").strip() or None,
        "models": list(models or []),
    }
    paths = client["files"](home)
    try:
        new_contents = client["apply"](paths, ctx)
    except AgentConfigError:
        raise
    except Exception as exc:
        raise AgentConfigError("failed to build %s config: %s"
                               % (client_id, exc))
    files = []
    for path in paths.values():
        content = new_contents.get(path)
        if content is None:
            continue
        files.append({
            "path": path,
            "content": content,
            "existed": os.path.exists(path),
        })
    return {
        "client": client_id,
        "label": client["label"],
        "base_url": base_url,
        "model": ctx["model"],
        "files": files,
    }


def integrate(accounts_dir, client_id, base_url, api_key, model=None,
              models=None, home=None, key_id=None):
    """Point `client_id` at this gateway; returns what was written.

    The first apply backs up the original bytes of every touched file; later
    applies keep the earliest backup so restore always returns to the state
    before the very first integration.

    `key_id` records *which* panel key was handed to the client (never the key
    itself), so a later re-apply - switching the upstream exit rewrites the
    embedded model list - can resolve the same key again.
    """
    built = plan(client_id, base_url, api_key, model=model, models=models,
                 home=home)
    state = _load_state(accounts_dir)
    record = state.get(client_id) or {}
    previous_files = {entry.get("path"): entry
                      for entry in record.get("files") or []
                      if isinstance(entry, dict)}
    # Phase 1: stage backups for all target files before making any modifications.
    staged = []
    for entry in built["files"]:
        path, content, existed = entry["path"], entry["content"], entry["existed"]
        prior = previous_files.get(path)
        if prior is not None and not prior.get("existed") and existed:
            # We created this file in an earlier apply. While it still holds
            # exactly what we wrote it stays "ours", so restore deletes it
            # instead of resurrecting our own bytes (that is the documented
            # promise, and the bytes may hold the API key); a hand edit since
            # then makes it the user's file again, and it gets backed up.
            try:
                if _sha256_bytes(_read_bytes(path)) == prior.get("sha256"):
                    existed = False
            except AgentConfigError:
                pass
        backup = None
        if existed and prior and prior.get("backup"):
            # Keep the very first backup: it holds the pre-integration bytes.
            backup = prior.get("backup")
        elif existed:
            backup = _backup_file(accounts_dir, client_id, path,
                                  _read_bytes(path))
        staged.append((path, content, existed, backup))

    # Phase 2: apply writes transactionally. If any write fails, roll back
    # all already-written files so a multi-file integration (e.g. DSH) never
    # leaves a half-configured state with missing state records.
    written = []
    entries = []
    try:
        for path, content, existed, backup in staged:
            _atomic_write_text(path, content)
            written.append((path, existed, backup))
            entries.append({
                "path": path,
                "existed": existed,
                "backup": backup,
                "sha256": _sha256_bytes(content.encode("utf-8")),
                "bytes": len(content.encode("utf-8")),
            })
    except Exception as exc:
        for w_path, w_existed, w_backup in written:
            try:
                if w_existed and w_backup:
                    # w_backup 是 _backup_file() 记下的**完整备份路径**（state
                    # 与 restore() 都按整条路径用它），直接读回即可。这里曾经
                    # 先拼了一次 backup_dir(...)，而那个名字全模块都不存在：
                    # NameError 被下面的 except 吞掉，于是多文件客户端写一半
                    # 失败时既不回滚也不写 state——面板显示「未配置」，而
                    # restore() 又因为查不到记录而拒绝执行。
                    _atomic_write_bytes(w_path, _read_bytes(w_backup))
                elif not w_existed and os.path.exists(w_path):
                    os.unlink(w_path)
            except Exception:
                pass
        raise AgentConfigError("failed to write %s files: %s" % (client_id, exc))

    state[client_id] = {
        "applied_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "base_url": built["base_url"],
        "model": built["model"],
        "key_id": key_id,
        "home": home,
        "files": entries,
    }
    _save_state(accounts_dir, state)
    return {
        "client": client_id,
        "label": built["label"],
        "applied_at": state[client_id]["applied_at"],
        "base_url": built["base_url"],
        "model": built["model"],
        "key_id": key_id,
        "files": entries,
    }


def integration_state(accounts_dir):
    """The raw per-client integration record (never contains an API key)."""
    return _load_state(accounts_dir)


def restore(accounts_dir, client_id, home=None):
    """Undo integrate(): put the backed-up originals back byte for byte.

    Files that did not exist before the first apply are deleted; the state
    record for the client is cleared.
    """
    client = _client(client_id)
    home = _resolve_home(home)
    state = _load_state(accounts_dir)
    record = state.get(client_id)
    if not isinstance(record, dict) or not record.get("files"):
        raise AgentConfigError(
            "%s has no recorded integration to restore" % client["label"])
    restored = []
    for entry in record.get("files") or []:
        path = entry.get("path")
        if not path:
            continue
        if entry.get("existed") and entry.get("backup"):
            backup = entry["backup"]
            if not os.path.exists(backup):
                raise AgentConfigError(
                    "backup missing for %s: %s" % (path, backup))
            _atomic_write_bytes(path, _read_bytes(backup))
            restored.append({"path": path, "action": "restored"})
        else:
            # We created this file; removing it returns to the original state.
            try:
                os.remove(path)
                restored.append({"path": path, "action": "deleted"})
            except FileNotFoundError:
                restored.append({"path": path, "action": "already-absent"})
            # Applying had to create the parent directories too. Leaving an
            # empty one behind makes the probe report the client as installed
            # when the only config it ever had was ours, so prune the chain we
            # emptied - never touching a directory that still holds anything.
            _prune_empty_parents(path, home)
    state.pop(client_id, None)
    _save_state(accounts_dir, state)
    return {
        "client": client_id,
        "label": client["label"],
        "restored": restored,
    }


# ---------------------------------------------------------------------------
# Standalone configurator
# ---------------------------------------------------------------------------
#
# The panel's one-click setup runs inside the gateway process, so it can only
# ever edit the machine the gateway runs on - which is also why /agents/apply
# refuses non-loopback callers and container deployments. The CLI inverts the
# direction: it runs where the clients are and reaches the gateway only over
# HTTP (the model list), so a gateway on a server, NAS or router can still
# serve clients on every other machine.


def fetch_models_from_gateway(base_url, api_key, timeout=10):
    """The gateway's model list, for clients that embed model definitions.

    Reads the OpenAI-compatible ``GET /v1/models``, which is reachable with
    the API key alone (it is not a panel route). Doubles as key validation:
    a 401 here means the key is wrong, anything else means the gateway or its
    upstream is unreachable.
    """
    base = str(base_url or "").strip().rstrip("/")
    if not re.match(r"^https?://", base):
        raise AgentConfigError("base_url must start with http:// or https://")
    if base.endswith("/v1"):
        base = base[:-3]
    request = urllib.request.Request(
        base + "/v1/models",
        headers={"Authorization": "Bearer " + str(api_key or "")})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise AgentConfigError(
                "the gateway rejected the API key (HTTP 401)")
        raise AgentConfigError("gateway /v1/models failed: HTTP %s" % exc.code)
    except ValueError as exc:
        raise AgentConfigError(
            "gateway /v1/models returned invalid JSON: %s" % exc)
    except Exception as exc:
        raise AgentConfigError(
            "cannot reach the gateway /v1/models: %s" % exc)
    models = []
    seen = set()
    for item in (payload.get("data") or []):
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        entry = {"id": mid}
        context = item.get("context_length") or item.get("max_input_tokens")
        if context:
            try:
                entry["context_window"] = int(context)
            except (TypeError, ValueError):
                pass
        out_tokens = item.get("max_output_tokens")
        if out_tokens:
            try:
                entry["max_output"] = int(out_tokens)
                entry["max_output_tokens"] = int(out_tokens)
            except (TypeError, ValueError):
                pass
        efforts = item.get("reasoning_efforts")
        if isinstance(efforts, (list, tuple)):
            levels = [str(e).strip() for e in efforts if str(e or "").strip()]
            if levels:
                entry["reasoning_efforts"] = levels
        name = item.get("name")
        if name:
            entry["name"] = str(name)
        # Capability metadata the Codex desktop-app catalog needs.
        inputs = item.get("input_modalities")
        if isinstance(inputs, list):
            entry["input_modalities"] = [
                str(x) for x in inputs if str(x).strip()]
        if item.get("supports_reasoning") is not None:
            entry["reasoning"] = bool(item.get("supports_reasoning"))
        if item.get("supports_tool_calls") is not None:
            entry["supports_tool_calls"] = bool(item.get("supports_tool_calls"))
        if item.get("supports_vision") is not None:
            entry["supports_vision"] = bool(item.get("supports_vision"))
        if item.get("vision") is not None:
            entry["vision"] = bool(item.get("vision"))
        efforts = item.get("reasoning_efforts")
        if isinstance(efforts, list):
            entry["reasoning_efforts"] = [
                str(x) for x in efforts if str(x).strip()]
        default_effort = (item.get("reasoning_default_effort")
                          or item.get("reasoning_fixed_effort"))
        if default_effort:
            entry["reasoning_default_effort"] = str(default_effort)
        desc = item.get("description")
        if desc:
            entry["description"] = str(desc)
        models.append(entry)
    # Same cap as the panel's apply: a runaway catalog must not turn into a
    # megabyte config file.
    return models[:80]


def _default_state_dir():
    """Where the standalone CLI keeps its state and backups.

    Deliberately NOT the gateway's accounts/ directory: this runs on a
    different machine, and every machine keeps its own record and backups.
    """
    return os.path.join(os.path.expanduser("~"), ".wb-agent-config")


def _mask_key(key):
    key = str(key or "")
    if not key:
        return "(none)"
    if len(key) <= 4:
        return "***"
    return "***" + key[-4:]


def _cli_error(message):
    sys.stderr.write("error: %s\n" % message)
    return 1


def main(argv=None):
    # --home/--state-dir/--json are accepted before AND after the subcommand:
    # the shared parent carries them into every subparser with SUPPRESS
    # defaults, so a subparser never clobbers a value given at the top level.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--home", default=argparse.SUPPRESS,
                        help="home directory to configure "
                             "(default: the current user's home)")
    common.add_argument("--state-dir", default=argparse.SUPPRESS,
                        dest="state_dir",
                        help="where the integration state and backups live "
                             "(default: ~/.wb-agent-config)")
    common.add_argument("--json", action="store_true", dest="as_json",
                        default=argparse.SUPPRESS,
                        help="print machine-readable JSON")

    parser = argparse.ArgumentParser(
        prog="wb_agents.py", parents=[common],
        description="One-click agent integration for local AI clients. Runs "
                    "where the clients are, so the gateway may live on "
                    "another machine.")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", parents=[common],
                   help="supported clients and what is installed on this machine")
    sub.add_parser("status", parents=[common],
                   help="this machine's integration records")

    apply_p = sub.add_parser("apply", parents=[common],
                             help="point a client at the gateway")
    apply_p.add_argument("--client", required=True,
                         help="one of: " + ", ".join(sorted(CLIENTS)))
    apply_p.add_argument("--base-url", required=True, dest="base_url",
                         help="gateway URL, e.g. http://gateway:8788/v1")
    apply_p.add_argument("--key", default=None,
                         help="gateway API key "
                              "(default: $WB_PROXY_API_KEY, then a prompt)")
    apply_p.add_argument("--model", default=None,
                         help="default model id to select in the client")
    apply_p.add_argument("--models-from-gateway", action="store_true",
                         dest="models_from_gateway",
                         help="fetch the model list from /v1/models "
                              "(this is the default)")
    apply_p.add_argument("--no-models", action="store_true", dest="no_models",
                         help="skip the model list; also skips key validation")
    apply_p.add_argument("--dry-run", action="store_true", dest="dry_run",
                         help="print what would be written; touch nothing")

    restore_p = sub.add_parser("restore", parents=[common],
                               help="undo an apply, byte for byte")
    restore_p.add_argument("--client", required=True,
                           help="one of: " + ", ".join(sorted(CLIENTS)))

    args = parser.parse_args(argv)

    # A Chinese path or model name in the output must not abort the run on a
    # legacy Windows console - the same guard tests/run_all.py applies to
    # itself.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    as_json = getattr(args, "as_json", False)
    state_dir = getattr(args, "state_dir", None) or _default_state_dir()
    home = _resolve_home(getattr(args, "home", None))

    if args.command in ("list", "status"):
        data = overview(state_dir, home)
        if as_json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0
        for client_id in sorted(data):
            entry = data[client_id]
            print("%-12s %-18s %s" % (
                client_id, entry["label"],
                "installed" if entry["installed"] else "not detected"))
            for path in entry["config_paths"]:
                print("    config:  %s" % path)
            if args.command == "status":
                applied = entry.get("applied")
                if applied:
                    print("    applied: %s  base_url=%s  model=%s%s" % (
                        applied["at"], applied["base_url"],
                        applied["model"] or "-",
                        "  (external change detected)"
                        if applied["external_change"] else ""))
                else:
                    print("    applied: never")
        return 0

    if args.command == "apply":
        if args.no_models and args.models_from_gateway:
            return _cli_error(
                "--no-models and --models-from-gateway are mutually exclusive")
        client_id = str(args.client).strip()
        try:
            _client(client_id)
        except AgentConfigError as exc:
            return _cli_error(str(exc))
        base_url = str(args.base_url).strip()
        if not re.match(r"^https?://", base_url):
            return _cli_error("--base-url must start with http:// or https://")
        key = str(args.key or os.environ.get("WB_PROXY_API_KEY") or "")
        # Prompt only when there is a real console. On Windows isatty() is
        # also true for the NUL device (a common stdin for spawned or
        # service-run processes), and getpass there reads CONIN$ directly -
        # it would block forever in a process that has no console. stdout
        # being a terminal is the signal that one exists.
        if not key and sys.stdin.isatty() and sys.stdout.isatty():
            try:
                key = getpass.getpass("Gateway API key: ").strip()
            except (EOFError, KeyboardInterrupt):
                key = ""
        if not key:
            return _cli_error(
                "no API key: pass --key, set WB_PROXY_API_KEY, or run "
                "interactively")

        models = []
        if not args.no_models:
            try:
                models = fetch_models_from_gateway(base_url, key)
            except AgentConfigError as exc:
                return _cli_error(
                    "%s - retry, or pass --no-models to configure without "
                    "the model list and key validation" % exc)

        if args.dry_run:
            try:
                built = plan(client_id, base_url, key, model=args.model,
                             models=models, home=home)
            except AgentConfigError as exc:
                return _cli_error(str(exc))
            if as_json:
                print(json.dumps({
                    "client": built["client"],
                    "label": built["label"],
                    "base_url": built["base_url"],
                    "model": built["model"],
                    "files": [{"path": item["path"],
                               "existed": item["existed"],
                               "content": item["content"]}
                              for item in built["files"]],
                }, ensure_ascii=False, indent=2))
                return 0
            print("dry run: nothing will be written")
            print("client:  %s (%s)" % (built["label"], built["client"]))
            print("gateway: %s" % built["base_url"])
            print("key:     %s" % _mask_key(key))
            for item in built["files"]:
                print()
                print("--- %s %s" % (
                    "modify" if item["existed"] else "create", item["path"]))
                print(item["content"], end="")
                if not item["content"].endswith("\n"):
                    print()
            return 0

        try:
            result = integrate(state_dir, client_id, base_url, key,
                               model=args.model, models=models, home=home)
        except AgentConfigError as exc:
            return _cli_error(str(exc))
        if as_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        print("configured %s (%s) -> %s" % (
            result["label"], result["client"], result["base_url"]))
        for entry in result["files"]:
            print("  %-8s %s" % (
                "updated" if entry["existed"] else "created", entry["path"]))
        if models:
            print("models:  %d fetched from the gateway (key accepted)"
                  % len(models))
        print("key:     %s" % _mask_key(key))
        print("state:   %s" % state_dir)
        print("restore: python %s restore --client %s"
              % (os.path.basename(__file__), client_id))
        return 0

    if args.command == "restore":
        client_id = str(args.client).strip()
        try:
            result = restore(state_dir, client_id, home=home)
        except AgentConfigError as exc:
            return _cli_error(str(exc))
        if as_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        print("restored %s (%s)" % (result["label"], result["client"]))
        for entry in result["restored"]:
            print("  %-9s %s" % (entry["action"], entry["path"]))
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
