"""Settings and the list of collections (one indexed folder = one collection = one database)."""

import hashlib
import importlib
import json
import os
import re
import secrets
import sys
import threading
import time
from copy import deepcopy
from pathlib import Path

from . import paths

DEFAULTS = {
    "version": 1,
    "port": 8737,
    # where MySearch.app's --daemon supervisor finds the Python service (<project>/.venv/bin/python -m mse serve)
    "service": {"project": str(paths.PROJECT)},
    "token": None,
    "model": {"path": str(paths.DEFAULT_MODEL), "text_path": str(paths.DEFAULT_MODEL) + "-text", "dims": 768,
              "reranker": str(paths.MODELS / "qwen3-reranker-0.6b-8bit"),
              "asr": {"path": str(paths.MODELS / "whisper-large-v3-turbo-8bit"),
                      "processor": str(paths.MODELS / "whisper-large-v3-turbo-8bit")}},
    "schedule": {
        "scan_every_min": 60,        # safety-net rescan; file events index changes within seconds
        "nightly_at": "02:30",       # heavy work: OCR, page images, audio/video, transcripts, re-embedding
        "nightly_enabled": True,
        "only_on_ac_power": True,
        "live_embed_limit": 400,     # embed small daytime changes right away
        "live_heavy_limit": 5,       # OCR / picture / page work for a few new files right away (on AC)
        "learn_nightly": True,       # after the night sync: measure search quality, re-tune, apply if safe (learn.py)
    },
    "worker": {"idle_unload_min": 10, "batch_text": 16, "batch_media": 4},
    # 1-bit index + 16x exact re-scoring: 99% identical top-10 to exact search on real data, ~10x faster.
    "search": {"limit": 20, "vector_mode": "binary", "oversample": 16,
               # Cross-encoder re-ranking for `precise` requests (rank fusion, close calls only, see rerank.py):
               # a 28-question private gold set: MRR 0.896 -> 0.946, ~0.25 s added on average. Ignored while False.
               "rerank_enabled": True,
               # re-ranker knobs (rerank.py GATE / W_RETRIEVAL), tuned with the fusion weights by `mse tune`
               "rerank_gate": 0.8, "rerank_weight": 0.75,
               # overview files (SUMMARY, README, INDEX, TODO …) score x this; tuned by `mse tune` with the weights
               "hub_penalty": 0.6},
    "translation": {"enabled": True, "languages": ["en", "de"], "corpus_bridge": True},
    # Re-ranker learning from favourites (finetune.py): a nightly LoRA fine-tune once there are enough favourites,
    # active only when it measurably helps (held-out favourites MRR up, gold sets held, latency kept). collections:
    # None = one shared adapter learned from every collection's favourites, or a list of collection names.
    "finetune": {"enabled": True, "min_examples": 300, "min_queries": 100, "min_new": 25, "collections": None,
                 "fav_min_gain": 0.02, "gold_max_loss": 0.01, "max_latency_increase": 0.10, "max_minutes": 15},
    # What agents (MCP, piped CLI) receive: personal identifiers masked; the app always shows everything.
    "agents": {"redact": True, "default_scope": "cwd"},
    # Whole-Mac file-name search (mse/macindex.py): names only, never contents. `exclude` is the editable part of the
    # privacy rules (credential stores, secrets, browser profiles, Mail, Messages ... are excluded in code and cannot
    # be switched off); `skip_protected` keeps the crawl out of the folders macOS asks consent for until Full Disk
    # Access is granted (auto) -- always | never | auto.
    "mac_index": {"enabled": True, "roots": [str(Path.home())], "exclude": ["~/Library/"],
                  "skip_protected": "auto", "rescan_every_h": 24},
    # Ask mode (mse/ask.py): a short cited answer from the top results, by a cloud model while online (see
    # ASK_CLOUD_DEFAULTS below) or the small local model. Off: no model is called or loaded; switching off unloads the
    # local one at once. Its other knobs (route, provider, model, local_model, max_tokens, ...) default in
    # ASK_CLOUD_DEFAULTS / ask.ASK_DEFAULTS and can be overridden here; config.json keeps only what was changed.
    "ask": {"enabled": False},
    "collections": [],
    # Named sets of collections searched together, e.g. {"name": "work", "label": "Work",
    # "collections": ["notes", "projects"]}. A group searches exactly its members (in_global is ignored).
    "groups": [],
}

