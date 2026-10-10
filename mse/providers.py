"""Cloud model providers for Ask: which exist, their API keys, and whether they can be reached right now.

Built in (config.BUILTIN_PROVIDERS): anthropic, the Claude API (Messages, streaming); its key is an Anthropic API key.
Custom: any OpenAI-compatible endpoint, kind "openai-chat" (Chat Completions: DeepSeek, OpenRouter, LM Studio, Ollama
...) or "openai-responses" (a local router, ...). "local" is the MLX worker and not a provider here.

config.json holds, under "ask" → "providers", only what differs from the built-in ({label?, base_url?, models?, ...})
and the custom providers ({kind, label, base_url, models, ...}). Never a key. Optional fields of any provider (see
config.PROVIDER_OPTIONAL): key_file (where its key is kept), training_models (patterns of the models its provider may
use inputs of for training), auto_models (list the models by itself the first time).

Keys live in the login Keychain as generic passwords: service com.mysearch.provider, account = provider id. The
native helper reads and writes them (`msehelper keychain get|has|set|delete <account>`) and takes a new secret on
stdin, so a key never appears in a process list, config.json, a log, an API response or test output. The daemon
reads a key only to make a call, keeps it in memory for KEY_TTL seconds, and reads without Keychain UI (--no-ui):
a background request can never put a dialog on screen. Environment variables (ANTHROPIC_API_KEY, MSE_KEY_<ID>) are a
fallback for the CLI only (allow_env=True): the launchd service sees no shell environment, and the daemon never asks
for one.

A provider's key, in order: a key stored in the Keychain, else the file named by its key_file (read at request time and
cached in memory only until the file's path, mtime, size or inode change; one line of printable characters, at most
4096 bytes read), else (CLI only) its environment variable, else none. The file's content is never copied to
config.json, a log, an API response or test output, and like any key it is only ever sent to that provider's base_url,
over https or to this Mac (can_send_key). GET /api/ask/providers says where a key comes from: key_source = keychain |
file | env (CLI only) | none. `connect` copies a key file's content into the Keychain on request.

Online: a TCP connect to the provider's host (ONLINE_TIMEOUT_S, never a full API call), cached ONLINE_TTL_S seconds
and updated by every cloud call (a network failure marks the host offline at once, a success online).
"""

import ipaddress
import json
import os
import re
import socket
import stat
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import config, paths
from .cloud import CloudError, is_loopback

SERVICE = "com.mysearch.provider"
ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
RESERVED_IDS = {"local", "cloud", "auto", "all"}
KEY_TTL = 600                 # a key read from the Keychain stays in the daemon's memory this long
MISSING_TTL = 30              # "no key" is re-checked after this long
HAS_TTL = 60
ONLINE_TTL_S = 60
AUTOLIST_RETRY_S = 60         # a failed automatic model-list fetch is tried again after this long
AUTOLIST_TIMEOUT_S = 4
ONLINE_TIMEOUT_S = 1.5
ENV_NAMES = {"anthropic": "ANTHROPIC_API_KEY"}              # any other provider: MSE_KEY_<ID>
KEY_FILE_BYTES = 4096
TEST_MESSAGES = [{"role": "system", "content": "This is a connection test. Reply with the single word OK."},
                 {"role": "user", "content": "Connection test: reply OK."}]

WHY = {
    "no_key": "no API key is stored for '{pid}': add it in Settings → Ask, or run `mse providers set-key {pid}`",
    "unknown_provider": "the Ask provider '{pid}' does not exist (Settings → Ask, or `mse providers use`)",
    "no_model": "no model is chosen for '{pid}' (Settings → Ask, or `mse providers use {pid} <model>`)",
    "insecure_url": "refusing to send the key for '{pid}' over plain http to another machine; use an https base_url",
    "training_unconfirmed": "this model's provider may use inputs for training — check the provider's terms, and you have not confirmed that for it "
                            "(Settings › Ask, or `mse providers use {pid} <model> --allow-training`)",
}


class KeychainError(RuntimeError):
    pass


