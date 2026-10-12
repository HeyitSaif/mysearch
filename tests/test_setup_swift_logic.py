"""Builds and runs tests/swift/main.swift: the Swift logic added for the DMG install that needs no window or service (the
update check's version order, schedule and request; the daemon's choice of Python; the snapshot PNG writer; the Quick Search
shortcut rules and key reading). The harness is compiled together with app/Updater.swift, app/Daemon.swift,
app/SnapshotPNG.swift and app/Hotkeys.swift, so it tests the shipped code. Skipped
without swiftc. No network, no window, no service.
Run: .venv/bin/python -m unittest discover -s tests -t ."""

import platform
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(platform.system() == "Darwin" and shutil.which("swiftc"), "needs macOS and swiftc")
class SwiftLogic(unittest.TestCase):
    def test_harness_passes(self):
        out = Path(tempfile.mkdtemp(prefix="mse-swift-"))
        exe = out / "logic"
        build = subprocess.run(
            ["swiftc", "-swift-version", "5", "-module-cache-path", str(out / "modcache"), "-o", str(exe),
             str(ROOT / "app" / "Updater.swift"), str(ROOT / "app" / "Daemon.swift"), str(ROOT / "app" / "SnapshotPNG.swift"),
             str(ROOT / "app" / "Hotkeys.swift"),
             str(ROOT / "tests" / "swift" / "main.swift")], capture_output=True, text=True, timeout=600)
        self.assertEqual(build.returncode, 0, build.stderr[-3000:])
        run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=120, cwd=out)
        failed = [l for l in run.stdout.splitlines() if l.startswith("FAIL")]
        self.assertEqual(failed, [], "\n".join(failed))
        self.assertEqual(run.returncode, 0, run.stdout[-2000:] + run.stderr[-2000:])
        self.assertIn("ALL PASSED", run.stdout)
        self.assertGreater(sum(1 for l in run.stdout.splitlines() if l.startswith("PASS")), 40)


if __name__ == "__main__":
    unittest.main()