# ---- Ask with cloud models (mse/providers.py, mse/cloud.py). These are defaults only: they are never written into
# config.json, which keeps what the user changed under "ask" (and custom providers under "ask" → "providers").
ASK_ROUTES = ("auto", "cloud", "local")
PROVIDER_KINDS = ("anthropic", "openai-chat", "openai-responses")
ASK_CLOUD_DEFAULTS = {
    # "cloud" (default): the chosen cloud model answers; if it cannot, the question gets an error, never the local model
    # (unless local_fallback). "local": the local model answers, nothing leaves the Mac. "auto" is the old name of
    # cloud + local_fallback and is still read that way. The local model (2.5 GB) loads only when the user chose it.
    "route": "cloud",
    "local_fallback": False,           # true: use the local model when no cloud model is reachable (the old "auto")
    "provider": "anthropic",           # the active cloud provider (BUILTIN_PROVIDERS or a custom one)
    "model": "claude-opus-5-5",        # the active cloud model
    "cloud_redact": True,              # mask personal identifiers (mse/redact.py) in passages + history before they leave
    "cloud_max_tokens": 8192,          # cloud models may think before they answer; the answer itself stays short
    "cloud_effort": "low",             # output_config.effort for the Claude models that take it (short cited answers)
    "cloud_timeout_s": 60,             # per network read while a cloud answer streams
    # model ids the user confirmed may be selected although the provider may use inputs for training (a model matched
    # by its provider's training_models, see trains_on_inputs); a model of that kind that is not listed here never
    # answers. Empty by default.
    "allow_training_models": [],
}
# Fields of a provider entry in config.json (ask → providers → <id>). A custom provider needs kind and base_url; every
# other field is optional. The built-in one (anthropic) takes the same optional fields as overrides.
#   kind             anthropic | openai-chat | openai-responses
#   label, base_url, models   what the pickers show; models is the list last fetched or typed
#   key_file         path of a local file that holds the API key (~ is expanded). Read when a call needs it, never copied
#                    into config.json or a log, only ever sent to this provider's base_url. A key in the Keychain wins
#   training_models  glob patterns (* and ?, case-insensitive) of the model ids whose provider may use inputs for
#                    training: such a model is marked with a warning and answers only after a one-time confirmation
#   auto_models      true: the model list is fetched once by itself when the provider is first looked at
PROVIDER_OPTIONAL = ("models_meta", "models_at", "key_file", "training_models", "auto_models")
BUILTIN_PROVIDERS = {
    # the four ids are the current Claude models of the Anthropic API (claude-api skill, models table, 2026-10); this is
    # only the list before the first "Refresh list", which replaces it with what GET /v1/models returns for the key
    "anthropic": {"kind": "anthropic", "label": "Claude (Anthropic API)", "base_url": "https://api.anthropic.com/v1",
                  "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"]},
}
TRAINING_WARNING = "The provider may use inputs for training — check the provider's terms"


def check_training_models(v) -> list[str]:
    """A provider's training_models: a list of at most 20 glob patterns (* = any characters, ? = one character)."""
    if isinstance(v, str):
        v = v.split(",")
    if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
        raise ValueError("training_models must be a list of model-id patterns such as [\"*-train\"]")
    out = []
    for x in v:
        x = x.strip()
        if not x:
            continue
        if len(x) > 200 or any(ord(ch) < 32 for ch in x):
            raise ValueError("a training_models pattern is one line of at most 200 characters")
        if x not in out:
            out.append(x)
    if len(out) > 20:
        raise ValueError("at most 20 training_models patterns")
    return out


def _glob_re(pattern: str):
    return re.compile("".join(".*" if ch == "*" else "." if ch == "?" else re.escape(ch) for ch in pattern),
                      re.I | re.S)


def trains_on_inputs(model, provider: dict | None = None) -> bool:
    """Whether `provider` (a provider entry or API row carrying training_models) marks `model` as one whose inputs and
    outputs its provider may use for training. Such a model needs a one-time confirmation (ask.allow_training_models)
    before it can answer. No provider, no patterns, no warning."""
    if not isinstance(model, str) or not model or not isinstance(provider, dict):
        return False
    pats = provider.get("training_models")
    return isinstance(pats, list) and any(isinstance(x, str) and x and _glob_re(x).fullmatch(model) for x in pats)


def local_chat_models() -> list[dict]:
    """The MLX models in <data>/models that can write Ask answers: a folder with config.json and weights, a causal or
    conditional-generation architecture, and not one of the other roles (embeddings, re-ranker, speech). Detected by
    their files only: nothing is loaded. -> [{"name", "path", "size_mb"}] sorted by name."""
    out = []
    root = paths.MODELS
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return out
    for d in entries:
        n = d.name.lower()
        if not d.is_dir() or n.startswith(".") or any(x in n for x in ("rerank", "embed", "whisper", "lora")):
            continue
        weights = list(d.glob("*.safetensors"))
        try:
            c = json.loads((d / "config.json").read_text())
        except (OSError, ValueError):
            continue
        arch = " ".join(c.get("architectures") or []) if isinstance(c, dict) else ""
        if not weights or not re.search(r"(CausalLM|ConditionalGeneration)$", arch.split(" ")[0] if arch else ""):
            continue
        out.append({"name": d.name, "path": str(d),
                    "size_mb": round(sum(w.stat().st_size for w in weights) / 1048576)})
    return out

COLLECTION_DEFAULTS = {
    "name": "",
    "path": "",
    "description": "",
    "in_global": True,
    "enabled": True,
    "ask_cloud": True,            # Ask may send this folder's passages to a cloud model; False: always the local model
    "exclude": [],
    "metadata_only": [],
    "contexts": {},
    "media": {
        "images": True,           # OCR + image embedding
        "pdf_pages": "smart",     # off | smart (scanned pages + page 1) | all
        "max_pdf_pages": 30,
        "audio": True,
        "video": True,
        "transcribe": True,       # speech → text with timestamps (Whisper), for audio and video
        "max_media_minutes": 90,
    },
    "max_file_mb": 200,
}

