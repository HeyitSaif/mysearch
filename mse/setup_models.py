"""Models for a fresh install: the manifest, the presence check and the installer behind `mse setup models`.

Each model is a folder in <data>/models/ under the name the app already uses (paths.MODELS, config.py DEFAULTS and
ask.py), so an existing install is recognised as it is. A folder counts as installed when every file its loader reads
is there and it has weights, whatever produced it. Downloads come from public Hugging Face repos at a pinned revision,
into a dot-prefixed `.<folder>.partial` folder that is renamed into place only when complete: a re-run resumes the
download and never replaces a finished folder. Nothing here converts a model. A model with no recorded conversion
recipe is listed with `source: unknown` and is not installed automatically (docs/INSTALL.md).

The same installer runs for the app's setup assistant (GET/POST /api/setup/models, mse/server.py): the service starts it as a
child process, `mse setup models --events`, which prints one JSON line per event; the service itself stays offline
(HF_HUB_OFFLINE=1) and only that child, which exists only while a download the user started is running, talks to
Hugging Face. Same folders, same pinned revisions, same `.partial` folder renamed into place, nothing ever deleted.

Provenance was checked on 2026-10-10 against the Hugging Face API (public, read-only) and the reference install's local folders:
file byte sizes, the LFS sha256 of the weights, and the revision named by the download cache. No weights were read.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import paths

GB = 1_000_000_000
TIERS = ("minimal", "full")   # `full` includes `minimal`; "optional" entries are never installed by setup


@dataclass(frozen=True)
class Part:
    repo: str                       # Hugging Face repo id
    revision: str                   # commit the reference copy matches
    files: tuple[str, ...] | None   # None: every file of the repo


@dataclass(frozen=True)
class Model:
    folder: str                     # exact folder name in <data>/models: existing installs use it, never rename
    role: str
    tier: str                       # minimal | full | optional
    size_bytes: int                 # approximate download size
    licence: str
    gated: bool
    required: tuple[str, ...]       # what the loader reads: a folder without these is not installed
    parts: tuple[Part, ...] = ()    # empty: not installable automatically
    produced: str = ""              # how the folder comes to exist: download, merge, conversion, or unknown
    source: str = ""                # provenance and evidence, and what is not known


MANIFEST: tuple[Model, ...] = (
    Model(
        folder="embeddinggemma-2-8bit",
        produced="download, pre-converted by mlx-community",
        role="embeddings: text search, and images, PDF pages, audio and video (one vector space)",
        tier="minimal",
        size_bytes=1_266_584_562,
        licence="Apache-2.0 (model card and Hugging Face licence tag, checked 2026-10-10)",
        gated=False,
        required=("config.json", "tokenizer.json", "model.safetensors"),
        parts=(Part("mlx-community/embeddinggemma-2-8bit", "7505ef2f8ddef45efef6d060865f27989b3c9cec", None),),
        source="mlx-community/embeddinggemma-2-8bit, pre-converted MLX, 8-bit affine, group 64. model.safetensors and "
               "config.json have the repo's byte sizes; README.md and two JSON files differ in size locally (edited or "
               "re-saved). Weights not hashed.",
    ),
    Model(
        folder="embeddinggemma-2-8bit-text",
        produced="unknown (cut from the full folder; method not recorded)",
        role="text-only copy of the model above: smaller and quicker to load for text search (optional)",
        tier="optional",
        size_bytes=320_000_000,
        licence="as embeddinggemma-2-8bit (Apache-2.0)",
        gated=False,
        required=("config.json", "tokenizer.json", "model.safetensors"),
        source="source: unknown. No public text-only repo was found and the repo holds no conversion recipe. Search does "
               "not need it: mse/embedder.py (Worker._start, Pool.embed_texts) falls back to embeddinggemma-2-8bit when "
               "this folder is absent. How it was cut is not published.",
    ),
    Model(
        folder="qwen3-reranker-0.6b-8bit",
        produced="converted by hand from the original weights (command not recorded): not installable automatically",
        role="re-ranker for close calls. Without it, search runs unre-ranked and the error is recorded in the timings",
        tier="full",
        size_bytes=644_626_711,
        licence="Apache-2.0 (original Qwen/Qwen3-Reranker-0.6B)",
        gated=False,
        required=("config.json", "tokenizer.json", "model.safetensors"),
        source="original weights: Qwen/Qwen3-Reranker-0.6B at e61197ed45024b0ed8a2d74b80b4d909f1255473 (the reference install's download "
               "cache holds that snapshot). The 8-bit folder was converted by hand (config: affine, 8 bits, group 64). The "
               "command is not recorded and the repo holds no recipe, so it is not installed automatically. No public 8-bit "
               "MLX repo was found (mlx-community lists 4-bit and mxfp8 builds). The conversion (mlx-lm version and flags) "
               "is not published.",
    ),
    Model(
        folder="whisper-large-v3-turbo-8bit",
        produced="download, merged: weights and config from the MLX repo, processor files from the original repo",
        role="speech to text with timestamps (audio and video transcripts)",
        tier="full",
        size_bytes=869_093_446,
        licence="MIT (openai/whisper-large-v3-turbo, the original). The MLX repo's card says apache-2.0",
        gated=False,
        required=("config.json", "model.safetensors", "preprocessor_config.json", "tokenizer.json"),
        parts=(
            Part("mlx-community/whisper-large-v3-turbo-8bit", "62103fc276a35fdc76e318f314d8ff47987fba89",
                 ("model.safetensors", "config.json", "multilingual.tiktoken")),
            Part("openai/whisper-large-v3-turbo", "41f01f3fe87f28c78e2fbf8b568835947dd65ed9",
                 ("added_tokens.json", "generation_config.json", "merges.txt", "normalizer.json",
                  "preprocessor_config.json", "special_tokens_map.json", "tokenizer.json",
                  "tokenizer_config.json", "vocab.json")),
        ),
        source="weights and config.json: mlx-community/whisper-large-v3-turbo-8bit (weights' LFS sha256 1dffa280...; the "
               "reference install's cache holds that snapshot). The processor and tokenizer files come from openai/whisper-large-v3-turbo; "
               "all nine have that repo's byte sizes locally. Its config.json and 1.6 GB model.safetensors are not used, "
               "because the MLX config.json would be overwritten.",
    ),
    Model(
        folder="qwen3-4b-instruct-2507-4bit",
        produced="download, pre-converted by mlx-community",
        role="local answer model for Ask: used only while Ask is on and a local answer is chosen (default local model, "
             "mse/ask.py)",
        tier="full",
        size_bytes=2_278_972_236,
        licence="Apache-2.0 (mlx-community model card; original Qwen/Qwen3-4B-Instruct-2507)",
        gated=False,
        required=("config.json", "tokenizer.json", "model.safetensors"),
        parts=(Part("mlx-community/Qwen3-4B-Instruct-2507-4bit", "50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b", None),),
        source="mlx-community/Qwen3-4B-Instruct-2507-4bit, the revision the reference install downloaded: the download record names commit "
               "50d42775, and its etag equals the repo's model.safetensors LFS sha256 (2a73c6c2...). All 13 files have the "
               "repo's byte sizes.",
    ),
    Model(
        folder="qwen3.5-4b-4bit",
        produced="download, pre-converted by mlx-community (optional)",
        role="alternative local answer model for Ask (optional: choose it in Settings > Ask)",
        tier="optional",
        size_bytes=3_062_000_000,
        licence="Apache-2.0 (model card; original Qwen/Qwen3.5-4B)",
        gated=False,
        required=("config.json", "tokenizer.json", "model.safetensors"),
        parts=(Part("mlx-community/Qwen3.5-4B-MLX-4bit", "32f3e8ecf65426fc3306969496342d504bfa13f3", None),),
        source="mlx-community/Qwen3.5-4B-MLX-4bit at 32f3e8ec: the download record's tree name is that revision, which is also "
               "the repo's current head. The card describes the maintainer's conversion, not one of ours. Not in any tier.",
    ),
)


def _partial(m: Model) -> Path:
    return paths.MODELS / f".{m.folder}.partial"


def _tilde(p) -> str:
    s, home = str(p), str(Path.home())
    if s == home:
        return "~"
    return "~" + s[len(home):] if s.startswith(home + os.sep) else s


def human(n: float) -> str:
    return f"{n / GB:.2f} GB" if n >= GB / 10 else f"{n / 1_000_000:.0f} MB"


def _folder_bytes(folder: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(folder):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _present(folder: Path, required: tuple[str, ...]) -> bool:
    """Installed: every file the loader reads is there and non-empty, and the folder holds weights."""
    try:
        if not all((folder / name).stat().st_size > 0 for name in required):
            return False
        return any(p.stat().st_size > 0 for p in folder.glob("*.safetensors"))
    except OSError:
        return False


def status(m: Model) -> str:
    """present | incomplete (a folder without what its loader reads: never touched) | partial (a resumable download) |
    missing. Read-only."""
    folder = paths.MODELS / m.folder
    if folder.is_dir():
        return "present" if _present(folder, m.required) else "incomplete"
    return "partial" if _partial(m).is_dir() else "missing"


def tier_models(tier: str) -> list[Model]:
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}: use minimal or full")
    wanted = TIERS[: TIERS.index(tier) + 1]
    return [m for m in MANIFEST if m.tier in wanted]


def check_rows() -> list[dict]:
    """One row per manifest model, for `mse setup check --json`. Read-only."""
    rows = []
    for m in MANIFEST:
        folder = paths.MODELS / m.folder
        rows.append({
            "folder": m.folder, "tier": m.tier, "role": m.role, "status": status(m), "produced": m.produced,
            "installed_bytes": _folder_bytes(folder) if folder.is_dir() else 0,
            "download_bytes": m.size_bytes, "installable": bool(m.parts), "gated": m.gated,
            "licence": m.licence, "source": m.source, "path": _tilde(folder),
        })
    return rows


def _model_line(r: dict) -> str:
    st = r["status"]
    if st == "present":
        detail = f"present, {human(r['installed_bytes'])}"
    elif st == "missing" and r["tier"] == "optional":
        detail = "missing (optional)"
    elif st == "missing" and r["installable"]:
        detail = f"missing, {human(r['download_bytes'])} to download"
    elif st == "missing":
        detail = "missing, not installable automatically (docs/INSTALL.md)"
    elif st == "incomplete":
        detail = "incomplete folder, not touched (move it aside yourself, then re-run)"
    else:
        detail = "partial download (re-run the setup to resume)"
    return f"model       {r['folder']:<32} [{r['tier']}] {detail}"


def _runs(args: list[str]) -> str:
    if not args:
        return "an unknown program"
    return "MySearch.app" if "--daemon" in args else f"the project's Python ({_tilde(args[0])})"


def _system_lines() -> list[str]:
    import plistlib
    from . import service
    apps = [p for p in (Path("/Applications/MySearch.app"), Path.home() / "Applications" / "MySearch.app") if p.exists()]
    lines = ["app         " + (", ".join(_tilde(p) for p in apps) if apps
                              else "not installed (scripts/setup.sh offers to copy build/MySearch.app to /Applications)")]
    lines.append(f"service     {service.status()}")
    # what the installed LaunchAgent runs (its plist on disk), and what `mse service install` would run now
    would = service.plist()["ProgramArguments"]
    installed = None
    if service.PLIST.exists():
        try:
            with open(service.PLIST, "rb") as f:
                installed = plistlib.load(f).get("ProgramArguments") or []
        except Exception:
            installed = []
    where = paths.MODELS if paths.MODELS.exists() else Path.home()
    lines.append(f"disk        {human(shutil.disk_usage(where).free)} free where the models go ({_tilde(where)})")
    fda = "System Settings > Privacy & Security > Full Disk Access"
    if installed is None:
        msg = f"the service is not installed; after `uv run mse service install`, grant Full Disk Access to {_runs(would)}"
    elif ("--daemon" in installed) != ("--daemon" in would):
        msg = (f"the installed service runs {_runs(installed)}; run `uv run mse service install` to run {_runs(would)}, "
               "then grant it Full Disk Access")
    else:
        msg = f"grant Full Disk Access to {_runs(installed)}"
    lines.append(f"full disk   {msg} ({fda})")
    return lines


def report_lines() -> list[str]:
    """What `mse setup check` and `mse doctor` print for models, the app, the service and disk. Read-only."""
    return [_model_line(r) for r in check_rows()] + _system_lines()


def _download(m: Model, out, tqdm_class=None, on_part=None) -> None:
    """Download every part of one model into its partial folder, then rename the folder into place. tqdm_class: a progress
    sink for huggingface_hub (the setup assistant's child process); None keeps the library's own bars. on_part(i) is called
    before each part (a model can have two repos)."""
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import HfHubHTTPError, RepositoryNotFoundError

    part = _partial(m)
    part.mkdir(parents=True, exist_ok=True)
    current = m.parts[0].repo
    try:
        for i, p in enumerate(m.parts, 1):
            current = p.repo
            out(f"  [{i}/{len(m.parts)}] {p.repo} @ {p.revision[:8]}")
            if on_part is not None:
                on_part(i)
            extra = {"tqdm_class": tqdm_class} if tqdm_class is not None else {}
            snapshot_download(repo_id=p.repo, revision=p.revision, local_dir=str(part),
                              allow_patterns=list(p.files) if p.files else None, **extra)
    except RepositoryNotFoundError as e:
        raise RuntimeError(f"{current}: not found, or it needs its terms accepted on https://huggingface.co/{current} "
                           "and a login (uv run hf auth login). Hugging Face answers both cases the same way. "
                           f"Detail: {e}") from None
    except HfHubHTTPError as e:
        raise RuntimeError(f"{current}: Hugging Face refused the request ({e}). Re-run the same command to resume.") from None
    except KeyboardInterrupt:
        raise RuntimeError("stopped. Re-run the same command: the download resumes") from None
    except Exception as e:
        raise RuntimeError(f"{current}: download failed ({type(e).__name__}: {e}). Re-run the same command: finished "
                           "files are kept and the download resumes.") from None
    missing = [n for n in m.required if not (part / n).exists()]
    if missing or not _present(part, m.required):
        raise RuntimeError(f"{m.folder}: the download finished without {', '.join(missing) or 'its weights'}. "
                           "Re-run the same command.")
    final = paths.MODELS / m.folder
    if final.exists():
        raise RuntimeError(f"{_tilde(final)} appeared during the download; not replacing it. Check it, then re-run.")
    os.replace(part, final)
    out(f"  installed {m.folder}")


def install(tier: str, dry_run: bool = False, out=print) -> None:
    """Install the models of a tier. Present folders are skipped. --dry-run prints the plan and changes nothing."""
    models = tier_models(tier)
    out(f"Models for the '{tier}' tier, in {_tilde(paths.MODELS)}:")
    todo: list[Model] = []
    blocked: list[str] = []
    for m in models:
        st = status(m)
        if st == "present":
            out(f"  present    {m.folder:<32} skip")
        elif st == "incomplete":
            blocked.append(m.folder)
            out(f"  incomplete {m.folder:<32} folder lacks files; not touched")
        elif not m.parts:
            out(f"  manual     {m.folder:<32} not installable automatically (docs/INSTALL.md)")
        else:
            todo.append(m)
            label = "resume" if st == "partial" else "download"
            out(f"  {label:<10} {m.folder:<32} {human(m.size_bytes)}  {' + '.join(p.repo for p in m.parts)}")
    if dry_run:
        out(f"\nDry run: nothing downloaded or changed. {len(todo)} model(s) would be downloaded.")
        return
    if blocked:
        raise RuntimeError(f"incomplete folder(s): {', '.join(blocked)}. Move each one out of the models folder yourself "
                           "(Finder), then re-run.")
    if not todo:
        out("\nNothing to download.")
        return
    if os.environ.get("HF_HUB_OFFLINE", "").strip().lower() in ("1", "true", "yes", "on"):
        raise RuntimeError("HF_HUB_OFFLINE is set in this shell, so nothing can be downloaded. Unset it and run again.")
    need = sum(m.size_bytes for m in todo)
    paths.MODELS.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(paths.MODELS).free
    if free < need + GB:
        raise RuntimeError(f"not enough free disk space: about {human(need)} is needed, plus 1 GB, and {human(free)} is free")
    for n, m in enumerate(todo, 1):
        out(f"\n[{n}/{len(todo)}] {m.folder}")
        _download(m, out)
    out("\nDone. Check with: uv run mse setup check")


# ====================================================================== the setup assistant (the app's first-run screens)
# GET /api/setup/status, GET and POST /api/setup/models, POST /api/setup/models/cancel (mse/server.py) call these.

class SetupRefused(ValueError):
    """A request the service refuses; the sentence is shown by the app as it is (a 400)."""


def app_bundle() -> Path | None:
    """The MySearch.app this code runs from (the bundled runtime keeps the package in <app>/Contents/Resources/mse), else None."""
    r = paths.PROJECT
    if r.name == "Resources" and r.parent.name == "Contents" and r.parent.parent.suffix == ".app":
        return r.parent.parent
    return None


def _disk_where() -> Path:
    for p in (paths.MODELS, paths.DATA, Path.home()):
        if p.exists():
            return p
    return Path("/")


def _partial_bytes(m: Model) -> int:
    """Bytes already on disk for a resumable download. Allocated blocks are counted, never more than the file's size, so a
    file that was stretched to its final length before it was filled does not look finished."""
    total = 0
    for root, _dirs, files in os.walk(_partial(m)):
        for name in files:
            try:
                st = os.stat(os.path.join(root, name))
            except OSError:
                continue
            total += min(st.st_size, st.st_blocks * 512)
    return total


def plan(tier: str) -> dict:
    """What a download of `tier` would do. Read-only. todo: models to fetch; blocked: folders that exist without their weights
    (never touched); manual: models without a public source that are not installed; present: already installed.
    need_bytes: what is still to fetch, counting what a resumable partial folder already holds."""
    try:
        models = tier_models(tier)
    except ValueError as e:
        raise SetupRefused(str(e)) from None
    todo, blocked, manual, present = [], [], [], []
    need = 0
    for m in models:
        st = status(m)
        if st == "present":
            present.append(m.folder)
        elif st == "incomplete":
            blocked.append(m.folder)
        elif not m.parts:
            manual.append(m.folder)
        else:
            todo.append(m)
            need += max(0, m.size_bytes - (_partial_bytes(m) if st == "partial" else 0))
    return {"todo": todo, "blocked": blocked, "manual": manual, "present": present, "need_bytes": need}


def models_view() -> dict:
    """GET /api/setup/models: the manifest with what is on disk, the plan per tier, the download job and the free disk."""
    rows = check_rows()
    for r in rows:
        r["download_human"] = human(r["download_bytes"])
        r["installed_human"] = human(r["installed_bytes"]) if r["installed_bytes"] else ""
    tiers = {}
    for t in TIERS:
        p = plan(t)
        tiers[t] = {"folders": [m.folder for m in tier_models(t)],
                    "todo": [m.folder for m in p["todo"]], "blocked": p["blocked"], "manual": p["manual"],
                    "present": p["present"],
                    "total_bytes": sum(m.size_bytes for m in p["todo"]), "need_bytes": p["need_bytes"],
                    "need_human": human(p["need_bytes"]),
                    "ready": not p["todo"] and not p["blocked"]}
    try:
        free = shutil.disk_usage(_disk_where()).free
    except OSError:
        free = None
    return {"models": rows, "tiers": tiers, "job": JOB.view(), "free_bytes": free, "models_dir": _tilde(paths.MODELS),
            "offline_service": os.environ.get("HF_HUB_OFFLINE", "").strip().lower() in ("1", "true", "yes", "on")}


def setup_status() -> dict:
    """GET /api/setup/status: what the setup assistant needs to decide whether to open, in one small answer."""
    from . import config, macindex
    cfg = config.load()
    n = len(cfg.get("collections") or [])
    minimal = [status(m) == "present" for m in tier_models("minimal")]
    p_full = plan("full")
    fda = None
    try:
        fda = macindex.full_disk_access()
    except Exception:
        pass
    app = app_bundle()
    from . import __version__
    return {
        "version": __version__,
        "collections": n,
        "minimal_models_ready": all(minimal),
        "full_models_ready": not p_full["todo"] and not p_full["blocked"],
        "needs_setup": n == 0 or not all(minimal),
        "full_disk_access": "granted" if fda is True else "not_granted" if fda is False else "unknown",
        "bundled": app is not None,
        "app": _tilde(app) if app else None,
        "job": JOB.view(),
    }


class DownloadJob:
    """One model download at a time, run by a child process (`mse setup models --events`) that prints a JSON line per event.
    The service keeps no network code of its own: it is started offline, and only this child, which exists only while the
    user's download runs, reaches Hugging Face."""

    def __init__(self):
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()              # one start at a time
        self._proc: subprocess.Popen | None = None
        self.command_factory = _child_command          # tests replace it with a fake that prints events
        self._reset()

    def _reset(self):
        self.state = "idle"             # idle | running | done | error | cancelled
        self.tier = None
        self.total_bytes = 0
        self.done_bytes = 0
        self.current = None
        self.models: list[dict] = []
        self.error = None
        self.message = ""
        self.started_at = None
        self.finished_at = None
        self.rate = None                # bytes per second, smoothed
        self._last = None               # (time, done_bytes)
        self._cancel = False

    # ---- reading
    def view(self) -> dict:
        with self._lock:
            return self._view()

    def _view(self) -> dict:
        total, done = self.total_bytes, min(self.done_bytes, self.total_bytes) if self.total_bytes else self.done_bytes
        pct = round(100.0 * done / total, 1) if total else (100.0 if self.state == "done" else 0.0)
        eta = round((total - done) / self.rate) if self.state == "running" and self.rate and total > done else None
        return {"state": self.state, "tier": self.tier, "total_bytes": total, "done_bytes": done, "percent": pct,
                "current": self.current, "models": [dict(m) for m in self.models], "rate_bps": round(self.rate) if self.rate else None,
                "eta_s": eta, "error": self.error, "message": self.message, "started_at": self.started_at,
                "finished_at": self.finished_at}

    # ---- starting and stopping
    def start(self, tier: str, on_done=None) -> dict:
        """Start the download of a tier. A running job is returned as it is. Raises SetupRefused with a sentence the app shows."""
        with self._start_lock:
            return self._start(tier, on_done)

    def _start(self, tier: str, on_done) -> dict:
        with self._lock:
            if self.state == "running":
                return {**self._view(), "started": False}
        p = plan(tier)
        if p["blocked"]:
            raise SetupRefused(f"{', '.join(p['blocked'])}: a folder with that name is in the models folder without its weights, so "
                               "it was not touched. Move it out of the models folder in Finder, then try again.")
        if not p["todo"]:
            with self._lock:
                self._reset()
                self.state, self.tier = "done", tier
                self.message = "Nothing to download: the models of this set are already installed."
                self.finished_at = time.time()
                return {**self._view(), "started": False}
        free = shutil.disk_usage(_disk_where()).free
        if free < p["need_bytes"] + GB:
            raise SetupRefused(f"not enough free disk space: about {human(p['need_bytes'])} is needed, plus 1 GB, and "
                               f"{human(free)} is free.")
        cmd, env = self.command_factory(tier)
        paths.LOGS.mkdir(parents=True, exist_ok=True)
        try:
            log = open(paths.LOGS / "setup-models.log", "ab")
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log, env=env,
                                    cwd=str(paths.PROJECT), text=True, bufsize=1)
            log.close()
        except OSError as e:
            raise SetupRefused(f"could not start the download: {e}") from None
        with self._lock:
            self._reset()
            self._proc = proc
            self.state, self.tier = "running", tier
            self.started_at = time.time()
            self.total_bytes = sum(m.size_bytes for m in p["todo"])
            self.models = [{"folder": m.folder, "size_bytes": m.size_bytes, "state": "pending"} for m in p["todo"]]
            self.message = "Starting the download"
            view = {**self._view(), "started": True}
        threading.Thread(target=self._read, args=(proc, on_done), daemon=True, name="setup-download").start()
        return view

    def cancel(self) -> dict:
        """Stop a running download. What was downloaded stays in its .partial folder; the next start resumes it."""
        with self._lock:
            proc = self._proc if self.state == "running" else None
            if proc is not None:
                self._cancel = True
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        return self.view()

    # ---- the reader thread
    def _apply(self, ev: dict) -> bool:
        """Fold one event into the state. True on the final `done`."""
        kind = ev.get("event")
        with self._lock:
            if kind == "plan":
                self.total_bytes = int(ev.get("total_bytes") or self.total_bytes)
            elif kind == "model":
                folder, st = ev.get("folder"), ev.get("state")
                for m in self.models:
                    if m["folder"] == folder:
                        m["state"] = "downloading" if st == "start" else "installed" if st == "installed" else m["state"]
                if st == "start":
                    self.current = folder
                    self.message = f"Downloading {folder}"
                elif st == "installed":
                    self.message = f"Installed {folder}"
            elif kind == "bytes":
                done, now = int(ev.get("done_bytes") or 0), time.monotonic()
                if self._last and now > self._last[0] and done >= self._last[1]:
                    inst = (done - self._last[1]) / (now - self._last[0])
                    self.rate = inst if self.rate is None else 0.3 * inst + 0.7 * self.rate
                self._last = (now, done)
                self.done_bytes = max(self.done_bytes, done)
            elif kind == "error":
                self.error = str(ev.get("message") or "the download failed")
            elif kind == "done":
                return True
        return False

    def _read(self, proc, on_done) -> None:
        saw_done = False
        try:
            for raw in proc.stdout:
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue                    # a stray line from a library: the log file has the rest
                if isinstance(ev, dict) and self._apply(ev):
                    saw_done = True
        finally:
            rc = proc.wait()
            try:
                proc.stdout.close()
            except OSError:
                pass
        with self._lock:
            if self._cancel:
                self.state, self.message = "cancelled", "Stopped. Start again to resume: finished files are kept."
            elif saw_done and rc == 0:
                self.state, self.message, self.error = "done", "All models installed.", None
                self.done_bytes, self.current, self.rate = self.total_bytes, None, None
                for m in self.models:
                    m["state"] = "installed"
            else:
                self.state = "error"
                self.error = self.error or f"the download process ended with status {rc}; see ~/Library/Logs/MySearch/setup-models.log"
                self.message = "The download stopped. Start it again to resume: finished files are kept."
            self.finished_at = time.time()
            self._proc = None
            final = self.state
        if on_done is not None:
            try:
                on_done(final)
            except Exception:
                pass


