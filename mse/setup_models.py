"""Models for a fresh install: the manifest, the presence check and the installer behind `mse setup models`.

Each model is a folder in <data>/models/ under the name the app already uses (paths.MODELS, config.py DEFAULTS and
ask.py), so an existing install is recognised as it is. A folder counts as installed when every file its loader reads
is there and it has weights, whatever produced it. Downloads come from public Hugging Face repos at a pinned revision,
into a dot-prefixed `.<folder>.partial` folder that is renamed into place only when complete: a re-run resumes the
download and never replaces a finished folder. Nothing here converts a model. A model with no recorded conversion
recipe is listed with `source: unknown` and is not installed automatically (docs/INSTALL.md).

Provenance was checked on 2026-10-10 against the Hugging Face API (public, read-only) and the reference install's local folders:
file byte sizes, the LFS sha256 of the weights, and the revision named by the download cache. No weights were read.
"""

from __future__ import annotations

import os
import shutil
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


def _download(m: Model, out) -> None:
    """Download every part of one model into its partial folder, then rename the folder into place."""
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import HfHubHTTPError, RepositoryNotFoundError

    part = _partial(m)
    part.mkdir(parents=True, exist_ok=True)
    current = m.parts[0].repo
    try:
        for i, p in enumerate(m.parts, 1):
            current = p.repo
            out(f"  [{i}/{len(m.parts)}] {p.repo} @ {p.revision[:8]}")
            snapshot_download(repo_id=p.repo, revision=p.revision, local_dir=str(part),
                              allow_patterns=list(p.files) if p.files else None)
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