# Per-folder starting rules, keyed by the folder's name, read from <data>/presets.json when a folder is added.
# docs/presets.example.json shows the format. No file, no presets.
PRESETS_FILE = "presets.json"


def presets() -> dict:
    """{folder name: {"description", "exclude", "metadata_only", "contexts"}} from <data>/presets.json, or {}."""
    path = paths.DATA / PRESETS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        print(f"mse: presets not loaded from {path}: {e}", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


_lock = threading.RLock()


def slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    return s or "folder"


def _merge(base: dict, extra: dict) -> dict:
    out = deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


_legacy = None                 # the optional upgrade module (mse/legacy_providers.py), False when this tree has none
_backed_up = None              # sha256 of the config.json bytes last copied to backups/ by this process
_warned: set = set()           # upgrade problems already printed by this process (a request must not repeat them)


def _write_raw(raw: dict) -> None:
    tmp = paths.CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False))
    os.chmod(tmp, 0o600)
    os.replace(tmp, paths.CONFIG)


def _warn(msg: str) -> None:
    if msg not in _warned:
        _warned.add(msg)
        print(msg, file=sys.stderr)


def _upgrade(raw: dict) -> dict:
    """Run the installation's optional one-time upgrade of config.json, if this tree ships one. Before the file is
    touched it is copied to <data>/backups/config.before-<tag>-<timestamp>.json; the upgrade only adds keys. Without
    that copy nothing is written and the upgraded view applies in memory only. Never fatal."""
    global _legacy, _backed_up
    if _legacy is None:
        try:
            _legacy = importlib.import_module(f"{__package__}.legacy_providers")
        except ModuleNotFoundError as e:
            if e.name != f"{__package__}.legacy_providers":
                raise
            _legacy = False
    if not _legacy:
        return raw
    try:
        new = _legacy.migrate(raw)
        if new is None:
            return raw
        try:
            before = paths.CONFIG.read_bytes()
            digest = hashlib.sha256(before).hexdigest()
            if digest != _backed_up:                       # a write that keeps failing does not copy the same file again
                backups = paths.DATA / "backups"
                backups.mkdir(parents=True, exist_ok=True)
                stem = f"config.before-{_legacy.TAG}-{time.strftime('%Y%m%d-%H%M%S')}"
                for n in range(1000):                      # never over an existing copy (a second process, same second)
                    copy = backups / (stem + (f"-{n}" if n else "") + ".json")
                    try:
                        with open(copy, "xb") as f:
                            f.write(before)
                        break
                    except FileExistsError:
                        continue
                os.chmod(copy, 0o600)
                _backed_up = digest
            _write_raw(new)
        except OSError as e:
            _warn(f"mse: config upgrade applied in memory only, the file was not changed: {e}")
        return new
    except Exception as e:                       # an upgrade must never stop the configuration from loading
        _warn(f"mse: config upgrade skipped: {type(e).__name__}: {e}")
        return raw


def load() -> dict:
    paths.ensure_dirs()
    with _lock:
        cfg = {}
        if paths.CONFIG.exists():
            cfg = _upgrade(json.loads(paths.CONFIG.read_text()))
        cfg = _merge(DEFAULTS, cfg)
        cfg["collections"] = [_merge(COLLECTION_DEFAULTS, c) for c in cfg.get("collections", [])]
        cfg["groups"] = [_norm_group(g) for g in cfg.get("groups") or [] if isinstance(g, dict) and g.get("name")]
        if not cfg.get("token"):
            cfg["token"] = secrets.token_urlsafe(24)
            save(cfg)
        return cfg


def save(cfg: dict) -> None:
    with _lock:
        tmp = paths.CONFIG.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
        os.chmod(tmp, 0o600)
        os.replace(tmp, paths.CONFIG)


def get_collection(cfg: dict, name: str) -> dict | None:
    for c in cfg["collections"]:
        if c["name"] == name:
            return c
    return None


def collection_for_path(cfg: dict, p: str | Path) -> dict | None:
    """The collection whose folder contains `p` (deepest match)."""
    p = str(Path(p).expanduser().resolve())
    best = None
    for c in cfg["collections"]:
        root = c["path"].rstrip("/")
        if p == root or p.startswith(root + "/"):
            if best is None or len(root) > len(best["path"]):
                best = c
    return best


def add_collection(path: str, name: str | None = None, **settings) -> dict:
    with _lock:
        cfg = load()
        root = Path(path).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"not a folder: {root}")
        name = slugify(name or root.name)
        if get_collection(cfg, name):
            raise ValueError(f"a collection named '{name}' already exists")
        if name in RESERVED_SCOPE_NAMES:
            raise ValueError(f"'{name}' is reserved (a search filter word or 'all'); choose another name with --name")
        if get_group(cfg, name):
            raise ValueError(f"'{name}' is already used by a group; choose another name with --name")
        if (owner := alias_owner(cfg, name)):
            raise ValueError(f"'{name}' is already a shortcut for the {owner[0]} '{owner[1]}'; "
                             "choose another name with --name")
        for c in cfg["collections"]:
            other = Path(c["path"])
            if root == other or other in root.parents or root in other.parents:
                raise ValueError(f"overlaps with collection '{c['name']}' ({other}); collections must not nest")
        coll = _merge(COLLECTION_DEFAULTS, presets().get(root.name, {}))
        coll = _merge(coll, settings)
        coll.update(name=name, path=str(root), created=time.strftime("%Y-%m-%dT%H:%M:%S"))
        cfg["collections"].append(coll)
        save(cfg)
        return coll