class UnknownProvider(KeyError):
    pass


# ------------------------------------------------------------------ Keychain

class HelperKeychain:
    """The login Keychain through the native helper. A missing helper means no keys (never an exception on reads)."""

    def _call(self, action: str, account: str, secret: str | None = None, ui: bool = False) -> dict:
        if not paths.HELPER.exists():
            raise KeychainError("the native helper is not installed (./scripts/build_helper.sh)")
        cmd = [str(paths.HELPER), "keychain", action, account] + ([] if ui else ["--no-ui"])
        try:
            r = subprocess.run(cmd, input=(secret or "").encode(), capture_output=True, timeout=60 if ui else 15)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise KeychainError(f"the Keychain helper did not answer ({type(e).__name__})") from None
        lines = r.stdout.decode("utf-8", "replace").strip().splitlines()
        try:
            d = json.loads(lines[-1]) if lines else None
        except ValueError:
            d = None
        if not isinstance(d, dict):
            raise KeychainError("the Keychain helper gave no answer")
        if "error" in d:
            msg = str(d["error"])
            if "unknown command keychain" in msg:
                msg = "the installed helper has no Keychain support yet; rebuild it: ./scripts/build_helper.sh"
            raise KeychainError(msg)
        return d

    def get(self, account: str) -> str | None:
        if not paths.HELPER.exists():
            return None
        d = self._call("get", account)
        return d.get("secret") if d.get("found") else None

    def has(self, account: str) -> bool:
        if not paths.HELPER.exists():
            return False
        return bool(self._call("has", account).get("exists"))

    def set(self, account: str, secret: str) -> None:
        self._call("set", account, secret, ui=True)

    def delete(self, account: str) -> bool:
        if not paths.HELPER.exists():
            return False
        return bool(self._call("delete", account, ui=True).get("deleted"))


keychain = HelperKeychain()           # tests replace it with an in-memory fake
_lock = threading.Lock()
_keys: dict[str, tuple[float, str | None]] = {}
_has: dict[str, tuple[float, bool]] = {}
_errors: dict[str, str] = {}
_file_keys: dict[str, tuple] = {}     # key_file path -> (signature, key) as last read; memory only
_autolist: dict[str, float] = {}      # provider id -> when its model list was last fetched automatically
_online: dict[tuple[str, int], tuple[float, bool]] = {}
_probing: set = set()


def forget_cache() -> None:
    with _lock:
        _keys.clear()
        _has.clear()
        _errors.clear()
        _file_keys.clear()
        _autolist.clear()
        _online.clear()


def env_name(pid: str) -> str:
    return ENV_NAMES.get(pid) or "MSE_KEY_" + re.sub(r"[^A-Z0-9]", "_", pid.upper())


def key_file_path(p: dict | None) -> Path | None:
    """The provider's key_file with ~ expanded, or None."""
    kf = (p or {}).get("key_file")
    return Path(kf).expanduser() if isinstance(kf, str) and kf else None


def file_key(p: dict | None) -> str | None:
    """The key in the provider's key_file, read at request time. Cached in memory only and read again when the file
    changes (path, mtime, size or inode); a missing, unreadable or invalid file is None and is looked at again on the
    next call. The key is never logged, returned by the API or written anywhere."""
    path = key_file_path(p)
    if path is None:
        return None
    name = str(path)
    try:
        st = os.stat(path)
    except OSError:
        with _lock:
            _file_keys.pop(name, None)
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    sig = (st.st_mtime_ns, st.st_size, st.st_ino)
    with _lock:
        hit = _file_keys.get(name)
    if hit and hit[0] == sig:
        return hit[1]
    try:
        with open(path, "rb") as f:
            raw = f.read(KEY_FILE_BYTES)
        key = clean_key(raw.decode("utf-8", "replace"))
    except (OSError, ValueError):
        with _lock:
            _file_keys.pop(name, None)
        return None
    with _lock:
        _file_keys[name] = (sig, key)
    return key