def _child_command(tier: str) -> tuple[list[str], dict]:
    """The command and environment of the download child. HF_HUB_OFFLINE is dropped from its environment: the service sets it
    for itself and its workers, and a download that was asked for must be able to go online."""
    cmd = [sys.executable, "-m", "mse", "setup", "models", f"--{tier}", "--events"]
    env = {k: v for k, v in os.environ.items() if k != "HF_HUB_OFFLINE"}
    # No Hugging Face account identity goes with the request: every model in the manifest is public (gated: False), so the
    # token a user may have stored for other work is not read. (`mse setup models` in a terminal is unchanged.)
    env.update(PYTHONUNBUFFERED="1", HF_HUB_DISABLE_TELEMETRY="1", HF_HUB_DISABLE_IMPLICIT_TOKEN="1", PYTHONDONTWRITEBYTECODE="1")
    return cmd, env


JOB = DownloadJob()


def start_download(tier, on_done=None) -> dict:
    if not isinstance(tier, str):
        raise SetupRefused('send {"tier": "minimal"} or {"tier": "full"}')
    return JOB.start(tier, on_done)


def cancel_download() -> dict:
    return JOB.cancel()


# ---- the child side: `mse setup models --minimal|--full --events`

class _ByteSink:
    """Receives huggingface_hub's byte counts without printing anything. snapshot_download makes two byte bars per call (bytes
    received, bytes written); the larger of the two is the progress of that part, and the parts of a model add up."""

    def __init__(self):
        self.lock = threading.Lock()
        self.base = 0                   # bytes of the parts that are finished
        self.bars: list = []            # the byte bars of the part in progress

    def begin_part(self, _i: int = 0) -> None:
        with self.lock:
            self.base += max((int(b.n or 0) for b in self.bars), default=0)
            self.bars = []

    def bytes(self) -> int:
        with self.lock:
            return self.base + max((int(b.n or 0) for b in self.bars), default=0)

    def seen(self) -> bool:
        with self.lock:
            return bool(self.bars) or self.base > 0

    def tqdm_class(self):
        from huggingface_hub.utils import tqdm as hf_tqdm
        sink = self

        class _Bar(hf_tqdm):
            def __init__(self, *args, **kwargs):
                is_bytes = kwargs.get("unit") == "B"
                kwargs["disable"] = True        # nothing is drawn; the app draws its own bar
                super().__init__(*args, **kwargs)
                if is_bytes:
                    with sink.lock:
                        sink.bars.append(self)

            def update(self, n=1):
                self.n = (self.n or 0) + (n or 0)
                return True

        return _Bar


