import json
import threading
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

    def test_copy_keeps_test_inputs_and_drops_only_module_build_output(self):
        src = self.root / "inputs"
        files = {"pom.xml": "<project/>",
                 "target/classes/X.class": "x",                          # saída do build do módulo
                 "src/test/projects/x/target/foo.txt": "fixture",        # entrada de teste (sem pom.xml ao lado)
                 "src/test/resources/A.class": "bytes",                  # entrada de teste
                 "src/test/projects/y/pom.xml": "<project/>",
                 "src/test/projects/y/target/out.txt": "build de y",     # target ao lado de um pom.xml
                 ".idea/workspace.xml": "ide"}
        for rel, text in files.items():
            f = src / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        prep = P.prepare(self.home, self.settings, P.local_bug(str(src)), self.log)
        p = prep.pristine
        self.assertTrue((p / "src/test/projects/x/target/foo.txt").exists())
        self.assertTrue((p / "src/test/resources/A.class").exists())
        self.assertFalse((p / "target").exists())
        self.assertFalse((p / "src/test/projects/y/target").exists())
        self.assertFalse((p / ".idea").exists())
        w = P.working_copy(prep, self.home)
        self.assertTrue((w / "src/test/projects/x/target/foo.txt").exists())
        self.assertTrue((w / "src/test/resources/A.class").exists())
        self.assertFalse((w / P.MARKER).exists())

    def test_local_folder_edits_are_picked_up(self):
        src = self.root / "live"
        src.mkdir()
        (src / "a.txt").write_text("1")
        bug = P.local_bug(str(src))
        first = P.prepare(self.home, self.settings, bug, self.log)
        (first.pristine / "extra.txt").write_text("keep")
        same = P.prepare(self.home, self.settings, bug, self.log)            # nada mudou: reaproveita
        self.assertTrue((same.pristine / "extra.txt").exists())
        (src / "a.txt").write_text("2222")                                    # arquivo editado
        edited = P.prepare(self.home, self.settings, bug, self.log)
        self.assertEqual((edited.pristine / "a.txt").read_text(), "2222")
        self.assertFalse((edited.pristine / "extra.txt").exists())
        (src / "b.txt").write_text("novo")                                    # arquivo novo
        self.assertTrue((P.prepare(self.home, self.settings, bug, self.log).pristine / "b.txt").exists())
        (src / "b.txt").unlink()                                              # arquivo removido
        self.assertFalse((P.prepare(self.home, self.settings, bug, self.log).pristine / "b.txt").exists())

    def test_local_folder_ignored_changes_do_not_refresh(self):
        src = self.root / "live2"
        (src / "target").mkdir(parents=True)
        (src / "pom.xml").write_text("<project/>")
        bug = P.local_bug(str(src))
        first = P.prepare(self.home, self.settings, bug, self.log)
        (first.pristine / "extra.txt").write_text("keep")
        (src / "target" / "novo.class").write_text("saída do build")          # não é copiado: não conta
        again = P.prepare(self.home, self.settings, bug, self.log)
        self.assertTrue((again.pristine / "extra.txt").exists())

    def test_local_folder_refresh_flag_forces_recopy(self):
        src = self.root / "live3"
        src.mkdir()
        (src / "a.txt").write_text("1")
        bug = P.local_bug(str(src))
        first = P.prepare(self.home, self.settings, bug, self.log)
        (first.pristine / "extra.txt").write_text("keep")
        again = P.prepare(self.home, self.settings, bug, self.log, refresh=True)
        self.assertFalse((again.pristine / "extra.txt").exists())

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

    def test_working_copy_skips_bugsjar_metadata(self):
        src = self.root / "p3"
        (src / ".bugs-dot-jar").mkdir(parents=True)
        (src / ".bugs-dot-jar" / "info.json").write_text("{}")
        (src / "a.txt").write_text("1")
        prep = P.prepare(self.home, self.settings, P.local_bug(str(src)), self.log)
        w = P.working_copy(prep, self.home)
        self.assertTrue((w / "a.txt").exists())
        self.assertFalse((w / ".bugs-dot-jar").exists())

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


    def test_src_truth_with_space_and_accent_paths(self):
        b = self.root / "pasta com espaço e ç" / "b"
        f = self.root / "pasta com espaço e ç" / "f"
        for d, ret in ((b, 1), (f, 2)):
            (d / "org/x").mkdir(parents=True)
            (d / "org/x/Foo.java").write_text(f"package org.x;\nclass Foo {{\n  int f() {{ return {ret}; }}\n}}\n")
        faults = P._src_truth(b, f)
        self.assertEqual([(x.cls, x.line) for x in faults], [("org.x.Foo", 3)])
        (f / "org/x/Foo.java").write_bytes((b / "org/x/Foo.java").read_bytes())
        with self.assertRaises(StageError):
            P._src_truth(b, f)

    def test_local_repo_url_never_modifies_original(self):
        repo = self.root / "orig"
        repo.mkdir()
        _git(repo, "init", "-q")
        (repo / "a.txt").write_text("1")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "c")
        bug = P.local_bug(f"{repo}@{_head(repo)}")
        prep = P.prepare(self.home, self.settings, bug, self.log)
        self.assertTrue((prep.pristine / "a.txt").exists())
        self.assertFalse((repo / ".git" / "worktrees").exists())
        P.prepare(self.home, self.settings, bug, self.log, refresh=True)
        self.assertFalse((repo / ".git" / "worktrees").exists())

    def test_test_fqcn_most_specific_root(self):
        self.assertEqual(P._test_fqcn("mod/tests/src/test/java/x/T.java"), "x.T")
        self.assertEqual(P._test_fqcn("src/test/java/a/BTest.java"), "a.BTest")
        self.assertEqual(P._test_fqcn("test/org/y/ZTest.java"), "org.y.ZTest")

    def test_deleted_test_files_not_relevant(self):
        fix = self.fake_bugsjar()
        repo = self.root / "bj" / "Proj"
        _git(repo, "checkout", "-q", fix)
        (repo / "src/test/java/a/OldTest.java").write_text("package a;\nclass OldTest {}\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "add")
        _git(repo, "rm", "-q", "src/test/java/a/OldTest.java")
        _git(repo, "commit", "-qm", "del")
        self.assertEqual(P._changed_tests(repo, "HEAD"), [])

    def test_rmtree_readonly(self):
        d = self.root / "ro"
        (d / "sub").mkdir(parents=True)
        f = d / "sub" / "obj"
        f.write_text("x")
        import os, stat
        os.chmod(f, stat.S_IREAD)
        P._rmtree(d)
        self.assertFalse(d.exists())

    def test_concurrent_prepare_same_bug(self):
        src = self.root / "conc"
        src.mkdir()
        (src / "a.txt").write_text("1")
        bug = P.local_bug(str(src))
        res, errs = [], []

        def work():
            try:
                res.append(P.prepare(self.home, self.settings, bug, self.log))
            except Exception as e:  # noqa: BLE001
                errs.append(e)

        ts = [threading.Thread(target=work) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errs, [])
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0].pristine, res[1].pristine)
        self.assertTrue((res[0].pristine / "a.txt").exists())


if __name__ == "__main__":
    unittest.main()
