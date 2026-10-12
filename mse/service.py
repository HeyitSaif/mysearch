"""Run the daemon as a macOS LaunchAgent: starts at login, restarts if it stops."""

import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

from . import paths

LABEL = "com.mysearch.daemon"
APP_EXECUTABLE = Path("/Applications/MySearch.app/Contents/MacOS/MySearch")
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def own_app_executable() -> Path | None:
    """The MySearch.app this code runs from, when it runs from the app's bundled runtime (the DMG install, no clone), or the
    executable the app itself names in MSE_APP_EXECUTABLE. None for a clone with a venv."""
    env = os.environ.get("MSE_APP_EXECUTABLE")
    if env and Path(env).is_file():
        return Path(env)
    from .setup_models import app_bundle
    bundle = app_bundle()
    exe = bundle / "Contents" / "MacOS" / "MySearch" if bundle else None
    return exe if exe and exe.is_file() else None


def unstable_location(path: str | Path) -> str | None:
    """Why a LaunchAgent must not point at this app: a mounted disk image goes away on eject, and a quarantined app is
    run from a randomised read-only copy. None when the location is fine."""
    p = str(path)
    if p.startswith("/Volumes/"):
        return "MySearch.app is running from a disk image or an external volume. Drag it to Applications first."
    if "/AppTranslocation/" in p:
        return "macOS is running MySearch.app from a temporary read-only copy. Move it to Applications, then open it again."
    return None


def plist(app_executable: str | Path | None = None) -> dict:
    """With MySearch.app installed, launchd starts the app's own executable in --daemon mode, which spawns the Python
    service as its child: macOS then attributes the service (and the helper's crawl) to MySearch.app, so one Full Disk
    Access grant on the app covers them. Without the app, launchd runs the venv's Python directly (the old setup).
    Run from the app's bundled runtime, the app that holds it is the one launchd starts, wherever it is installed."""
    app = Path(app_executable) if app_executable else own_app_executable() or APP_EXECUTABLE
    python = str(paths.PROJECT / ".venv" / "bin" / "python")
    args = [str(app), "--daemon"] if app.exists() else [python, "-m", "mse", "serve"]
    return {
        "Label": LABEL,
        "ProgramArguments": args,
        "WorkingDirectory": str(paths.PROJECT),
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "HF_HUB_OFFLINE": "1",
            "PYTHONUNBUFFERED": "1",
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 15,
        # A search is something the user is waiting for (Quick Search, the file-name list while typing); the default process
        # type lets macOS schedule the service like a background job. Background work keeps its own low priority (the
        # embedding worker and the fine-tune run under `nice -n 10`, the helper's crawl and OCR renice themselves).
        # Takes effect when the agent is re-installed (`mse service install`); a running agent keeps its old plist.
        "ProcessType": "Interactive",
        # launchd sends SIGKILL this long after SIGTERM. The supervisor waits up to 30 s for the service to stop (Daemon.swift),
        # so launchd must wait longer, or the supervisor dies first and the service is left running with the port.
        "ExitTimeOut": 45,
        "StandardOutPath": str(paths.LOGS / "launchd.log"),
        "StandardErrorPath": str(paths.LOGS / "launchd.log"),
    }


def install(app_executable: str | Path | None = None) -> str:
    where = unstable_location(app_executable or own_app_executable() or "")
    if where:
        raise RuntimeError(where)
    paths.ensure_dirs()
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], capture_output=True)
    with open(PLIST, "wb") as f:
        plistlib.dump(plist(app_executable), f)
    r = subprocess.run(["launchctl", "bootstrap", _domain(), str(PLIST)], capture_output=True, text=True)
    if r.returncode != 0 and "already" not in r.stderr.lower():
        raise RuntimeError(r.stderr.strip())
    subprocess.run(["launchctl", "enable", f"{_domain()}/{LABEL}"], capture_output=True)
    subprocess.run(["launchctl", "kickstart", "-k", f"{_domain()}/{LABEL}"], capture_output=True)
    return str(PLIST)


def uninstall() -> None:
    subprocess.run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], capture_output=True)
    if PLIST.exists():
        PLIST.unlink()


def wait_ready(timeout: float = 20.0) -> bool:
    """True once the daemon answers /api/status (it needs a few seconds to load after a start)."""
    from .cli import daemon_up
    deadline = time.monotonic() + timeout
    while True:
        if daemon_up():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def restart(wait: float = 20.0) -> bool:
    """Restart and wait until the daemon answers, so `mse service restart && mse files crawl --force` works."""
    subprocess.run(["launchctl", "kickstart", "-k", f"{_domain()}/{LABEL}"], capture_output=True)
    return wait_ready(wait) if wait else True


def status() -> str:
    r = subprocess.run(["launchctl", "print", f"{_domain()}/{LABEL}"], capture_output=True, text=True)
    if r.returncode != 0:
        return "not installed"
    state = next((l.strip() for l in r.stdout.splitlines() if l.strip().startswith("state =")), "state = ?")
    pid = next((l.strip() for l in r.stdout.splitlines() if l.strip().startswith("pid =")), "")
    return f"installed ({state}{', ' + pid if pid else ''})"


if __name__ == "__main__":
    print(status(), file=sys.stderr)