def _env_key(pid: str) -> str | None:
    return (os.environ.get(env_name(pid)) or "").strip() or None


def _keychain_get(pid: str) -> str | None:
    now = time.time()
    with _lock:
        hit = _keys.get(pid)
    if hit and now - hit[0] < (KEY_TTL if hit[1] else MISSING_TTL):
        return hit[1]
    val = keychain.get(pid)
    with _lock:
        _keys[pid] = (time.time(), val)
        _has[pid] = (time.time(), bool(val))
    return val


def _keychain_has(pid: str) -> bool:
    now = time.time()
    with _lock:
        k, h = _keys.get(pid), _has.get(pid)
    if k and now - k[0] < (KEY_TTL if k[1] else MISSING_TTL):
        return bool(k[1])
    if h and now - h[0] < HAS_TTL:
        return h[1]
    try:
        found = keychain.has(pid)
    except KeychainError:
        found = False
    with _lock:
        _has[pid] = (time.time(), found)
    return found


def get_key(pid: str, allow_env: bool = False, p: dict | None = None) -> str | None:
    """The provider's key: the Keychain (cached); then the file named by its key_file; for the CLI only, then the
    provider's environment variable. `p`: the provider's entry (read from config.json when not given)."""
    p = p if p is not None else _entry(pid)
    try:
        val = _keychain_get(pid)
    except KeychainError:
        if not file_key(p):
            raise
        val = None                        # the Keychain is unreadable but the key file is not
    if not val:
        val = file_key(p)
    if not val and allow_env:
        val = _env_key(pid)
    return val


def key_source(pid: str, allow_env: bool = False, p: dict | None = None) -> str:
    """Where get_key would take the key from, without reading it out: "keychain", "file" (the provider's key_file),
    "env" (CLI only) or "none"."""
    p = p if p is not None else _entry(pid)
    if _keychain_has(pid):
        return "keychain"
    if file_key(p):
        return "file"
    if allow_env and _env_key(pid):
        return "env"
    return "none"


def has_key(pid: str, allow_env: bool = False, p: dict | None = None) -> bool:
    """Whether a key is available, without handing it out (Keychain attributes only, or the key file)."""
    return key_source(pid, allow_env, p) != "none"


def clean_key(key) -> str:
    """A key as stored: one line of printable characters. The message of a ValueError never contains the key."""
    if not isinstance(key, str):
        raise ValueError("the key must be a string")
    k = key.strip()
    if not k:
        raise ValueError("the key is empty")
    if len(k) > 8192 or any(ord(ch) < 33 or ord(ch) == 127 for ch in k):
        raise ValueError("an API key is one line of printable characters without spaces")
    return k


def set_key(pid: str, key: str) -> None:
    """Store a (clean_key-checked) key in the Keychain and use it from now on."""
    keychain.set(pid, key)
    with _lock:
        _keys[pid] = (time.time(), key)
        _has[pid] = (time.time(), True)
        _errors.pop(pid, None)


def forget_key(pid: str) -> bool:
    gone = keychain.delete(pid)
    with _lock:
        _keys[pid] = (time.time(), None)
        _has[pid] = (time.time(), False)
        _errors.pop(pid, None)
    return gone


# ------------------------------------------------------------------ the providers

def _settings() -> dict:
    from . import ask
    return ask.settings()


def all_providers(s: dict | None = None) -> dict:
    """id -> {kind, label, base_url, models, builtin}: the built-ins with the user's overrides, then custom ones.
    `s`: ask.settings(), which carries this view as "providers"; None reads config.json."""
    if s is not None and isinstance(s.get("providers"), dict):
        return s["providers"]
    return config.provider_entries(config.load())


def get(pid: str, s: dict | None = None) -> dict:
    p = all_providers(s).get(pid)
    if p is None:
        raise UnknownProvider(pid)
    return p


def _entry(pid: str) -> dict | None:
    try:
        return get(pid, None)
    except UnknownProvider:
        return None


def _host(p: dict) -> str:
    return urlsplit(p["base_url"]).hostname or ""