def update_collection(name: str, changes: dict) -> dict:
    allowed = {"description", "in_global", "enabled", "exclude", "metadata_only", "contexts", "media", "max_file_mb",
               "aliases", "ask_cloud"}
    with _lock:
        cfg = load()
        coll = get_collection(cfg, name)
        if not coll:
            raise KeyError(name)
        for k, v in changes.items():
            if k not in allowed:
                raise ValueError(f"cannot change '{k}'")
            if k == "ask_cloud" and not isinstance(v, bool):
                raise ValueError("ask_cloud must be true or false")
            if k == "aliases":                 # short names for the scope shortcut ("w: invoice"); [] removes them
                aliases = check_aliases(cfg, v, "folder", name)
                if aliases:
                    coll["aliases"] = aliases
                else:
                    coll.pop("aliases", None)
                continue
            coll[k] = _merge(coll[k], v) if isinstance(v, dict) and k == "media" else v
        save(cfg)
        return coll


def remove_collection(name: str) -> dict:
    with _lock:
        cfg = load()
        coll = get_collection(cfg, name)
        if not coll:
            raise KeyError(name)
        cfg["collections"] = [c for c in cfg["collections"] if c["name"] != name]
        for g in cfg.get("groups") or []:          # a group left empty stays (and shows as empty)
            g["collections"] = [m for m in g["collections"] if m != name]
        save(cfg)
        return coll


def update_settings(changes: dict) -> dict:
    allowed = {"schedule", "worker", "search", "translation", "agents", "ask", "finetune", "mac_index"}
    with _lock:
        cfg = load()
        old_local = (cfg.get("ask") or {}).get("local_model")
        for k, v in changes.items():
            if k not in allowed or not isinstance(v, dict):
                raise ValueError(f"cannot change '{k}'")
            if k == "ask":
                v = check_ask(v, cfg)
            if k == "mac_index":
                from . import macindex
                v = macindex.check_settings(v)
            cfg[k] = _merge(cfg[k], v)
        save(cfg)
    if "ask" in changes and (not cfg["ask"].get("enabled", False)
                             or ("local_model" in changes["ask"] and cfg["ask"].get("local_model") != old_local)):
        _unload_ask()            # switched off, or another local model chosen: the next local answer loads the new one            # outside the lock: it may wait a few seconds for an answer in progress
    return cfg


def check_key_file(v) -> str | None:
    """A provider's key_file: where its API key is kept, as the user wrote it (a leading ~ stays). Absolute or ~/...;
    None or "" removes it. The file is only read when a call needs the key (mse/providers.py)."""
    if v is None or v == "":
        return None
    if not isinstance(v, str) or not v.strip() or len(v.strip()) > 500 or any(ord(ch) < 32 for ch in v):
        raise ValueError("key_file must be the path of a file that holds the API key, e.g. ~/.config/myrouter/key")
    v = v.strip()
    if not (v.startswith("/") or v == "~" or v.startswith("~/")):
        raise ValueError("key_file must be an absolute path or start with ~/")
    return v


def _extras(o: dict) -> dict:
    """The optional fields of a provider entry that are set and well formed (anything else is ignored, never fatal)."""
    out = {}
    for k in PROVIDER_OPTIONAL:
        if not o.get(k):
            continue
        v = o[k]
        if k == "key_file":
            try:
                v = check_key_file(v)
            except ValueError:
                continue
        elif k == "training_models":
            try:
                v = check_training_models(v)
            except ValueError:
                continue
        elif k == "auto_models":
            v = v is True
        out[k] = deepcopy(v)
    return out


def provider_entries(cfg: dict) -> dict:
    """Every provider id -> its merged entry (built-ins with the user's overrides, then valid custom ones).
    mse/providers.py builds its view on this; it is here so settings can be validated without importing it."""
    raw = (cfg.get("ask") or {}).get("providers") or {}
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for pid, b in BUILTIN_PROVIDERS.items():
        o = raw.get(pid) if isinstance(raw.get(pid), dict) else {}
        out[pid] = {**deepcopy(b), **{k: deepcopy(o[k]) for k in ("label", "base_url", "models") if o.get(k)},
                    **_extras(o), "builtin": True}
    for pid, o in raw.items():
        if pid in out or not isinstance(o, dict) or o.get("kind") not in PROVIDER_KINDS or not o.get("base_url"):
            continue                                  # malformed entries are ignored, never fatal
        out[pid] = {"kind": o["kind"], "label": o.get("label") or pid, "base_url": o["base_url"],
                    "models": [m for m in o.get("models") or [] if isinstance(m, str)], "builtin": False,
                    **_extras(o)}
    return out


