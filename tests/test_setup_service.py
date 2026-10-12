"""How the LaunchAgent follows the app when the package runs from the app's bundled runtime (the DMG install): the app that
holds the runtime is the one launchd starts, wherever it is installed, and a location that goes away (a mounted disk image, a
quarantined copy) is refused. Nothing here calls launchctl or writes a plist.
Run: .venv/bin/python -m unittest discover -s tests -t ."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import paths, service  # noqa: E402


def fake_app(root: Path, name="MySearch.app") -> Path:
    exe = root / name / "Contents" / "MacOS" / "MySearch"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    (root / name / "Contents" / "Resources").mkdir()
    return exe


class AppFollowing(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mse-svc-"))

    def test_run_from_a_bundled_runtime_launchd_starts_that_app(self):
        exe = fake_app(self.tmp)
        with mock.patch.object(paths, "PROJECT", exe.parent.parent / "Resources"), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MSE_APP_EXECUTABLE", None)
            self.assertEqual(service.own_app_executable(), exe)
            self.assertEqual(service.plist()["ProgramArguments"], [str(exe), "--daemon"])

    def test_the_app_can_name_itself(self):
        exe = fake_app(self.tmp, "Elsewhere.app")
        with mock.patch.dict(os.environ, {"MSE_APP_EXECUTABLE": str(exe)}):
            self.assertEqual(service.own_app_executable(), exe)
            self.assertEqual(service.plist()["ProgramArguments"], [str(exe), "--daemon"])

    def test_a_clone_with_a_venv_is_unchanged(self):
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(service, "APP_EXECUTABLE", self.tmp / "none"):
            os.environ.pop("MSE_APP_EXECUTABLE", None)
            self.assertIsNone(service.own_app_executable())
            args = service.plist()["ProgramArguments"]
        self.assertEqual(args[1:], ["-m", "mse", "serve"])
        self.assertTrue(args[0].endswith(".venv/bin/python"))

    def test_an_explicit_path_still_wins(self):
        exe = fake_app(self.tmp)
        self.assertEqual(service.plist(exe)["ProgramArguments"], [str(exe), "--daemon"])

    def test_a_disk_image_or_a_translocated_copy_is_refused(self):
        for bad in ("/Volumes/MySearch/MySearch.app/Contents/MacOS/MySearch",
                    "/private/var/folders/xx/yy/T/AppTranslocation/ABC/d/MySearch.app/Contents/MacOS/MySearch"):
            with self.subTest(path=bad):
                self.assertIsNotNone(service.unstable_location(bad))
                with mock.patch("mse.service.subprocess.run") as run:
                    with self.assertRaises(RuntimeError):
                        service.install(bad)
                    run.assert_not_called()               # refused before launchctl is touched
        self.assertIsNone(service.unstable_location("/Applications/MySearch.app/Contents/MacOS/MySearch"))
        self.assertIsNone(service.unstable_location(""))


if __name__ == "__main__":
    unittest.main()