def _private(host: str) -> bool:
    if is_loopback(host) or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return ip.is_private or ip.is_link_local


def needs_key(pid: str, p: dict) -> bool:
    """Built-ins always do; so does a provider with a key_file (the key is how the user says it is needed); a custom
    endpoint on this Mac or the local network (LM Studio, Ollama) may run without."""
    return bool(p.get("builtin")) or bool(p.get("key_file")) or not _private(_host(p))


def can_send_key(p: dict) -> bool:
    """A key goes over https, or over http only to this Mac."""
    u = urlsplit(p["base_url"])
    return u.scheme == "https" or is_loopback(u.hostname)


def target(s: dict, allow_env: bool = False) -> tuple[dict | None, str | None]:
    """The active cloud provider ready to call -> ({"provider", "kind", "base_url", "label", "model", "gen"}, None),
    or (None, why): "unknown_provider", "no_model", "no_key", "insecure_url" or "keychain: …"."""
    from . import cloud
    pid = s.get("provider")
    p = all_providers(s).get(pid)
    if p is None:
        return None, "unknown_provider"
    model = s.get("model") or (p["models"][0] if p["models"] else None)
    if not model:
        return None, "no_model"
    if config.trains_on_inputs(model, p) and model not in (s.get("allow_training_models") or []):
        return None, "training_unconfirmed"
    try:
        key = get_key(pid, allow_env, p)
    except KeychainError as e:
        return None, f"keychain: {e}"
    if not key and needs_key(pid, p):
        return None, "no_key"
    if key and not can_send_key(p):
        return None, "insecure_url"
    return {"provider": pid, "kind": p["kind"], "base_url": p["base_url"], "label": p["label"], "model": model,
            "gen": cloud.generator(p, key, model, s)}, None


def why_message(why: str, pid: str) -> str:
    if why.startswith("keychain:"):
        return f"the key for '{pid}' could not be read from the Keychain ({why[9:].strip()})"
    if why == "no_key" and (_entry(pid) or {}).get("key_file"):
        shown = str(_entry(pid)["key_file"]).replace(str(Path.home()), "~", 1)
        return (f"no API key for '{pid}': its key file {shown} is missing or does not hold a key "
                f"(or store one: `mse providers set-key {pid}`)")
    return WHY.get(why, why).format(pid=pid)


# ------------------------------------------------------------------ online

