"""GET /api/tree: the immediate subfolders of an indexed folder, for the Ask folder picker.

Only what the collection itself would index is listed: a folder that the collection's rules exclude (its `exclude`
list, the built-in excludes, hidden folders, the secret patterns) or that the privacy rules of the whole-Mac index
block is never shown, not even by name. Symbolic links are not followed and `..` is refused."""

import os
import stat as S
from pathlib import Path

from . import config, macindex
from .indexer import _nokey_matcher, _secret_excluded, matchers

MAX_FOLDERS = 2000


class NotFound(KeyError):
    pass


def _clean(rel: str) -> list[str]:
    rel = (rel or "").replace("\\", "/").strip().strip("/")
    parts = [p for p in rel.split("/") if p and p != "."]
    if any(p == ".." or "\0" in p for p in parts):
        raise ValueError("path must stay inside the folder (no '..')")
    return parts


def folders(collection: str, rel: str = "") -> dict:
    cfg = config.load()
    name = config.canonical_name(cfg, (collection or "").lower()) or (collection or "").lower()
    coll = config.get_collection(cfg, name)
    if not coll:
        raise NotFound(f"no collection named '{collection}'")
    parts = _clean(rel)
    exclude, _ = matchers(coll)
    nokey = _nokey_matcher(coll)
    priv = macindex.manager().excluder()
    root = coll["path"].rstrip("/")

    def allowed(relpath: str, abspath: str) -> bool:
        # the collection's own rule (a Keynote deck is one folder, nothing inside it is listed; a private key is not)
        return not _secret_excluded(relpath, exclude, nokey, Path(root)) and not priv.blocked(abspath, soft=False)

    cur = root
    for i, part in enumerate(parts):                 # every step must be a real folder, never a link
        cur = os.path.join(cur, part)
        try:
            st = os.lstat(cur)
        except OSError:
            raise NotFound("folder not found")
        if S.S_ISLNK(st.st_mode) or not S.S_ISDIR(st.st_mode) or not allowed("/".join(parts[:i + 1]), cur):
            raise NotFound("folder not found")
    out = []
    try:
        entries = sorted((e for e in os.scandir(cur)), key=lambda e: e.name.lower())
    except OSError:
        raise NotFound("folder not found")
    for e in entries:
        try:
            if not e.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        relpath = "/".join(parts + [e.name])
        if not allowed(relpath, e.path):
            continue
        out.append({"name": e.name, "path": relpath, "children": _has_children(e.path, relpath, allowed)})
        if len(out) >= MAX_FOLDERS:
            break
    return {"collection": coll["name"], "path": "/".join(parts), "folders": out}


def _has_children(abspath: str, relpath: str, allowed) -> bool:
    try:
        with os.scandir(abspath) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False) and allowed(relpath + "/" + e.name, e.path):
                        return True
                except OSError:
                    continue
    except OSError:
        pass
    return False