def _emit_stdout(ev: dict) -> None:
    sys.stdout.write(json.dumps(ev, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _exit_with_parent() -> None:
    """The service starts this child. If the service goes away, so does the download (a partial folder stays and resumes)."""
    parent = os.getppid()

    def watch():
        while True:
            time.sleep(2.0)
            if os.getppid() != parent:
                os._exit(75)

    threading.Thread(target=watch, daemon=True, name="parent-watch").start()


def _for_app(message: str) -> str:
    """The installer's sentences say "Re-run the same command" (they are written for the terminal); in the app it is a button."""
    return message.replace("Re-run the same command: finished files are kept and the download resumes.",
                           "Try again: finished files are kept and the download resumes.") \
                  .replace("Re-run the same command to resume.", "Try again to resume.") \
                  .replace("Re-run the same command.", "Try again.") \
                  .replace("Re-run the same command", "Try again")


def install_events(tier: str, emit=_emit_stdout, watch_parent: bool = True) -> int:
    """Like install(), for the setup assistant: the same folders, revisions and `.partial` folders, but it prints JSON lines
    (plan, model start, bytes, model installed, done or error) instead of text. Returns the exit status."""
    try:
        _install_events(tier, emit, watch_parent)
    except (SetupRefused, RuntimeError) as e:
        emit({"event": "error", "message": _for_app(str(e))})
        return 1
    emit({"event": "done"})
    return 0


def _install_events(tier: str, emit, watch_parent: bool) -> None:
    p = plan(tier)
    if p["blocked"]:
        raise RuntimeError(f"incomplete folder(s): {', '.join(p['blocked'])}. Move each one out of the models folder yourself "
                           "(Finder), then try again.")
    todo = p["todo"]
    if not todo:
        return
    if os.environ.get("HF_HUB_OFFLINE", "").strip().lower() in ("1", "true", "yes", "on"):
        raise RuntimeError("HF_HUB_OFFLINE is set, so nothing can be downloaded.")
    paths.MODELS.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(paths.MODELS).free
    if free < p["need_bytes"] + GB:
        raise RuntimeError(f"not enough free disk space: about {human(p['need_bytes'])} is needed, plus 1 GB, and {human(free)} is free")
    if watch_parent:
        _exit_with_parent()
    emit({"event": "plan", "total_bytes": sum(m.size_bytes for m in todo),
          "models": [{"folder": m.folder, "size_bytes": m.size_bytes} for m in todo]})
    finished = 0
    for m in todo:
        emit({"event": "model", "folder": m.folder, "state": "start"})
        sink = _ByteSink()
        stop = threading.Event()
        t0 = time.monotonic()

        def report(m=m, sink=sink, stop=stop, finished=finished, t0=t0):
            while not stop.wait(0.5):
                got = sink.bytes()
                if not sink.seen() and time.monotonic() - t0 > 10:
                    got = _partial_bytes(m)          # the library's byte bars never showed up: count what is on disk
                emit({"event": "bytes", "folder": m.folder, "model_bytes": got,
                      "done_bytes": finished + min(got, m.size_bytes)})

        reporter = threading.Thread(target=report, daemon=True, name="report")
        reporter.start()
        try:
            _download(m, lambda _s: None, tqdm_class=sink.tqdm_class(), on_part=sink.begin_part)
        finally:
            stop.set()
            reporter.join(timeout=2)
        finished += m.size_bytes
        emit({"event": "bytes", "folder": m.folder, "model_bytes": m.size_bytes, "done_bytes": finished})
        emit({"event": "model", "folder": m.folder, "state": "installed"})