def check_ask(v: dict, cfg: dict) -> dict:
    """POST /api/settings {"ask": {...}}: validated (ValueError with a readable message), returned as it is merged.
    A provider switch without a model takes that provider's first model unless the current model is one of its."""
    v = dict(v)
    if "enabled" in v and not isinstance(v["enabled"], bool):
        raise ValueError("ask.enabled must be true or false")
    if "providers" in v:
        raise ValueError("providers are changed with POST /api/ask/providers (or `mse providers`), not here")
    if "route" in v and v["route"] not in ASK_ROUTES:
        raise ValueError(f"ask.route must be one of: {', '.join(ASK_ROUTES)}")
    if "cloud_redact" in v and not isinstance(v["cloud_redact"], bool):
        raise ValueError("ask.cloud_redact must be true or false")
    if "local_fallback" in v and not isinstance(v["local_fallback"], bool):
        raise ValueError("ask.local_fallback must be true or false")
    provs = provider_entries(cfg)
    if "provider" in v and v["provider"] not in provs:
        raise ValueError(f"unknown provider '{v['provider']}' (known: {', '.join(provs)})")
    if "model" in v:
        m = v["model"]
        if not isinstance(m, str) or not m.strip() or len(m.strip()) > 200 or any(ord(ch) < 32 for ch in m):
            raise ValueError("ask.model must be a model id, e.g. claude-opus-5-5")
        if m.strip().startswith(("/", "~")):
            raise ValueError("ask.model is the cloud model id; the local model's folder is ask.local_model")
        v["model"] = m.strip()
    elif "provider" in v:
        models = provs[v["provider"]]["models"]
        current = (cfg.get("ask") or {}).get("model") or ASK_CLOUD_DEFAULTS["model"]
        if models and current not in models:
            v["model"] = models[0]
    cur = cfg.get("ask") or {}
    if "allow_training_models" in v:
        v["allow_training_models"] = check_model_ids(v["allow_training_models"], "ask.allow_training_models")
    if "local_model" in v:
        v["local_model"] = check_local_model(v["local_model"])
    if "model" in v or "provider" in v:
        # a model that its provider may use for training is selected only after the user confirmed it, once
        model = v.get("model") or cur.get("model") or ASK_CLOUD_DEFAULTS["model"]
        pid = v.get("provider") or cur.get("provider") or ASK_CLOUD_DEFAULTS["provider"]
        allowed = v["allow_training_models"] if "allow_training_models" in v else cur.get("allow_training_models") or []
        if trains_on_inputs(model, provs.get(pid)) and model not in allowed:
            raise ValueError(f"training_confirmation_required: '{model}' is marked as a model its provider may use for "
                             f"training. {TRAINING_WARNING}: its inputs and outputs may be used for training. Confirm "
                             "it once by also sending ask.allow_training_models with this id (Settings › Ask asks you; "
                             "`mse providers use … --allow-training`).")
    return v


def check_model_ids(ids, name: str) -> list[str]:
    if not isinstance(ids, list) or any(not isinstance(m, str) for m in ids):
        raise ValueError(f"{name} must be a list of model ids")
    out = []
    for m in ids:
        m = m.strip()
        if not m:
            continue
        if len(m) > 200 or any(ord(ch) < 32 for ch in m):
            raise ValueError(f"{name}: a model id is one line of at most 200 characters")
        if m not in out:
            out.append(m)
    if len(out) > 200:
        raise ValueError(f"{name}: at most 200 model ids")
    return out


def check_local_model(v) -> str:
    """ask.local_model: the name of a model installed in <data>/models (or its full path) -> its full path."""
    if not isinstance(v, str) or not v.strip() or len(v) > 500:
        raise ValueError("ask.local_model must be the name of a model installed in the models folder")
    raw = v.strip()
    name = Path(raw).expanduser().name if raw.startswith(("/", "~")) else raw
    found = {m["name"]: m for m in local_chat_models()}
    if name not in found or (raw.startswith(("/", "~")) and Path(raw).expanduser().resolve() != Path(found[name]["path"]).resolve()):
        names = ", ".join(found) or "none installed"
        raise ValueError(f"'{name}' is not an installed local answer model (installed: {names})")
    return found[name]["path"]


def _unload_ask() -> None:
    """Ask switched off: stop its model worker now (in the daemon). Other processes never started one;
    if they had, their reaper would stop it within 30 s."""
    from . import embedder
    if embedder._pool is not None:
        embedder._pool.stop("ask")


# ------------------------------------------------------------------ groups and search scope
#
# A scope is what a search runs over: a comma-separated mix of collection names, group names and "all"
# ("all" = every enabled collection marked Include in All). Groups expand to exactly their members,
# duplicates are removed, unknown names are an error at the API boundary (strict) and ignored inside
# search.search() (lenient, so evaluation sets naming a missing collection keep working).
#
# Folders and groups may carry aliases, short names such as "w", accepted wherever a name is. The scope
# shortcut in a query ("w: invoice", "invoice in:work", see scope_shortcut) names folders, groups or aliases;
# it is applied at the API, CLI and MCP boundaries, never inside search.search(), so the MCP lock sees it.

# The inline filter keys of the search box (search.parse: "type:pdf", "in:work" ...) and "all" are never the
# name or alias of a folder or group, so a scope shortcut can never be mistaken for a filter or vice versa.
FILTER_KEYS = frozenset({"type", "kind", "ext", "in", "after", "before", "path", "has", "include", "exclude"})
RESERVED_SCOPE_NAMES = frozenset({"all"} | FILTER_KEYS)
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def aliases_of(entry: dict | None) -> list[str]:
    """A folder's or group's aliases as stored (lower case, no duplicates); [] when it has none."""
    raw = (entry or {}).get("aliases") or []
    if isinstance(raw, str):
        raw = raw.split(",")
    out = []
    for a in raw if isinstance(raw, list) else []:
        a = str(a).strip().lower()
        if a and a not in out:
            out.append(a)
    return out


