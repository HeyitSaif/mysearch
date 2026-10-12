"""Temporary folders for tests, resolved and indexable.

Two things about a temporary folder differ between machines and are easy to get wrong in a test:

* On macOS the per-user temp folder is a symlink: $TMPDIR is /var/folders/... and the real path is
  /private/var/folders/.... MySearch stores and prints real paths (a collection folder, a search hit, a crawl root), so a
  test that compares against the folder it created must compare against the resolved folder. `mkdtemp` here returns the
  real path.
* /private/var is one of the locations the whole-Mac name search never indexes (mse.macindex.HARD_ABS: caches, logs,
  the system). A crawl root under $TMPDIR is therefore refused on a Mac whose $TMPDIR is the default one, and under the
  sandbox of scripts/hooks/pre-push, which puts $TMPDIR inside `mktemp -d`. `indexable_tmp()` is the first temp base
  the rules allow: $TMPDIR when they do, else /tmp (/private/tmp is not a never-indexed location).

Both are used by the mac_index tests and the saved-search CLI test; other tests keep tempfile.mkdtemp."""

import os
import tempfile

_INDEXABLE: str | None = None


def resolved_tmp() -> str:
    """The real path of the temp folder of this run ($TMPDIR, else the platform default)."""
    return os.path.realpath(os.environ.get("TMPDIR") or tempfile.gettempdir())


def indexable_tmp() -> str:
    """A resolved temp base that mac_index accepts as a crawl root: $TMPDIR when the hard rules allow it, else /tmp.
    Chosen once per process. If neither is allowed and writable the plain temp folder is returned, so the test still
    fails with the rules' own message rather than here."""
    global _INDEXABLE
    if _INDEXABLE is None:
        from mse import macindex as mi

        rules = mi.Excluder({"exclude": []}, h=mi.home())
        base = resolved_tmp()
        for cand in (base, os.path.realpath("/tmp")):
            if os.path.isdir(cand) and os.access(cand, os.W_OK) and rules.root_allowed(cand):
                base = cand
                break
        _INDEXABLE = base
    return _INDEXABLE


def mkdtemp(prefix: str = "mse-test-", indexable: bool = False) -> str:
    """tempfile.mkdtemp, but the real path; with indexable=True the folder lies under indexable_tmp()."""
    base = indexable_tmp() if indexable else resolved_tmp()
    return os.path.realpath(tempfile.mkdtemp(prefix=prefix, dir=base))
