import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sbfl import prepare as P
from sbfl.adapters.base import Bug
from sbfl.errors import StageError
from sbfl.home import Home, load_settings
from sbfl.toolchain.detect import WslStatus


def _git(repo, *a):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                   check=True, capture_output=True)


def _head(repo):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                          text=True).stdout.strip()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = Home(self.root / "home").ensure()
        self.settings = load_settings(self.home)
        self.log = self.root / "log.txt"

    def fake_bugsjar(self):
        root = self.root / "bj"
        repo = root / "Proj"
        (repo / "src/main/java/a").mkdir(parents=True)
        (repo / "src/test/java/a").mkdir(parents=True)
        _git(root, "init", "-q", str(repo))
        (repo / "src/main/java/a/B.java").write_text("package a;\nclass B {\n  int f() { return 1; }\n}\n")
        (repo / "src/test/java/a/BTest.java").write_text("package a;\nclass BTest {}\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "buggy")
        buggy = _head(repo)
        (repo / "src/main/java/a/B.java").write_text("package a;\nclass B {\n  int f() { return 2; }\n}\n")
        (repo / "src/test/java/a/BTest.java").write_text("package a;\nclass BTest { int x; }\n")
        _git(repo, "commit", "-qam", "fix")
        fix = _head(repo)
        _git(repo, "branch", f"bugs-dot-jar_X-1_{fix}", buggy)
        self.settings.set("bugsjar", "root", str(root))
        return fix


class PrepareTests(Base):
    def test_local_dir_copy_does_not_touch_source(self):
        src = self.root / "meu proj"
        (src / "target").mkdir(parents=True)
        (src / "target" / "x").write_text("x")
        (src / "pom.xml").write_text("<project/>")
        before = (src.stat().st_mtime_ns, (src / "pom.xml").stat().st_mtime_ns)
        bug = P.local_bug(str(src))
        self.assertEqual(bug.benchmark, "local")
        prep = P.prepare(self.home, self.settings, bug, self.log)
        self.assertTrue((prep.pristine / "pom.xml").exists())
        self.assertFalse((prep.pristine / "target").exists())
        self.assertIsNone(prep.relevant_tests)
        self.assertEqual(before, (src.stat().st_mtime_ns, (src / "pom.xml").stat().st_mtime_ns))
        self.assertTrue((src / "target" / "x").exists())

    def test_bugsjar_checkout_with_fake_repo(self):
        fix = self.fake_bugsjar()
        bugs = P.list_bugs(self.settings, "bugsjar")
        self.assertEqual(len(bugs), 1)
        self.assertEqual(bugs[0].extra["fixed"], fix)
        self.assertEqual(P.bug_from_key(self.settings, bugs[0].key).extra, bugs[0].extra)
        prep = P.prepare(self.home, self.settings, bugs[0], self.log)
        self.assertEqual(prep.relevant_tests, ["a.BTest"])
        self.assertTrue(prep.truth)
        self.assertEqual(prep.truth[0].cls, "a.B")
        self.assertEqual(prep.module, ".")
        again = P.prepare(self.home, self.settings, bugs[0], self.log, refresh=True)
        self.assertEqual(again.relevant_tests, ["a.BTest"])

    def test_prepare_reuses_marker(self):
        src = self.root / "p1"
        src.mkdir()
        (src / "a.txt").write_text("1")
        bug = P.local_bug(str(src))
        first = P.prepare(self.home, self.settings, bug, self.log)
        (first.pristine / "extra.txt").write_text("keep")
        second = P.prepare(self.home, self.settings, bug, self.log)
        self.assertEqual(first.pristine, second.pristine)
        self.assertTrue((second.pristine / "extra.txt").exists())
        json.loads((first.pristine / P.MARKER).read_text(encoding="utf-8"))
        third = P.prepare(self.home, self.settings, bug, self.log, refresh=True)
        self.assertFalse((third.pristine / "extra.txt").exists())

    def test_working_copy_is_independent(self):
        src = self.root / "p2"
        (src / "target").mkdir(parents=True)
        (src / "a.txt").write_text("1")
        prep = P.prepare(self.home, self.settings, P.local_bug(str(src)), self.log)
        w = P.working_copy(prep, self.home)
        self.assertNotEqual(w, prep.pristine)
        (w / "a.txt").write_text("2")
        self.assertEqual((prep.pristine / "a.txt").read_text(), "1")
        self.assertNotEqual(w, P.working_copy(prep, self.home))

    def test_local_url_commit_spec(self):
        b = P.local_bug("https://github.com/x/repo.git@abcdef1234567890")
        self.assertEqual((b.project, b.bug_id), ("repo", "abcdef123456"))
        with self.assertRaises(ValueError):
            P.local_bug("nao existe")

    def test_local_key_raises(self):
        with self.assertRaises(ValueError):
            P.bug_from_key(self.settings, "local/x/dir-1")

    def test_to_wsl_path(self):
        self.assertEqual(P.to_wsl_path(Path("E:\\a\\b")), "/mnt/e/a/b")
        self.assertEqual(P.to_wsl_path("c:/x y/z"), "/mnt/c/x y/z")

    def test_d4j_unavailable_message(self):
        wsl = WslStatus(False, [], None, "WSL indisponível: não está instalado")
        bug = Bug("d4j", "Lang", "1")
        with mock.patch.object(P, "wsl_status", return_value=wsl), \
                mock.patch.object(P, "defects4j_cmd", return_value=None):
            with self.assertRaises(StageError) as cm:
                P.prepare(self.home, self.settings, bug, self.log)
        self.assertEqual(cm.exception.status, "checkout_falhou")
        self.assertIn("WSL", cm.exception.detail)
        self.assertFalse(P._pristine_dir(self.home, bug).exists())


if __name__ == "__main__":
    unittest.main()