def _norm_group(g: dict) -> dict:
    members = []
    for m in g.get("collections") or []:
        if isinstance(m, str) and m and m not in members:
            members.append(m)
    out = {"name": g["name"], "label": g.get("label") or g["name"], "collections": members}
    if aliases_of(g):
        out["aliases"] = aliases_of(g)
    if g.get("hidden") is True:          # no tab of its own; still searchable by name, alias and the scope menu
        out["hidden"] = True
    return out


def get_group(cfg: dict, name: str) -> dict | None:
    for g in cfg.get("groups") or []:
        if g["name"] == name:
            return g
    return None


def alias_owner(cfg: dict, alias: str) -> tuple[str, str] | None:
    """("folder" | "group", its name) for an alias, or None."""
    for c in cfg["collections"]:
        if alias in aliases_of(c):
            return "folder", c["name"]
    for g in cfg.get("groups") or []:
        if alias in aliases_of(g):
            return "group", g["name"]
    return None


def check_aliases(cfg: dict, aliases, kind: str, owner: str) -> list[str]:
    """Validated aliases for the folder or group `owner` (`kind` "folder" | "group"): lower case, slugs, never a
    reserved word, never the name of another folder or group, never another one's alias. Its own name is dropped
    and duplicates collapse. ValueError with a readable message otherwise."""
    if aliases is None:
        return []
    if isinstance(aliases, str):
        aliases = aliases.split(",")
    if not isinstance(aliases, list):
        raise ValueError("aliases must be a list of short names, e.g. [\"w\"]")
    out = []
    for a in aliases:
        a = str(a or "").strip().lower()
        if not a or a == owner or a in out:
            continue
        if not _SLUG.match(a):
            raise ValueError(f"alias '{a}' must be lowercase letters, digits and single dashes (e.g. '{slugify(a)}')")
        if a in RESERVED_SCOPE_NAMES:
            raise ValueError(f"'{a}' is reserved (a search filter word or 'all') and cannot be an alias")
        if get_collection(cfg, a):
            raise ValueError(f"'{a}' is already the name of a folder; choose another alias")
        if get_group(cfg, a):
            raise ValueError(f"'{a}' is already the name of a group; choose another alias")
        other = alias_owner(cfg, a)
        if other and other != (kind, owner):
            raise ValueError(f"'{a}' is already a shortcut for the {other[0]} '{other[1]}'")
        out.append(a)
    return out


def _members(collections) -> list[str]:
    if isinstance(collections, str):
        collections = collections.split(",")
    members = []
    for m in collections or []:
        m = str(m).strip().lower()
        if m and m not in members:
            members.append(m)
    return members


def save_group(label: str | None, collections, name: str | None = None, aliases=None, hidden=None) -> dict:
    """Create or update a group. `name` (a slug) is derived from the label when absent; an existing group with
    that name is updated, and what is not given (None) keeps its value there: the label, the members, the aliases
    and `hidden` (no tab of its own). ValueError with a readable message for anything invalid."""
    label = (label or "").strip()
    name = (name or "").strip()
    if not label and not name:
        raise ValueError("a group needs a label (or a name)")
    if name and not _SLUG.match(name):
        raise ValueError(f"group name '{name}' must be a slug: lowercase letters, digits and single dashes "
                         f"(e.g. '{slugify(name)}')")
    if hidden is not None and not isinstance(hidden, bool):
        raise ValueError("hidden must be true or false")
    name = name or slugify(label)
    with _lock:
        cfg = load()
        existing = get_group(cfg, name)
        if name in RESERVED_SCOPE_NAMES:
            raise ValueError(f"'{name}' is reserved (a search filter word or 'all'); choose another group name")
        if get_collection(cfg, name):
            raise ValueError(f"'{name}' is already the name of a collection; choose another group name")
        owner = alias_owner(cfg, name)
        if owner and owner != ("group", name):
            raise ValueError(f"'{name}' is already a shortcut for the {owner[0]} '{owner[1]}'; choose another group name")
        label = label or (existing["label"] if existing else name)
        members = list(existing["collections"]) if collections is None and existing else _members(collections)
        if not members:
            raise ValueError("a group needs at least one collection")
        unknown = [m for m in members if not get_collection(cfg, m)]
        if unknown:
            if any(get_group(cfg, m) for m in unknown):
                raise ValueError(f"groups cannot contain groups: {', '.join(unknown)}")
            raise ValueError(f"unknown collection{'s' if len(unknown) > 1 else ''}: {', '.join(unknown)}")
        group = {"name": name, "label": label, "collections": members}
        als = aliases_of(existing) if aliases is None and existing else check_aliases(cfg, aliases, "group", name)
        if als:
            group["aliases"] = als
        if (existing.get("hidden", False) if hidden is None and existing else hidden):
            group["hidden"] = True
        if existing:
            cfg["groups"] = [group if g is existing else g for g in cfg["groups"]]
        else:
            cfg["groups"].append(group)
        save(cfg)
        return group


