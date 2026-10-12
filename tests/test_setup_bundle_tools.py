"""scripts/bundle_tools.py: the build-time helpers that make the bundled Python runtime relocatable and check that nothing in
the app refers to the machine that built it. Mach-O cases compile a tiny dylib with clang (skipped without it); the rest use
plain files. Nothing is installed and no app is built.
Run: .venv/bin/python -m unittest discover -s tests -t ."""

import contextlib
import getpass
import importlib.util
import io
import os
import platform
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("bundle_tools", ROOT / "scripts" / "bundle_tools.py")
bt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bt)

MACHO = b"\xcf\xfa\xed\xfe" + b"\0" * 28


def make_app(root: Path) -> Path:
    app = root / "Test.app"
    (app / "Contents" / "Resources" / "runtime" / "lib" / "python3.13" / "site-packages" / "pkg").mkdir(parents=True)
    (app / "Contents" / "MacOS").mkdir()
    return app


def audit(app, *forbid):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = bt.audit(str(app), list(forbid))
    return rc, out.getvalue()


class Detection(unittest.TestCase):
    def test_macho_by_magic_number_not_by_name(self):
        d = Path(tempfile.mkdtemp(prefix="mse-bt-"))
        (d / "a.so").write_bytes(MACHO)
        (d / "tool").write_bytes(b"\xca\xfe\xba\xbe" + b"\0" * 28)           # a universal binary has no extension at all
        (d / "text.so").write_text("not a binary")
        (d / "link").symlink_to(d / "a.so")
        self.assertEqual([Path(p).name for p in bt.machos(str(d))], ["a.so", "tool"])
        self.assertTrue(bt.is_macho(str(d / "a.so")))
        self.assertFalse(bt.is_macho(str(d / "link")))
        self.assertFalse(bt.is_macho(str(d / "missing")))


class Scrub(unittest.TestCase):
    def test_text_files_are_rewritten_and_binaries_are_left_alone(self):
        d = Path(tempfile.mkdtemp(prefix="mse-bt-"))
        (d / "cfg.py").write_text("prefix = '/build/cache/python/cpython-3.13'\nname = 'x'\n")
        (d / "lib.dylib").write_bytes(MACHO + b"/build/cache/python/cpython-3.13")
        log = bt.scrub(str(d), [("/build/cache/python/cpython-3.13", "/Applications/MySearch.app/Contents/Resources/runtime")])
        self.assertEqual((d / "cfg.py").read_text(), "prefix = '/Applications/MySearch.app/Contents/Resources/runtime'\nname = 'x'\n")
        self.assertIn(b"/build/cache", (d / "lib.dylib").read_bytes())
        self.assertEqual(sorted(l.split(":")[0] for l in log), ["cfg.py", "lib.dylib"])
        self.assertTrue(any("left alone" in l for l in log))


class AuditText(unittest.TestCase):
    def test_a_clean_bundle_passes_and_third_party_leftovers_are_only_reported(self):
        app = make_app(Path(tempfile.mkdtemp(prefix="mse-bt-")))
        pkg = app / "Contents" / "Resources" / "runtime" / "lib" / "python3.13" / "site-packages" / "pkg"
        (pkg / "mod.py").write_text("PATH = '/usr/bin'\n")
        (pkg / "docs.txt").write_text("built with /opt/homebrew/Cellar/x/1.0 in /var/folders/zz/upstream/T/build-env-1\n")
        rc, out = audit(app)
        self.assertEqual(rc, 0, out)
        self.assertIn("result: OK", out)
        self.assertIn("info: a Homebrew prefix", out)
        self.assertIn("info: a build temp folder", out)
        self.assertIn("pkg", out)

    def test_this_machines_home_and_the_build_folders_fail(self):
        home = os.path.realpath(os.path.expanduser("~"))
        for what, text in (("the home folder", f"x = '{home}/projects/secret'\n"),
                           ("the user's folder", f"x = '/Users/{getpass.getuser()}/Developer'\n"),
                           ("a folder named by the caller", "x = '/srv/builds/mysearch-1234/tree'\n")):
            with self.subTest(what):
                app = make_app(Path(tempfile.mkdtemp(prefix="mse-bt-")))
                (app / "Contents" / "Resources" / "runtime" / "lib" / "python3.13" / "site-packages" / "pkg" / "mod.py").write_text(text)
                rc, out = audit(app, "/srv/builds/mysearch-1234")
                self.assertEqual(rc, 1, out)
                self.assertIn("HARD findings", out)

    def test_an_absolute_symlink_fails(self):
        app = make_app(Path(tempfile.mkdtemp(prefix="mse-bt-")))
        (app / "Contents" / "Resources" / "runtime" / "bin").mkdir()
        (app / "Contents" / "Resources" / "runtime" / "bin" / "python3").symlink_to("/usr/bin/python3")
        rc, out = audit(app)
        self.assertEqual(rc, 1)
        self.assertIn("symlink", out)


