"""v0.4 seção 2: robustez em repositórios reais (AppData redirecionado, JaCoCo, tentativas, Jaguar 1, guia, CLI)."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sbfl import cli
from sbfl import execute as ex
from sbfl import status as st
from sbfl.adapters.base import Bug
from sbfl.autosearch import candidates
from sbfl.diagnose import Diagnosis, Suggestion, applicable, classify
from sbfl.diffgt import FaultLine
from sbfl.home import Home, load_settings, redirected_path
from sbfl.outputs import parse_jaguar1_log
from sbfl.prepare import Prepared, find_poms
from sbfl.run_maven import AttemptConfig, AttemptResult
from sbfl.store import load_run

BUG = Bug("local", "proj", "7")
JAVAAGENT = ("[INFO] --- maven-surefire-plugin:2.12.4:test (default-test) @ x ---\n"
             "FATAL ERROR in native method: processing of -javaagent failed\n"
             "-javaagent:C:\\m2\\org\\jacoco\\org.jacoco.agent\\0.6.2\\jacocoagent.jar=destfile=x\n"
             "[ERROR] The forked VM terminated without properly saying goodbye.\n[INFO] BUILD FAILURE\n")


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)


class AppDataTest(Tmp):
    def test_redirected_path(self):
        plain = r"C:\Users\lex\AppData\Local\jflp\.probe"
        real = r"C:\Users\lex\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Local\jflp\.probe"
        self.assertEqual(redirected_path(plain, real),
                         r"C:\Users\lex\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Local\jflp")
        self.assertIsNone(redirected_path(plain, plain.upper()))
        self.assertIsNone(redirected_path(r"C:\Users\LEX~1\x\.probe", r"C:\Users\lex\x\.probe"))   # nome curto 8.3

    def test_normal_home_not_virtualized(self):
        h = Home(self.tmp / "h")
        self.assertIsNone(h.virtualized_root())
        self.assertFalse((self.tmp / "h" / ".probe").exists())


class JacocoTest(Tmp):
    def _res(self, log):
        lp = self.tmp / "log.txt"
        lp.write_text(log, encoding="utf-8")
        return AttemptResult(1, False, 1.0, lp, {}, {}, extra={})

    def test_javaagent_crash_suggests_skip_jacoco_for_jaguar1(self):
        d = classify("jaguar1", self._res(JAVAAGENT))
        self.assertEqual(d.status, st.PLUGIN_INCOMPATIVEL)
        self.assertEqual([s.fix for s in d.suggestions], ["skip-jacoco"])
        self.assertTrue(any("javaagent" in e for e in d.evidence))

    def test_skip_jacoco_only_for_jaguar1(self):
        sugs = [{"fix": "skip-jacoco"}]
        self.assertEqual(applicable("jaguar1", sugs), sugs)
        self.assertEqual(applicable("maven", sugs), [])
        self.assertEqual(applicable("jaguar2", sugs), [])

    def test_maven_command_has_jacoco_skip(self):
        from sbfl.run_maven import run_maven
        from sbfl.toolchain.profiles import ToolEnv
        home = Home(self.tmp / "h").ensure()
        s = load_settings(home)
        work = self.tmp / "w"
        work.mkdir()
        te = mock.MagicMock(spec=ToolEnv)
        te.env = {}
        te.mvn = lambda *a: ["mvn", *a]
        te.describe = lambda: {}
        prep = Prepared(BUG, work, ".", None, [], {})
        with mock.patch("sbfl.run_maven.proc.run", return_value={"rc": 0, "timed_out": False, "seconds": 0.1}):
            r = run_maven(home, s, prep, work, AttemptConfig(), self.tmp / "l.log", toolenv=te)
        self.assertIn("-Djacoco.skip=true", r.command)


class DedupTest(unittest.TestCase):
    def test_default_jdk_is_resolved_before_comparing(self):
        s = mock.MagicMock()
        s.get = lambda sec, key, default=None: default
        first = AttemptConfig()
        te = {"jdk": {"major": 8}, "maven": {"version": "3.6.3"}}
        com = candidates(first, Diagnosis(st.DESCONHECIDO), "maven", s, set(), te)
        self.assertNotIn(8, [c.jdk for c in com])
        self.assertIn(8, [c.jdk for c in candidates(first, Diagnosis(st.DESCONHECIDO), "maven", s, set())])
        diag = Diagnosis(st.DEPENDENCIA_NAO_RESOLVIDA, suggestions=[Suggestion(maven="3.6.3")])
        com = candidates(first, diag, "maven", s, set(), te)
        self.assertNotIn("3.6.3", [c.maven for c in com])


class BestAttemptTest(Tmp):
    def setUp(self):
        super().setUp()
        self.home = Home(self.tmp / "h").ensure()
        self.settings = load_settings(self.home)
        pristine = self.tmp / "pristine"
        pristine.mkdir()
        (pristine / "pom.xml").write_text("<project/>")
        self.prep = Prepared(BUG, pristine, ".", ["a.BTest"], [FaultLine("a.B", 3, "removed")], {})
        self.lines: list[str] = []
        self.cfgs: list[AttemptConfig] = []

    def runner(self, logs, jdks):
        def run(home, settings, prep, workdir, cfg, log, toolenv=None, on_line=None):
            self.cfgs.append(cfg)
            i = len(self.cfgs) - 1
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(logs[i], encoding="utf-8")
            return AttemptResult(1, False, 1.0, log, {}, {}, ["mvn"],
                                 {"jdk": {"major": jdks[i]}, "maven": {"version": "3.6.3"}}, {})
        return run

    def go(self, logs, jdks, **kw):
        with mock.patch.object(ex, "prepare", return_value=self.prep), \
                mock.patch.object(ex, "run_maven", self.runner(logs, jdks)):
            rel = ex.execute(self.home, self.settings, BUG, "maven",
                             ex.RunOptions(log_cb=self.lines.append, **kw))
        return load_run(self.home, rel)

    def test_status_comes_from_attempt_that_went_furthest(self):
        compile_err = ("[INFO] BUILD FAILURE\n[ERROR] COMPILATION ERROR :\n"
                       "[ERROR] Source option 5 is no longer supported\n")
        run = self.go([JAVAAGENT, compile_err, compile_err, compile_err, compile_err, compile_err],
                      [8, 11, 17, 21, 21, 21])
        self.assertEqual(run["status"], "plugin_incompativel")
        self.assertTrue(any(w.startswith("tentativa 2 (JDK ") for w in run["warnings"]), run["warnings"])
        self.assertFalse(any(w.startswith("tentativa 1") for w in run["warnings"]))
        self.assertIn("# jflp tentativa 1: plugin_incompativel", self.lines)
        self.assertIn("# jflp tentativa 2: compilacao_falhou", self.lines)
        self.assertIsNone(self.cfgs[0].jdk)
        self.assertNotIn(8, [c.jdk for c in self.cfgs])      # JDK 8 já era o padrão: sem tentativa repetida
        self.assertEqual(list((self.home.work / "w").iterdir()), [])

    def test_tie_prefers_most_recent(self):
        a = "[ERROR] COMPILATION ERROR :\n"
        b = "[ERROR] animal-sniffer Undefined reference\n"
        run = self.go([a, b, b, b, b, b], [8, 11, 11, 11, 11, 11])
        self.assertEqual(run["status"], "plugin_incompativel")


class Jaguar1LogTest(Tmp):
    def test_log_lines_to_tests(self):
        log = self.tmp / "jaguar.log"
        log.write_text(
            "# sbfl attempt 1\n"
            "23:28:46.116 [main] INFO  JaguarLogger - Test testAbsNegative(demo.CalcTest) : Failed\n"
            "23:28:46.117 [main] INFO  JaguarLogger - Test testAdd(demo.CalcTest) : Passed\n", encoding="utf-8")
        self.assertEqual(parse_jaguar1_log(log),
                         {"demo.CalcTest#testAbsNegative": "failed", "demo.CalcTest#testAdd": "passed"})


class GuiaTest(Tmp):
    def test_find_poms_does_not_descend_into_projects_and_limits(self):
        for n in ("a", "b", "c/d", "a/core"):
            (self.tmp / n).mkdir(parents=True)
            (self.tmp / n / "pom.xml").write_text("<project/>")
        self.assertEqual(find_poms(self.tmp), ["a", "b", "c/d"])
        r = find_poms(self.tmp, limit=2)
        self.assertEqual((list(r), r.mais), (["a", "b"], 1))

class CliTest(Tmp):
    def test_parse_bug_folder_and_url(self):
        s = load_settings(Home(self.tmp / "h"))
        (self.tmp / "proj").mkdir()
        b = cli.parse_bug(s, str(self.tmp / "proj"))
        self.assertEqual((b.benchmark, b.project), ("local", "proj"))
        b = cli.parse_bug(s, "https://x.org/a/repo.git@abcdef1234567890", "core")
        self.assertEqual((b.project, b.extra["module"]), ("repo", "core"))
        with self.assertRaises(ValueError):
            cli.parse_bug(s, "lixo")

    def test_parse_bug_folder_with_module(self):
        s = load_settings(Home(self.tmp / "h"))
        (self.tmp / "col" / "core").mkdir(parents=True)
        (self.tmp / "col" / "core" / "pom.xml").write_text("<project/>")
        b = cli.parse_bug(s, str(self.tmp / "col"), "core")
        self.assertEqual(b.project, "core")


if __name__ == "__main__":
    unittest.main()
