"""Third-party upstream: point the gateway at another OpenAI-compatible API.

The gateway speaks WorkBuddy's internal wire format to itself: every handler
parses the upstream response as chat-completions SSE (choices[].delta.content)
and rewrites it into the Responses / Anthropic / Chat envelopes the clients
asked for. A third-party OpenAI-compatible endpoint already speaks that inner
format, so this module only has to swap *where* the request goes and *which
credential* rides along - the translation layers downstream are untouched.

Turning it on replaces the WorkBuddy exit for chat traffic (model list, account
pool, realm, per-key exits). Turning it off restores the previous state
verbatim: the configuration is persisted, so an operator who flips the switch
back gets the WorkBuddy accounts they had before. Agent integrations are not
touched either way - they point at this gateway, not at an upstream.
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

#: Where the profile is persisted. Lives beside settings.json so a deployment
#: that relocates its accounts dir keeps the two together.
CONFIG_FILE = "thirdparty-upstream.json"

#: A body larger than this is refused, for the same reason the gateway caps its
#: own request bodies: the whole thing is read into memory before it is parsed.
MAX_BODY_BYTES = 32 * 1024 * 1024

#: Seconds to wait for the first byte of the upstream response.
DEFAULT_HEADER_TIMEOUT = 60

_lock = threading.Lock()
_cache = {"at": 0.0, "models": None}
_CACHE_TTL = 60.0


def config_path(accounts_dir):
    return os.path.join(accounts_dir, CONFIG_FILE)


def load(accounts_dir):
    """The saved profile, or the disabled default when nothing is stored."""
    try:
        with open(config_path(accounts_dir), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {
        "enabled": data.get("enabled") is True,
        "base_url": str(data.get("base_url") or "").strip(),
        "api_key": str(data.get("api_key") or "").strip(),
        # Cached on the profile so the panel can render the model list without
        # a round trip; refreshed by probe_models().
        "models": [str(m) for m in (data.get("models") or []) if str(m).strip()],
        "models_at": data.get("models_at") or 0,
        "models_error": str(data.get("models_error") or ""),
    }


def save(accounts_dir, profile):
    """Persist a profile, dropping a stale model list when the endpoint moved.

    The model cache is keyed on nothing, so a profile whose base_url or key
    changed must not keep advertising the previous endpoint's models.
    """
    with _lock:
        current = load(accounts_dir)
        payload = {
            "enabled": profile.get("enabled") is True,
            "base_url": str(profile.get("base_url") or "").strip(),
            "api_key": str(profile.get("api_key") or "").strip(),
            "updated_at": time.time(),
        }
        same_endpoint = (payload["base_url"] == current.get("base_url")
                         and payload["api_key"] == current.get("api_key"))
        if same_endpoint:
            payload["models"] = current.get("models") or []
            payload["models_at"] = current.get("models_at") or 0
            payload["models_error"] = current.get("models_error") or ""
        os.makedirs(accounts_dir, exist_ok=True)
        tmp = config_path(accounts_dir) + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, config_path(accounts_dir))
    _cache["at"] = 0.0
    _cache["models"] = None
    return load(accounts_dir)


def is_enabled(accounts_dir):
    return load(accounts_dir)["enabled"]


def normalise_chat_url(base_url):
    """Append `/chat/completions` unless the caller already did.

    Someone pasting `https://api.example.com/v1` and someone pasting
    `https://api.example.com/v1/chat/completions` mean the same endpoint, and
    neither should end up with a doubled path.
    """
    url = str(base_url or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        return None
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1"):
        return url + "/chat/completions"
    return url + "/v1/chat/completions"


def chat_url(accounts_dir):
    """The chat-completions URL of the configured endpoint, or None."""
    profile = load(accounts_dir)
    if not profile["enabled"] or not profile["base_url"]:
        return None
    return normalise_chat_url(profile["base_url"])


def probe_models(accounts_dir, timeout=30):
    """Fetch the endpoint's model list and cache it on the profile.

    Returns (models, error): an empty list plus an error string means the
    endpoint could not be reached or refused the key. Never raises, so a bad
    URL cannot take the panel down with it.
    """
    profile = load(accounts_dir)
    if not profile["enabled"]:
        return [], "third-party upstream is disabled"
    if not profile["base_url"] or not profile["api_key"]:
        return [], "base_url and api_key are required"
    base = normalise_chat_url(profile["base_url"])
    if not base:
        return [], "base_url must start with http:// or https://"
    url = re.sub(r"/chat/completions$", "/models", base)
    req = urllib.request.Request(url, method="GET", headers={
        "Authorization": "Bearer " + profile["api_key"],
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_BODY_BYTES)
        payload = json.loads(raw.decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(400).decode("utf-8", "replace")
        except Exception:
            pass
        return [], "HTTP %s %s" % (exc.code, detail[:200])
    except Exception as exc:
        return [], str(exc)[:200]
    items = payload.get("data") if isinstance(payload, dict) else payload
    models = []
    if isinstance(items, list):
        for item in items:
            mid = (item.get("id") if isinstance(item, dict) else item) or ""
            mid = str(mid).strip()
            if mid and mid not in models:
                models.append(mid)
    with _lock:
        data = load(accounts_dir)
        data.update({"models": models, "models_at": time.time(),
                     "models_error": ""})
        os.makedirs(accounts_dir, exist_ok=True)
        tmp = config_path(accounts_dir) + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, config_path(accounts_dir))
    _cache["at"] = time.time()
    _cache["models"] = models
    return models, ""


def model_ids(accounts_dir):
    """The endpoint's model ids, refreshing at most once a minute."""
    profile = load(accounts_dir)
    if not profile["enabled"]:
        return []
    now = time.time()
    if _cache["models"] is not None and now - _cache["at"] < _CACHE_TTL:
        return list(_cache["models"])
    if profile["models"] and now - (profile.get("models_at") or 0) < _CACHE_TTL:
        _cache["at"] = now
        _cache["models"] = list(profile["models"])
        return list(profile["models"])
    models, _err = probe_models(accounts_dir)
    return models