@unittest.skipUnless(platform.system() == "Darwin" and shutil.which("clang") and shutil.which("install_name_tool"), "needs clang and install_name_tool")
class MachoLoadCommands(unittest.TestCase):
    def build_dylib(self, d: Path, name: str, *flags: str) -> Path:
        src = d / "f.c"
        src.write_text("int f(void) { return 1; }\n")
        out = d / name
        subprocess.run(["clang", "-dynamiclib", "-o", str(out), str(src), *flags], check=True, capture_output=True)
        return out

    def test_fix_removes_absolute_rpaths_and_turns_placeholder_names_into_relative_ones(self):
        d = Path(tempfile.mkdtemp(prefix="mse-bt-"))
        lib = self.build_dylib(d, "libx.dylib", "-install_name", "/DLC/pkg/.dylibs/libx.dylib", "-Wl,-rpath,/opt/nowhere/lib", "-Wl,-rpath,@loader_path")
        self.assertEqual(bt.rpaths(str(lib)), ["/opt/nowhere/lib", "@loader_path"])
        self.assertEqual(bt.install_id(str(lib)), "/DLC/pkg/.dylibs/libx.dylib")
        changes = bt.fix(str(d))
        self.assertEqual(bt.rpaths(str(lib)), ["@loader_path"])
        self.assertEqual(bt.install_id(str(lib)), "@rpath/libx.dylib")
        self.assertEqual(len(changes), 2, changes)
        self.assertEqual(bt.fix(str(d)), [])                       # a second run finds nothing to do

    def test_a_dependency_on_a_placeholder_path_is_pointed_at_the_real_file(self):
        d = Path(tempfile.mkdtemp(prefix="mse-bt-"))
        (d / "pkg" / ".dylibs").mkdir(parents=True)
        base = self.build_dylib(d / "pkg" / ".dylibs", "libbase.dylib", "-install_name", "@loader_path/libbase.dylib")
        (d / "pkg" / "h.c").write_text("extern int f(void);\nint h(void) { return f(); }\n")
        user = d / "pkg" / "user.so"
        subprocess.run(["clang", "-dynamiclib", "-o", str(user), str(d / "pkg" / "h.c"), str(base)], check=True, capture_output=True)
        # the way delocate leaves a copied library: its name is a placeholder under /DLC/<package>/
        subprocess.run(["install_name_tool", "-change", "@loader_path/libbase.dylib", "/DLC/pkg/.dylibs/libbase.dylib", str(user)],
                       check=True, capture_output=True)
        self.assertIn("/DLC/pkg/.dylibs/libbase.dylib", bt.dependencies(str(user)))
        bt.fix(str(d), site=str(d))
        self.assertEqual([x for x in bt.dependencies(str(user)) if "libbase" in x], ["@loader_path/.dylibs/libbase.dylib"])

    def test_audit_flags_a_dependency_or_name_that_points_at_this_machine(self):
        d = Path(tempfile.mkdtemp(prefix="mse-bt-"))
        app = make_app(d)
        res = app / "Contents" / "Resources" / "runtime" / "lib" / "python3.13" / "site-packages" / "pkg"
        good = self.build_dylib(res, "libgood.dylib", "-install_name", "@rpath/libgood.dylib")
        rc, out = audit(app)
        self.assertEqual(rc, 0, out)
        bad = self.build_dylib(res, "libbad.dylib", "-install_name", "/Users/alex/build/libbad.dylib", "-Wl,-rpath,/opt/homebrew/lib")
        rc, out = audit(app)
        self.assertEqual(rc, 1)
        self.assertIn("absolute rpath /opt/homebrew/lib", out)
        self.assertIn("install name /Users/alex/build/libbad.dylib", out)
        bt.fix(str(res))
        # the install name is not a placeholder, so it stays a finding until the file is rebuilt; the rpath is gone
        self.assertEqual(bt.rpaths(str(bad)), [])
        self.assertEqual(bt.install_id(str(good)), "@rpath/libgood.dylib")


@unittest.skipUnless(platform.system() == "Darwin" and shutil.which("codesign"), "needs codesign")
class Entitlements(unittest.TestCase):
    """The signing tools parse entitlement files strictly: a comment with two hyphens in a row is a syntax error that
    plutil accepts and codesign rejects ("AMFIUnserializeXML"), and a rejected file leaves the old signature in place."""

    FILES = sorted((ROOT / "scripts" / "entitlements").glob("*.entitlements"))

    def test_there_are_two_files(self):
        self.assertEqual([f.name for f in self.FILES], ["MySearch.entitlements", "python-runtime.entitlements"])

    def test_no_comment_contains_a_double_hyphen(self):
        import re
        for f in self.FILES:
            for c in re.findall(r"<!--(.*?)-->", f.read_text(), re.S):
                self.assertNotIn("--", c, f.name)

    def test_codesign_embeds_every_key_of_each_file(self):
        import plistlib
        d = Path(tempfile.mkdtemp(prefix="mse-ent-"))
        for f in self.FILES:
            target = d / f.stem
            shutil.copy("/bin/ls", target)
            r = subprocess.run(["codesign", "--force", "--sign", "-", "--options", "runtime", "--entitlements", str(f), str(target)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("Failed to parse", r.stderr)
            wanted = set(plistlib.loads(f.read_bytes()))
            out = subprocess.run(["codesign", "-d", "--entitlements", ":-", str(target)], capture_output=True).stdout
            self.assertEqual(set(plistlib.loads(out[out.index(b"<?xml"):])), wanted, f.name)


if __name__ == "__main__":
    unittest.main()
