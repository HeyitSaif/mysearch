"""The test package. Imported first by `python -m unittest discover -s tests -t .`, before any test module.

A headless run puts HOME, TMPDIR and MSE_HOME into a throw-away folder (scripts/hooks/pre-push, docs/RELEASING.md)
so that no real data folder, Keychain item or service is touched. On macOS `mktemp -d` answers /var/folders/..., which
is /private/var/folders/... — one of the locations the whole-Mac name search never indexes (mse.macindex.HARD_ABS).
A home folder there cannot be a crawl root, so every test that uses `~` as the default root would fail with the rules'
own message. Such a HOME is therefore moved, once, to a fresh folder under a base the rules allow (tests/tmpdirs.py):
still not the real home folder, still throw-away. MSE_HOME (the data folder) is left as given. A HOME the rules allow
(a real one, or a sandbox under /tmp) is not touched.

The decision is taken in a child interpreter: mse.config and mse.paths read the home folder when they are imported,
so HOME has to be final before this process imports mse at all."""

import os
import subprocess
import sys
import tempfile

# The data folder must be the throw-away one before mse is imported anywhere, exactly as every test module sets it.
os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

_CHECK = """
import os, sys
from mse import macindex as mi
from tests import tmpdirs
home = os.path.realpath(sys.argv[1])
allowed = mi.Excluder({"exclude": []}, h=home).root_allowed(home)
print("ok" if allowed else tmpdirs.indexable_tmp())
"""


def _indexable_home() -> None:
    if os.environ.get("MSE_TEST_HOME_CHECK") == "1":       # this is the child itself: it imports this package too
        return
    home = os.environ.get("HOME") or os.path.expanduser("~")
    try:
        r = subprocess.run([sys.executable, "-c", _CHECK, home], capture_output=True, text=True, timeout=60,
                           cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           env={**os.environ, "MSE_TEST_HOME_CHECK": "1"})
        verdict = r.stdout.strip().splitlines()[-1] if r.returncode == 0 and r.stdout.strip() else "ok"
    except (OSError, subprocess.SubprocessError):
        verdict = "ok"                                   # cannot tell: leave HOME alone, the tests will say why
    if verdict == "ok":
        return
    moved = os.path.realpath(tempfile.mkdtemp(prefix="mse-test-home-", dir=verdict))
    os.environ["HOME"] = moved
    os.environ["MSE_TEST_HOME_MOVED_FROM"] = home      # for anyone reading the environment of a failing run


_indexable_home()