def _entry(cfg: dict, name: str) -> tuple[dict | None, str | None]:
    """The folder or group called `name` and its kind ("folder" | "group")."""
    if (c := get_collection(cfg, name)) is not None:
        return c, "folder"
    if (g := get_group(cfg, name)) is not None:
        return g, "group"
    return None, None


def add_alias(alias: str, target: str) -> dict:
    """Give a folder or a group one more short name: `mse alias add w work`.
    -> {"alias", "kind", "name", "aliases"}; adding one it already has changes nothing."""
    alias, target = str(alias or "").strip().lower(), str(target or "").strip().lower()
    with _lock:
        cfg = load()
        entry, kind = _entry(cfg, target)
        if entry is None:
            if (owner := alias_owner(cfg, target)):
                raise ValueError(f"'{target}' is itself a shortcut (for the {owner[0]} '{owner[1]}'); "
                                 f"name the folder or group: mse alias add {alias} {owner[1]}")
            raise ValueError(f"no folder or group named '{target}' (known: {', '.join(known_scope_names(cfg)[1:]) or 'none'})")
        if alias == target:
            raise ValueError(f"'{alias}' is already its name")
        if not alias:
            raise ValueError("give the alias, e.g. mse alias add w work")
        entry["aliases"] = check_aliases(cfg, aliases_of(entry) + [alias], kind, target)
        save(cfg)
        return {"alias": alias, "kind": kind, "name": target, "aliases": entry["aliases"]}


def remove_alias(alias: str) -> dict:
    """Take a short name back: `mse alias rm w`. KeyError when no folder or group has it."""
    alias = str(alias or "").strip().lower()
    with _lock:
        cfg = load()
        owner = alias_owner(cfg, alias)
        if not owner:
            raise KeyError(f"no folder or group has the alias '{alias}'")
        entry, _ = _entry(cfg, owner[1])
        rest = [a for a in aliases_of(entry) if a != alias]
        if rest:
            entry["aliases"] = rest
        else:
            entry.pop("aliases", None)
        save(cfg)
        return {"alias": alias, "kind": owner[0], "name": owner[1], "aliases": rest}


def canonical_name(cfg: dict, token: str) -> str | None:
    """A scope token -> the folder or group (or "all") it names: names stay, aliases resolve, unknown -> None."""
    if token == "all" or get_collection(cfg, token) or get_group(cfg, token):
        return token
    owner = alias_owner(cfg, token)
    return owner[1] if owner else None


def remove_group(name: str) -> dict:
    with _lock:
        cfg = load()
        g = get_group(cfg, name)
        if not g:
            raise KeyError(f"no group named '{name}'")
        cfg["groups"] = [x for x in cfg["groups"] if x["name"] != name]
        save(cfg)
        return g


def scope_tokens(spec) -> list[str]:
    """'notes, Work' / ['a,b', 'c'] / None -> ['notes', 'work'] (lower-cased, de-duplicated)."""
    if spec is None:
        return []
    if isinstance(spec, str):
        spec = [spec]
    out = []
    for v in spec:
        for t in str(v or "").split(","):
            t = t.strip().lower()
            if t and t not in out:
                out.append(t)
    return out


def global_names(cfg: dict) -> list[str]:
    return [c["name"] for c in cfg["collections"] if c.get("enabled", True) and c.get("in_global", True)]


def resolve_scope(cfg: dict, spec, strict: bool = True) -> dict:
    """{"collections": [names], "label": "Work" | "work + notes" | "All", "tokens": [...],
    "unknown": [...]}. No scope at all means All. Strict: unknown names raise ValueError."""
    tokens = scope_tokens(spec)
    if not tokens:
        return {"collections": global_names(cfg), "label": "All", "tokens": [], "unknown": []}
    names, labels, unknown = [], [], []
    for t in tokens:
        t = canonical_name(cfg, t) or t                  # an alias ("w") means its folder or group
        if t == "all":
            members, label = global_names(cfg), "All"
        elif get_collection(cfg, t):
            members, label = [t], t
        elif (g := get_group(cfg, t)) is not None:
            members, label = [m for m in g["collections"] if get_collection(cfg, m)], g.get("label") or t
        else:
            unknown.append(t)
            continue
        labels.append(label)
        names += [m for m in members if m not in names]
    if unknown and strict:
        what = "collections or groups" if len(unknown) > 1 else "collection or group"
        raise ValueError(f"unknown {what}: {', '.join(unknown)}"
                         f" (known: {', '.join(known_scope_names(cfg)) or 'none'})")
    return {"collections": names, "label": " + ".join(labels) or "nothing", "tokens": tokens, "unknown": unknown}


def known_scope_names(cfg: dict) -> list[str]:
    return ["all"] + [c["name"] for c in cfg["collections"]] + [g["name"] for g in cfg.get("groups") or []]


