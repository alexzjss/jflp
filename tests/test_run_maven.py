from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from sbfl.adapters.base import Bug
from sbfl.home import Home, load_settings
from sbfl.prepare import Prepared
from sbfl.run_maven import AttemptConfig, run_maven, test_selection_args
from sbfl.toolchain.detect import Jdk, Maven
from sbfl.toolchain.profiles import ToolEnv

DATA = Path(__file__).resolve().parent.parent / "sbfl" / "selftest_data" / "fixed"


def _prep(tmp, tests, module="."):
    return Prepared(Bug("local", "p", "1"), tmp, module, tests, [], {})


class SelectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_selection_args_relevant(self):
        p = _prep(self.tmp, ["a.BTest", "a.CTest"])
        self.assertEqual(test_selection_args(p, "relevant", self.tmp), ["-Dtest=a.BTest,a.CTest"])

    def test_selection_args_all(self):
        self.assertEqual(test_selection_args(_prep(self.tmp, ["a.BTest"]), "all", self.tmp), [])
        self.assertEqual(test_selection_args(_prep(self.tmp, None), "relevant", self.tmp), [])
        self.assertEqual(test_selection_args(_prep(self.tmp, []), "relevant", self.tmp), [])

    def test_selection_args_long_list_uses_includes_file(self):
        tests = [f"pkg.sub.Classe{i:04d}Test" for i in range(400)] + ["pkg.Outer$Inner"]
        pristine = self.tmp / "pristine"
        (pristine / "mod").mkdir(parents=True)
        prep = _prep(pristine, tests, "mod")
        args = test_selection_args(prep, "relevant", self.tmp)
        self.assertFalse((pristine / "mod" / "jflp-tests.txt").exists())
        self.assertEqual(args, ["-Dsurefire.includesFile=jflp-tests.txt"])
        raw = (self.tmp / "mod" / "jflp-tests.txt").read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\r", raw)
        lines = raw.decode("utf-8").splitlines()
        self.assertIn("pkg/sub/Classe0000Test.java", lines)
        self.assertIn("pkg/Outer.java", lines)
        self.assertEqual(len(lines), 401)


class RunMavenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = Home(self.tmp / "Pasta Ção")
        self.settings = load_settings(self.home)

    def _fake_toolenv(self):
        mhome = self.tmp / "maven"
        (mhome / "bin").mkdir(parents=True)
        script = mhome / "bin" / "fake.py"
        script.write_text(textwrap.dedent("""\
            import os, sys
            d = os.path.join(os.getcwd(), "target", "surefire-reports")
            os.makedirs(d, exist_ok=True)
            open(os.path.join(d, "TEST-a.BTest.xml"), "w").write(
                '<testsuite><testcase classname="a.BTest" name="t1"><failure message="x"/></testcase>'
                '<testcase classname="a.BTest" name="t2"/></testsuite>')
            print("ARGS", " ".join(sys.argv[1:]))
            import json
            json.dump(sys.argv[1:], open(os.path.join(os.getcwd(), "argv.json"), "w"))
            """), encoding="utf-8")
        if os.name == "nt":
            exe = mhome / "bin" / "mvn.cmd"
            exe.write_text(f'@"{sys.executable}" "{script}" %*\r\n')
        else:
            exe = mhome / "bin" / "mvn"
            exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
            exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        jdk = Jdk(self.tmp / "jdk", 11, "11.0.1", "managed")
        return ToolEnv(jdk, Maven(mhome, (3, 6, 3), "managed"), dict(os.environ), self.home.m2)

    def test_run_maven_with_fake_mvn(self):
        work = self.tmp / "w"
        work.mkdir()
        te = self._fake_toolenv()
        log = self.tmp / "logs" / "run.log"
        r = run_maven(self.home, self.settings, _prep(work, ["a.BTest"]), work,
                      AttemptConfig(), log, toolenv=te)
        self.assertEqual(r.rc, 0)
        self.assertFalse(r.timed_out)
        self.assertEqual(r.tests, {"a.BTest#t1": "failed", "a.BTest#t2": "passed"})
        self.assertEqual(r.outputs["surefire"], work / "target" / "surefire-reports")
        self.assertIn("-Dtest=a.BTest", r.command)
        self.assertIn("-Dmaven.test.failure.ignore=true", r.command)
        self.assertEqual(r.toolenv["maven"]["version"], "3.6.3")
        self.assertIn("ARGS", log.read_text(encoding="utf-8"))

    def test_unblock_http_adds_settings_file(self):
        work = self.tmp / "w"
        work.mkdir()
        te = self._fake_toolenv()
        r = run_maven(self.home, self.settings, _prep(work, ["a.BTest"]), work,
                      AttemptConfig(fixes_on=frozenset({"unblock-http"})), self.tmp / "l.log", toolenv=te)
        i = r.command.index("-s")
        f = Path(r.command[i + 1])
        self.assertEqual(f, self.home.tools / "jflp-settings-unblock.xml")
        text = f.read_text(encoding="utf-8")
        self.assertIn("maven-default-http-blocker", text)
        self.assertIn("<blocked>false</blocked>", text)
        r = run_maven(self.home, self.settings, _prep(work, ["a.BTest"]), work,
                      AttemptConfig(), self.tmp / "l2.log", toolenv=te)
        self.assertNotIn("-s", r.command)

    def test_fake_mvn_receives_comma_list_as_one_argument(self):
        work = self.tmp / "w"
        work.mkdir()
        run_maven(self.home, self.settings, _prep(work, ["a.BTest", "a.CTest"]), work,
                  AttemptConfig(), self.tmp / "c.log", toolenv=self._fake_toolenv())
        argv = json.loads((work / "argv.json").read_text())
        self.assertEqual([a for a in argv if a.startswith("-Dtest")], ["-Dtest=a.BTest,a.CTest"])

    def test_config_key_is_deterministic(self):
        a = AttemptConfig(fixes_on=frozenset({"b", "a"}), params={"y": 1, "x": 2})
        b = AttemptConfig(fixes_on=frozenset({"a", "b"}), params={"x": 2, "y": 1})
        self.assertEqual(a.key(), b.key())
        self.assertNotEqual(a.key(), AttemptConfig().key())

    @unittest.skipUnless(shutil.which("mvn") and shutil.which("java"), "requer mvn e java")
    def test_run_maven_real_selftest_project(self):
        work = self.tmp / "pasta com espaço"
        shutil.copytree(DATA, work)
        self.settings.set("run", "timeout_s", 1800)
        r = run_maven(self.home, self.settings, _prep(work, None), work, AttemptConfig(),
                      self.tmp / "real.log")
        self.assertIn("surefire", r.outputs, r.log.read_text(encoding="utf-8", errors="replace")[-2000:])
        self.assertTrue(r.tests)


if __name__ == "__main__":
    unittest.main()
