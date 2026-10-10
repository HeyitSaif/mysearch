"""Where MySearch keeps its data. Nothing is ever written inside an indexed folder."""

import os
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("MSE_HOME", Path.home() / "Library" / "Application Support" / "MySearch"))
CONFIG = DATA / "config.json"
COLLECTIONS = DATA / "collections"
CACHE_DB = DATA / "cache.db"
MODELS = DATA / "models"
BIN = DATA / "bin"
# The helper inside MySearch.app (passed by the app's --daemon supervisor as MSE_HELPER) wins when it exists, so macOS
# attributes the crawl/OCR/watch children to the app; the copy in the data folder is the fallback.
_bundled = os.environ.get("MSE_HELPER")
HELPER = Path(_bundled) if _bundled and Path(_bundled).exists() else BIN / "msehelper"
THUMBS = DATA / "thumbs"
TMP = DATA / "tmp"
TRASH = DATA / "trash"
LOGS = Path.home() / "Library" / "Logs" / "MySearch"
UI = Path(os.environ.get("MSE_UI_DIR") or PROJECT / "mse" / "ui")   # MSE_UI_DIR: serve a working copy of the interface

DEFAULT_MODEL = MODELS / "embeddinggemma-2-8bit"


def ensure_dirs() -> None:
    for d in (DATA, COLLECTIONS, MODELS, BIN, THUMBS, TMP, TRASH, LOGS):
        d.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA, 0o700)