def _tcp_probe(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


PROBE = _tcp_probe                    # tests replace it; nothing in the test suite touches the network


def _hostport(base_url: str) -> tuple[str, int]:
    u = urlsplit(base_url)
    return (u.hostname or "", u.port or (443 if u.scheme == "https" else 80))


def _probe(key: tuple[str, int]) -> bool:
    """PROBE in a helper thread, so a slow DNS lookup cannot hold the caller longer than ONLINE_TIMEOUT_S."""
    box: dict = {}

    def run():
        try:
            box["ok"] = bool(PROBE(key[0], key[1], ONLINE_TIMEOUT_S))
        except Exception:
            box["ok"] = False
    th = threading.Thread(target=run, daemon=True, name="ask-online")
    th.start()
    th.join(ONLINE_TIMEOUT_S + 0.2)
    ok = box.get("ok", False)
    with _lock:
        _online[key] = (time.time(), ok)
        _probing.discard(key)
    return ok


def online(base_url: str, block: bool = True) -> bool | None:
    """Can the provider's host be reached? Cached ONLINE_TTL_S. block=False (GET /api/status) never waits: it returns
    the last known answer (None before the first check) and refreshes it in the background."""
    key = _hostport(base_url)
    if not key[0]:
        return False
    with _lock:
        hit = _online.get(key)
    if hit and time.time() - hit[0] < ONLINE_TTL_S:
        return hit[1]
    if not block:
        with _lock:
            start = key not in _probing
            _probing.add(key)
        if start:
            threading.Thread(target=_probe, args=(key,), daemon=True, name="ask-online-bg").start()
        return hit[1] if hit else None
    return _probe(key)


def note_success(pid: str, base_url: str | None) -> None:
    with _lock:
        _errors.pop(pid, None)
        if base_url:
            _online[_hostport(base_url)] = (time.time(), True)


def note_failure(pid: str, base_url: str | None, err: BaseException) -> None:
    """After a failed cloud call: a network failure marks the host offline (the next question goes local at once
    under route auto); any other failure is remembered as the provider's error until a call succeeds."""
    with _lock:
        if getattr(err, "network", False) and base_url:
            _online[_hostport(base_url)] = (time.time(), False)
        else:
            _errors[pid] = str(err)[:300]


# ------------------------------------------------------------------ the API's view

def offline_hint(p: dict) -> str | None:
    """What to do about a provider that cannot be reached: only a server on this Mac can be started by the user."""
    u = urlsplit(p["base_url"])
    if is_loopback(u.hostname):
        return f"Start the server on this Mac ({u.hostname}:{u.port or (443 if u.scheme == 'https' else 80)})."
    return None


def row(pid: str, p: dict, online_now: bool | None = None, allow_env: bool = False) -> dict:
    """One provider for GET /api/ask/providers. Never the key, nor any part of it. key_source says where a key comes
    from (keychain | file | env | none); hint (offline only) says what to do about it; key_file is the path the user
    configured (not its content), training_models the patterns of the models the provider may train on."""
    src = key_source(pid, allow_env, p)
    hk = src != "none"
    nk = needs_key(pid, p)
    with _lock:
        err = _errors.get(pid)
    if nk and not hk:
        st = "no_key"
    elif online_now is False:
        st = "offline"
    elif err:
        st = "error"
    else:
        st = "ok"
    return {"id": pid, "label": p["label"], "kind": p["kind"], "base_url": p["base_url"], "builtin": p["builtin"],
            "has_key": hk, "key_source": src, "needs_key": nk, "models": list(p["models"]), "status": st,
            "model_info": dict(p.get("models_meta") or {}), "models_at": p.get("models_at"),
            "key_file": p.get("key_file"), "training_models": list(p.get("training_models") or []),
            "auto_models": bool(p.get("auto_models")),
            **({"error": err} if st == "error" else {}),
            **({"hint": offline_hint(p)} if st == "offline" and offline_hint(p) else {})}


def _autolist_models(pid: str, p: dict, reachable, allow_env: bool) -> bool:
    """A provider with auto_models has its model list loaded by itself the first time the providers are looked at (so the
    picker holds every model it offers without "Refresh list"): once a key is at hand, the endpoint answers and no list
    was ever fetched. A failed attempt is repeated after AUTOLIST_RETRY_S. True when the stored list changed."""
    if not p.get("auto_models") or p.get("models_at") or reachable is not True or not has_key(pid, allow_env, p):
        return False
    now = time.time()
    with _lock:
        last = _autolist.get(pid)
        if last is not None and now - last < AUTOLIST_RETRY_S:
            return False
        _autolist[pid] = now
    try:
        refresh_models(pid, allow_env, timeout=AUTOLIST_TIMEOUT_S)
    except (CloudError, ValueError, KeychainError, UnknownProvider):
        return False
    return True


def list_view(allow_env: bool = False) -> dict:
    """GET /api/ask/providers: {route, provider, model, online, providers: [row]}. Hosts of the providers that can be
    called are checked in parallel (at most ONLINE_TIMEOUT_S, cached). The model list of a provider with auto_models is
    fetched here the first time (see _autolist_models)."""
    s = _settings()
    provs = all_providers(s)
    usable = {pid: p for pid, p in provs.items() if has_key(pid, allow_env, p) or not needs_key(pid, p)}
    found: dict = {}
    threads = []
    for pid, p in usable.items():
        th = threading.Thread(target=lambda pid=pid, p=p: found.__setitem__(pid, online(p["base_url"])), daemon=True)
        th.start()
        threads.append(th)
    for th in threads:
        th.join(ONLINE_TIMEOUT_S + 0.5)
    if any(_autolist_models(pid, p, found.get(pid), allow_env) for pid, p in provs.items()):
        s = _settings()
        provs = all_providers(s)
    return {"route": s["route"], "provider": s["provider"], "model": s["model"], "online": found.get(s["provider"]),
            "allow_training_models": list(s.get("allow_training_models") or []),
            "local_fallback": bool(s.get("local_fallback")),
            "training_warning": config.TRAINING_WARNING,
            "providers": [row(pid, p, found.get(pid), allow_env) for pid, p in provs.items()]}


# ------------------------------------------------------------------ changes (POST /api/ask/providers ...)

def clean_url(u) -> str:
    if not isinstance(u, str) or not u.strip():
        raise ValueError("base_url is required, e.g. https://api.deepseek.com/v1 or http://127.0.0.1:11434/v1")
    u = u.strip().rstrip("/")
    s = urlsplit(u)
    if s.scheme not in ("http", "https") or not s.hostname:
        raise ValueError("base_url must be an http:// or https:// URL")
    if s.username or s.password or s.query or s.fragment:
        raise ValueError("base_url must not carry credentials, a query or a fragment (keys go to the Keychain)")
    if len(u) > 500:
        raise ValueError("base_url is too long")
    return u


def clean_models(models) -> list[str]:
    if isinstance(models, str):
        models = models.split(",")
    if not isinstance(models, list):
        raise ValueError("models must be a list of model ids")
    out = []
    for m in models:
        if not isinstance(m, str):
            raise ValueError("models must be a list of model ids")
        m = m.strip()
        if not m:
            continue
        if len(m) > 200 or any(ord(ch) < 32 for ch in m):
            raise ValueError("a model id is one line of at most 200 characters")
        if m not in out:
            out.append(m)
    if len(out) > 500:
        raise ValueError("at most 500 models per provider")
    return out


ALLOWED = {"id", "label", "kind", "base_url", "key", "models", "forget_key", "key_file", "training_models", "auto_models"}
OPTIONAL_FIELDS = ("models_meta", "models_at", "key_file", "training_models", "auto_models")


def save(body) -> dict:
    """Create or update a provider, store its key in the Keychain. {id, label?, kind?, base_url?, key?, models?,
    forget_key?, key_file?, training_models?, auto_models?} -> its row. Built-ins keep their kind. key_file: "" or null
    removes it. ValueError with a readable message for anything invalid."""
    if not isinstance(body, dict):
        raise ValueError("send a JSON object: {id, label?, kind?, base_url?, key?, models?, key_file?, training_models?}")
    extra = set(body) - ALLOWED
    if extra:
        raise ValueError(f"unknown field{'s' if len(extra) > 1 else ''}: {', '.join(sorted(extra))}")
    pid = body.get("id")
    if not isinstance(pid, str) or not ID_RE.match(pid) or pid in RESERVED_IDS:
        raise ValueError("id must be a short slug (lowercase letters, digits, single dashes), not "
                         + ", ".join(sorted(RESERVED_IDS)))
    builtin = config.BUILTIN_PROVIDERS.get(pid)
    key = body.get("key")
    with config._lock:
        cfg = config.load()
        raw = (cfg.get("ask") or {}).get("providers") or {}
        cur = dict(raw.get(pid) or {}) if isinstance(raw.get(pid), dict) else {}
        if builtin:
            if "kind" in body and body["kind"] != builtin["kind"]:
                raise ValueError(f"'{pid}' is built in: its kind stays {builtin['kind']}")
            entry = {k: v for k, v in cur.items() if k in ("label", "base_url", "models") + OPTIONAL_FIELDS}
        else:
            kind = body.get("kind", cur.get("kind"))
            if kind not in config.PROVIDER_KINDS:
                raise ValueError(f"kind must be one of: {', '.join(config.PROVIDER_KINDS)}")
            entry = {"kind": kind, "label": cur.get("label") or pid,
                     "base_url": clean_url(body.get("base_url", cur.get("base_url"))),
                     "models": list(cur.get("models") or []),
                     **{k: cur[k] for k in OPTIONAL_FIELDS if cur.get(k)}}
        if "label" in body:
            label = body["label"]
            if label is not None and (not isinstance(label, str) or len(label.strip()) > 80):
                raise ValueError("label must be a short text (at most 80 characters)")
            if label and label.strip():
                entry["label"] = label.strip()
            elif builtin:
                entry.pop("label", None)
        if "base_url" in body:
            entry["base_url"] = clean_url(body["base_url"])
        if "models" in body:
            entry["models"] = clean_models(body["models"])
        if "key_file" in body:
            kf = config.check_key_file(body["key_file"])
            entry.pop("key_file", None) if kf is None else entry.__setitem__("key_file", kf)
        if "training_models" in body:
            tm = config.check_training_models(body["training_models"])
            entry.pop("training_models", None) if not tm else entry.__setitem__("training_models", tm)
        if "auto_models" in body:
            if not isinstance(body["auto_models"], bool):
                raise ValueError("auto_models must be true or false")
            entry.pop("auto_models", None) if not body["auto_models"] else entry.__setitem__("auto_models", True)
        merged = {**(builtin or {}), **entry, "builtin": bool(builtin)}
        new_key = clean_key(key) if key not in (None, "") else None
    if (new_key or (body.get("forget_key") is not True and has_key(pid, p=merged))) and not can_send_key(merged):
        raise ValueError("a key is only ever sent over https (or http to this Mac): use an https base_url")
    # the Keychain first, outside the config lock (it may show a dialog the user has to answer): a Keychain
    # failure leaves the configuration unchanged
    if new_key:
        set_key(pid, new_key)
    elif body.get("forget_key") is True:
        forget_key(pid)
    with config._lock:
        cfg = config.load()
        raw = dict((cfg.get("ask") or {}).get("providers") or {})
        if builtin and not entry:
            raw.pop(pid, None)
        else:
            raw[pid] = entry
        cfg["ask"] = {**(cfg.get("ask") or {}), "providers": raw}
        config.save(cfg)
    return row(pid, get(pid, None))


def remove(pid: str, forget: bool = False) -> dict:
    """Remove a custom provider (forget=True also deletes its Keychain item). The active provider falls back to
    anthropic and its first model."""
    if pid in config.BUILTIN_PROVIDERS:
        raise ValueError(f"'{pid}' is built in and cannot be removed (forget its key instead)")
    with config._lock:
        cfg = config.load()
        a = cfg.get("ask") or {}
        raw = dict(a.get("providers") or {})
        if pid not in raw:
            raise UnknownProvider(pid)
        raw.pop(pid)
        a = {**a, "providers": raw}
        if a.get("provider") == pid:
            a["provider"] = "anthropic"
            a["model"] = config.BUILTIN_PROVIDERS["anthropic"]["models"][0]
        cfg["ask"] = a
        config.save(cfg)
    gone = forget_key(pid) if forget else False
    s = _settings()
    return {"ok": True, "removed": pid, "key_forgotten": gone, "provider": s["provider"], "model": s["model"]}


def _ready(pid: str, allow_env: bool) -> tuple[dict, str | None]:
    p = get(pid, None)
    key = get_key(pid, allow_env, p)
    if not key and needs_key(pid, p):
        raise ValueError(why_message("no_key", pid))
    if key and not can_send_key(p):
        raise ValueError(why_message("insecure_url", pid))
    return p, key


def _store_models(pid: str, rows: list[dict], at: float) -> list[str]:
    """Replace the provider's model list with what its endpoint listed (and the names that came with it)."""
    ids = [r["id"] for r in rows if len(r["id"]) <= 200 and not any(ord(ch) < 32 for ch in r["id"])][:1500]
    keep = set(ids)
    meta = {r["id"]: {k: v for k, v in r.items() if k != "id"} for r in rows if r["id"] in keep and len(r) > 1}
    with config._lock:
        cfg = config.load()
        a = dict(cfg.get("ask") or {})
        raw = dict(a.get("providers") or {})
        entry = dict(raw.get(pid) or {})
        if pid not in config.BUILTIN_PROVIDERS and not entry.get("base_url"):
            raise UnknownProvider(pid)
        entry["models"] = ids
        entry["models_at"] = at
        if meta:
            entry["models_meta"] = meta
        else:
            entry.pop("models_meta", None)
        raw[pid] = entry
        cfg["ask"] = {**a, "providers": raw}
        config.save(cfg)
    return ids


def refresh_models(pid: str, allow_env: bool = False, timeout: float = 15) -> dict:
    """Replace the provider's model list with every model its models endpoint lists: Claude API GET /v1/models
    (all pages), any OpenAI-compatible /models. Nothing is added that the endpoint did not list. -> {id, models, fetched, at, ms}"""
    p, key = _ready(pid, allow_env)
    t = time.time()
    try:
        from .cloud import list_models_detail
        rows = list_models_detail(p, key, timeout)
    except CloudError as e:
        note_failure(pid, p["base_url"], e)
        raise
    note_success(pid, p["base_url"])
    at = round(time.time(), 1)
    ids = _store_models(pid, rows, at)
    return {"id": pid, "models": ids, "fetched": len(ids), "at": at, "ms": round((time.time() - t) * 1000)}


def connect(pid: str, allow_env: bool = False) -> dict:
    """The user pressed "Connect" on a provider that has a key_file: this process reads the key file, stores the key in
    the Keychain (where it then wins over the file, until it is forgotten) and lists the provider's models. The key is
    never returned, logged or put in an error message. -> {connected, file, fetched?, models?, models_error?}"""
    p = get(pid, None)
    path = key_file_path(p)
    if path is None:
        raise ValueError(f"'{pid}' has no key_file: set one first (`mse providers add {pid} --key-file PATH`)")
    shown = str(p["key_file"]).replace(str(Path.home()), "~", 1)
    try:
        with open(path, "rb") as f:
            raw = f.read(KEY_FILE_BYTES)
    except FileNotFoundError:
        raise ValueError(f"there is no key file at {shown}") from None
    except OSError as e:
        raise ValueError(f"the key file {shown} could not be read ({type(e).__name__})") from None
    try:
        key = clean_key(raw.decode("utf-8", "replace"))
    except ValueError:
        raise ValueError(f"{shown} does not hold an API key (one line of printable characters)") from None
    if not can_send_key(p):
        raise ValueError(why_message("insecure_url", pid))
    set_key(pid, key)
    out = {"connected": True, "file": shown}
    del key, raw
    try:
        out.update({k: v for k, v in refresh_models(pid, allow_env).items() if k != "id"})
    except CloudError as e:
        out["models_error"] = str(e)          # the key is stored; the server may simply not be running
    return out


def test(pid: str, model: str | None = None, allow_env: bool = False) -> dict:
    """One tiny request ("reply OK") -> {ok, model, ms, error?}. Only ever run when the user asks for it."""
    from . import cloud
    if model is not None and (not isinstance(model, str) or len(model) > 200):
        raise ValueError("model must be a model id")
    p = get(pid, None)
    s = _settings()
    model = model or (s["model"] if s["provider"] == pid else (p["models"][0] if p["models"] else None))
    if not model:
        return {"ok": False, "model": None, "ms": 0, "error": why_message("no_model", pid)}
    try:
        p, key = _ready(pid, allow_env)
    except (ValueError, KeychainError) as e:
        return {"ok": False, "model": model, "ms": 0, "error": str(e)}
    t = time.time()
    try:
        for _ in cloud.stream(p, key, model, TEST_MESSAGES, max_tokens=256, temperature=0.0,
                              effort=s.get("cloud_effort"), timeout=30):
            pass
    except CloudError as e:
        note_failure(pid, p["base_url"], e)
        return {"ok": False, "model": model, "ms": round((time.time() - t) * 1000), "error": str(e)}
    note_success(pid, p["base_url"])
    return {"ok": True, "model": model, "ms": round((time.time() - t) * 1000)}