def open_chat(payload, accounts_dir):
    """POST a chat-completions request to the third-party endpoint.

    The body is built exactly as `open_upstream` builds it for WorkBuddy, so
    the caller can hand the response straight to the same translation layers.
    Returns the urllib response object; the caller owns closing it.
    """
    profile = load(accounts_dir)
    url = normalise_chat_url(profile["base_url"])
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": "Bearer " + profile["api_key"],
        "Content-Type": "application/json",
        "Accept": "text/event-stream, application/json",
    })
    return urllib.request.urlopen(req, timeout=DEFAULT_HEADER_TIMEOUT)


def describe(accounts_dir):
    """Panel-facing view: never carries the api key value."""
    profile = load(accounts_dir)
    return {
        "enabled": profile["enabled"],
        "base_url": profile["base_url"],
        "api_key_set": bool(profile["api_key"]),
        "models": profile["models"],
        "models_at": profile["models_at"],
        "models_error": profile["models_error"],
        "chat_url": normalise_chat_url(profile["base_url"]) if profile["base_url"] else "",
        "vendor": vendor_label(profile["base_url"]),
    }


#: Hostnames of services worth naming in the panel. A request billed by one of
#: these reads as its vendor rather than as "其他 (host)".
VENDOR_HOSTS = (
    ("developer.amd.com.cn", "AMD"),
    ("api.deepseek.com", "DeepSeek"),
    ("open.bigmodel.cn", "智谱 GLM"),
    ("api.moonshot.cn", "Moonshot"),
    ("api.minimax.chat", "MiniMax"),
    ("dashscope.aliyuncs.com", "阿里通义"),
    ("api.openai.com", "OpenAI"),
    ("generativelanguage.googleapis.com", "Google Gemini"),
    ("openrouter.ai", "OpenRouter"),
    ("api.anthropic.com", "Anthropic"),
    ("api.siliconflow.cn", "硅基流动"),
    ("v3.cuiyuanxiaoyuan.com", "Cuiyuanxiaoyuan"),
)


def vendor_label(base_url):
    """A short name for whoever the endpoint belongs to.

    Known hosts are named by their vendor; anything else falls back to the
    host itself, prefixed with 其他 so an unrecognised endpoint is visibly
    unrecognised rather than silently looking like a first-party source.
    """
    url = str(base_url or "").strip().lower()
    if not url:
        return ""
    host = re.sub(r"^[a-z]+://", "", url).split("/")[0].split("@")[-1]
    host = host.split(":")[0]
    for needle, name in VENDOR_HOSTS:
        if host == needle or host.endswith("." + needle):
            return name
    if host.startswith("www."):
        host = host[4:]
    return "其他 (%s)" % host