# ------------------------------------------------------------------ the scope shortcut in a query
#
#   w: invoice              at the start: a folder, group or alias, then ":" and a space, a quote or the end
#   w,notes: invoice        a comma list (spaces after the commas are fine)
#   w:"exact phrase"        the quotes stay: everything after the shortcut is the query as you would type it
#   invoice in:work          anywhere, as its own word (in:"work" and in:w,n too); not inside a "quoted phrase"
#
# Names are case-insensitive and recognised only when known: "Note: the meeting" stays the query. "all" counts.
# A shortcut overrides the selected tab, dropdown or `collection` argument for that search. An `in:` token
# naming something unknown is an error at the API / CLI (strict) and a refusal under the MCP lock.

_PREFIX = re.compile(r"\s*(?P<names>[\w-]+(?:\s*,\s*[\w-]+)*):(?=\s|[\"'“„]|$)", re.A)
_IN_TOKEN = re.compile(r"(?<!\S)in:(?:\"(?P<quoted>[^\"]*)\"|(?P<plain>[\w-]+(?:,[\w-]+)*))(?=\s|$)", re.A | re.I)
_QUOTED = re.compile(r"\"[^\"]*\"|“[^”]*”|„[^“”]*[“”]")


def shortcut_names(cfg: dict) -> dict[str, str]:
    """Every name the scope shortcut accepts (lower case) -> the folder, group or "all" it means. Reserved words
    never count, even for an old folder that happens to be called "path"."""
    out = {"all": "all"}
    for c in cfg["collections"]:
        out.setdefault(c["name"], c["name"])
    for g in cfg.get("groups") or []:
        out.setdefault(g["name"], g["name"])
    for e in list(cfg["collections"]) + list(cfg.get("groups") or []):
        for a in aliases_of(e):
            out.setdefault(a, e["name"])
    return {k: v for k, v in out.items() if k == "all" or k not in FILTER_KEYS}


def scope_shortcut(q: str, cfg: dict | None = None, strict: bool = False) -> dict:
    """The scope shortcut in a query (see above) ->
    {"tokens": the folders / groups / "all" it names, de-duplicated, or None when there is none,
     "rest": the query without it (unchanged when there is none), "names": the names as typed,
     "unknown": names in `in:` tokens that match nothing (those tokens stay in `rest`)}.
    strict: unknown names in an `in:` token raise ValueError (a 400 at the API)."""
    cfg = cfg if cfg is not None else load()
    q = q or ""
    known = shortcut_names(cfg)
    tokens, names, unknown, cuts = [], [], [], []

    def take(found):
        for n in found:
            if known[n] not in tokens:
                tokens.append(known[n])
            if n not in names:
                names.append(n)

    m = _PREFIX.match(q)
    if m:
        found = [n.strip().lower() for n in m.group("names").split(",")]
        if all(n in known for n in found):
            take(found)
            cuts.append((m.start(), m.end()))
    quoted = [(x.start(), x.end()) for x in _QUOTED.finditer(q)]
    for x in _IN_TOKEN.finditer(q):
        if any(a <= x.start() < b for a, b in quoted + cuts):
            continue
        raw = x.group("quoted") if x.group("quoted") is not None else x.group("plain")
        if raw.lstrip().startswith(("~", "/")):        # in:"~/My Folder": a folder path, not a MySearch folder name
            continue
        found = [n.strip().lower() for n in raw.split(",") if n.strip()]
        if not found:                                  # in:"" names nothing: plain text
            continue
        if all(n in known for n in found):
            take(found)
            cuts.append((x.start(), x.end()))
        else:
            unknown += [n for n in found if n not in known and n not in unknown]
    if strict and unknown:
        what = "collections or groups" if len(unknown) > 1 else "collection or group"
        raise ValueError(f"unknown {what} after in: {', '.join(unknown)}"
                         f" (known: {', '.join(known_scope_names(cfg)) or 'none'})")
    if not cuts:
        return {"tokens": None, "rest": q, "names": [], "unknown": unknown}
    pieces, at = [], 0
    for a, b in sorted(cuts):
        pieces.append(q[at:a])
        at = b
    pieces.append(q[at:])
    rest = re.sub(r"\s+", " ", " ".join(pieces)).strip()
    return {"tokens": tokens, "rest": rest, "names": names, "unknown": unknown}


_IN_FOLDER = re.compile(r"(?<!\S)in:(?:\"(?P<q>[^\"]*)\"|(?P<p>\S+))", re.I)


def folder_tokens(q: str) -> list[str]:
    """The folder paths a query names with in:~/… or in:/… (quoted when they hold spaces), expanded and normalised.
    `in:work` (a MySearch folder or group) is not a folder path and is left to scope_shortcut."""
    out = []
    for m in _IN_FOLDER.finditer(q or ""):
        raw = m.group("q") if m.group("q") is not None else m.group("p")
        for part in re.split(r",(?=[~/])", raw):
            part = part.strip()
            if part.startswith(("~", "/")):
                out.append(os.path.normpath(os.path.expanduser(part)))
    return out


def split_scope(q: str, cfg: dict | None = None) -> tuple[list[str] | None, str]:
    """(the folders / groups a shortcut names, or None; the query without it). Unknown `in:` names -> ValueError."""
    s = scope_shortcut(q, cfg, strict=True)
    return s["tokens"], s["rest"]


def strip_scope(q: str, cfg: dict | None = None) -> str:
    """The query without its scope shortcut: where to search is not what to search for (favourites, eval)."""
    return scope_shortcut(q, cfg)["rest"]
