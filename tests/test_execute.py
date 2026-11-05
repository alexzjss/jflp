from __future__ import annotations

import gzip
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sbfl import execute as ex
from sbfl import recipes
from sbfl.adapters.base import Bug
from sbfl.autosearch import attempt_cap, candidates
from sbfl.diagnose import Diagnosis, Suggestion
from sbfl.diffgt import FaultLine
from sbfl.errors import StageError
from sbfl.home import Home, load_settings
from sbfl.prepare import Prepared
from sbfl.run_maven import AttemptConfig, AttemptResult
from sbfl.store import load_run
from sbfl.toolchain.profiles import MissingTool

SNIFFER = ("[INFO] building\n[ERROR] Failed to execute goal org.codehaus.mojo:animal-sniffer-maven-plugin:"
           "1.0:check: Undefined reference: java/lang/X\n[INFO] BUILD FAILURE\n")
BUG = Bug("local", "proj", "7")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = Home(self.tmp / "Pasta Ção").ensure()
        self.settings = load_settings(self.home)
        pristine = self.tmp / "pristine"
        pristine.mkdir()
        (pristine / "pom.xml").write_text("<project/>")
        self.prep = Prepared(BUG, pristine, ".", ["a.BTest"], [FaultLine("a.B", 3, "removed")], {})
        self.cfgs: list[AttemptConfig] = []
        self.lines: list[str] = []

    def fake_runner(self, behave):
        """behave(n, cfg) -> 'ok' | 'sniffer' | 'testfail'."""
        def run(home, settings, prep, workdir, cfg, log, toolenv=None, on_line=None):
            self.cfgs.append(cfg)
            what = behave(len(self.cfgs), cfg)
            if what == "missing":
                raise MissingTool("jdk", str(cfg.jdk), f"JDK {cfg.jdk} não encontrado")
            log.parent.mkdir(parents=True, exist_ok=True)
            text = SNIFFER if what in ("sniffer", "reactor") else "[INFO] BUILD SUCCESS\n"
            if what == "nocsv":
                return AttemptResult(0, False, 1.0, log, {"surefire": workdir}, {}, ["mvn"],
                                     {"jdk": {"major": 8}}, {"pom_plan": {"detected": [], "diff": ""}})
            log.write_text(text, encoding="utf-8")
            if on_line:
                for ln in text.splitlines():
                    on_line(ln)
            out, tests = {}, {}
            extra = {} if what == "reactor" else {"pom_plan": {"detected": ["junit4"], "diff": "-a\n+b"}}
            if what not in ("sniffer", "reactor"):
                csv = workdir / "target" / "jaguar2.csv"
                csv.parent.mkdir(parents=True, exist_ok=True)
                csv.write_text("a/B,3,1,0,0,1,1.0\n")
                pom = workdir / "jaguar2-pom.xml"
                pom.write_text("<project/>")
                out = {"jaguar2_csv": csv, "pom": pom}
                tests = {"a.BTest#t": "passed" if what == "allpass" else "failed"}
            return AttemptResult(0, False, 1.0, log, out, tests, ["mvn", "test"], {"jdk": {"major": 11}}, extra)
        return run

    def go(self, behave, opts=None, kind="jaguar2", patch_prepare=True):
        opts = opts or ex.RunOptions()
        with mock.patch.object(ex, "prepare", return_value=self.prep), \
                mock.patch.object(ex, "run_jaguar2", self.fake_runner(behave)):
            rel = ex.execute(self.home, self.settings, BUG, kind, opts)
        return rel, load_run(self.home, rel)


class ExecuteTest(Base):
    def test_sem_teste_falhando_para_a_busca_e_nao_e_sucesso(self):
        rel, run = self.go(lambda n, c: "allpass")
        self.assertEqual(run["status"], "sem_teste_falhando")
        self.assertEqual(len(self.cfgs), 1)
        self.assertIn("Nenhum teste falhou", run["diagnosis"]["cause"])
        self.assertEqual(run["diagnosis"]["suggestions"], [])
        self.assertEqual(run["failures"], [])
        self.assertFalse(ex.st.is_success(run["status"]))

    def test_grava_falhas_com_provavel_erro_do_surefire(self):
        base = self.fake_runner(lambda n, c: "testfail")

        def with_reports(home, settings, prep, workdir, cfg, log, toolenv=None, on_line=None):
            r = base(home, settings, prep, workdir, cfg, log, toolenv, on_line)
            sf = workdir / "target" / "surefire-reports"
            sf.mkdir(parents=True, exist_ok=True)
            (sf / "TEST-a.BTest.xml").write_text(
                '<testsuite><testcase classname="a.BTest" name="t"><failure message="esperava 1" type="AssertionError">'
                "AssertionError: esperava 1\n\tat org.junit.Assert.fail(Assert.java:1)\n"
                "\tat a.B.soma(B.java:3)\n\tat a.BTest.t(BTest.java:9)\n</failure></testcase></testsuite>",
                encoding="utf-8")
            r.outputs["surefire"] = sf
            return r
        with mock.patch.object(ex, "prepare", return_value=self.prep), \
                mock.patch.object(ex, "run_jaguar2", with_reports):
            rel = ex.execute(self.home, self.settings, BUG, "jaguar2", ex.RunOptions())
        run = load_run(self.home, rel)
        self.assertEqual(run["failures"], [{"test": "a.BTest#t", "message": "esperava 1", "type": "AssertionError",
                                            "frame": {"cls": "a.B", "method": "soma", "file": "B.java", "line": 3}}])

    def test_avisos_do_prepare_vao_para_a_execucao(self):
        self.prep.info["warnings"] = ["não achei a verdade-terreno"]
        _, run = self.go(lambda n, c: "testfail")
        self.assertEqual(run["warnings"][0], "não achei a verdade-terreno")
        self.prep.info["jaguar2_suportado"] = False
        _, run = self.go(lambda n, c: "testfail")
        self.assertIn("não achei a verdade-terreno", run["warnings"])

    def test_execute_success_first_try(self):
        opts = ex.RunOptions(log_cb=self.lines.append)
        rel, run = self.go(lambda n, c: "testfail", opts)
        self.assertEqual(run["status"], "ok_testes_falhando")
        self.assertEqual(run["attempts"], 1)
        self.assertEqual(run["tests"]["failed"], 1)
        self.assertEqual(run["relevant_tests"], ["a.BTest"])
        self.assertEqual(run["truth"], [{"cls": "a.B", "line": 3, "kind": "removed"}])
        self.assertEqual(run["test_scope"], "relevant")
        self.assertIn("[INFO] BUILD SUCCESS", self.lines)
        d = self.home.runs.joinpath(*rel.split("/"))
        for f in ("run.json", "env.json", "command.txt", "attempts/1/attempt.json",
                  "attempts/1/log.txt.gz", "outputs/jaguar2.csv", "pom/jaguar2-pom.xml",
                  "pom/jaguar2-pom.diff"):
            self.assertTrue((d / f).exists(), f)
        self.assertTrue((d / "outputs").is_dir())
        with gzip.open(d / "attempts/1/log.txt.gz", "rt") as f:
            self.assertIn("BUILD SUCCESS", f.read())
        att = json.loads((d / "attempts/1/attempt.json").read_text(encoding="utf-8"))
        self.assertEqual(att["diagnosis"]["status"], "ok_testes_falhando")
        env = json.loads((d / "env.json").read_text(encoding="utf-8"))
        self.assertEqual(env["toolenv"], {"jdk": {"major": 11}})
        self.assertIn("python", env)
        self.assertEqual((d / "command.txt").read_text().strip(), "mvn test")
        self.assertTrue((d / "checkout.zip").exists() or (d / "checkout.tar.zst").exists())
        self.assertEqual(list((self.home.work / "w").iterdir()), [])  # cópia de trabalho apagada

    def test_autosearch_stops_at_first_success(self):
        rel, run = self.go(lambda n, c: "ok" if "skip-animal-sniffer" in c.fixes_on else "sniffer")
        self.assertEqual(run["attempts"], 2)
        self.assertEqual(run["status"], "ok_testes_falhando")
        self.assertEqual(len(run["attempt_configs"]), 2)
        # a correção sugerida mantém as detectadas
        self.assertEqual(self.cfgs[1].fixes_on, frozenset({"junit4", "skip-animal-sniffer"}))
        d = self.home.runs.joinpath(*rel.split("/"))
        self.assertTrue((d / "attempts/2/attempt.json").exists())

    def test_autosearch_disabled_stops(self):
        _, run = self.go(lambda n, c: "sniffer", ex.RunOptions(auto_search=False))
        self.assertEqual(run["attempts"], 1)
        self.assertEqual(run["status"], "plugin_incompativel")
        self.assertEqual(run["diagnosis"]["status"], "plugin_incompativel")

    def test_attempt_cap(self):
        _, run = self.go(lambda n, c: "sniffer")
        self.assertEqual(run["attempts"], self.settings.get("run", "max_attempts"))
        self.assertEqual(len(self.cfgs), 6)
        self.settings.set("run", "max_attempts", 3)
        self.cfgs.clear()
        _, run = self.go(lambda n, c: "sniffer")
        self.assertEqual(run["attempts"], 3)

    def test_test_failures_never_trigger_search(self):
        _, run = self.go(lambda n, c: "testfail")
        self.assertEqual(len(self.cfgs), 1)
        self.assertEqual(run["diagnosis"]["suggestions"], [])

    def test_recipe_applied_and_priority(self):
        for scope, fixes, glob in (("project", {"source-dirs"}, None), ("bug", {"junit4", "compiler-java"}, None)):
            recipes.save(self.home, BUG, "jaguar2", scope, AttemptConfig(frozenset(fixes)), glob, "nota")
        _, run = self.go(lambda n, c: "ok", ex.RunOptions(fixes_on=frozenset({"override-version"})))
        self.assertEqual(self.cfgs[0].fixes_on, frozenset({"junit4", "compiler-java", "override-version"}))
        self.assertEqual(run["recipe"]["match"], "7")

    def test_recipe_priority_levels(self):
        for scope, glob, fx in (("project", None, "a"), ("glob", "[0-9]", "g"), ("bug", None, "b")):
            recipes.save(self.home, BUG, "jaguar2", scope, AttemptConfig(frozenset({fx})), glob)
        self.assertEqual(recipes.find(self.home, BUG, "jaguar2").fixes, ["b"])
        other = Bug("local", "proj", "8")
        self.assertEqual(recipes.find(self.home, other, "jaguar2").fixes, ["g"])
        self.assertEqual(recipes.find(self.home, Bug("local", "proj", "88"), "jaguar2").fixes, ["a"])
        self.assertIsNone(recipes.find(self.home, BUG, "maven"))

    def test_recipe_save_appends(self):
        p1 = recipes.save(self.home, BUG, "jaguar2", "bug",
                          AttemptConfig(frozenset({"junit4"}), params={"override-version": {"plugins": {"a:b": "1"}}},
                                        jdk=8), note="uma")
        before = p1.read_text(encoding="utf-8")
        p2 = recipes.save(self.home, BUG, "jaguar2", "glob", AttemptConfig(frozenset({"x"}), maven="3.6.3"),
                          glob="1*")
        self.assertEqual(p1, p2)
        text = p2.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(before))
        self.assertEqual(text.count("[[recipe]]"), 2)
        r = recipes.find(self.home, BUG, "jaguar2")
        self.assertEqual((r.jdk, r.note, r.params["override-version"]["plugins"]["a:b"]), (8, "uma", "1"))

    def test_recipe_saves_and_applies_fixes_off_and_test_scope(self):
        recipes.save(self.home, BUG, "jaguar2", "bug",
                     AttemptConfig(frozenset({"junit4"}), frozenset({"source-dirs"}), test_scope="all"))
        text = (self.home.recipes / "local" / "proj.toml").read_text(encoding="utf-8")
        self.assertIn('fixes_off = ["source-dirs"]', text)
        self.assertIn('test_scope = "all"', text)
        r = recipes.find(self.home, BUG, "jaguar2")
        self.assertEqual((r.fixes_off, r.test_scope), (["source-dirs"], "all"))
        _, run = self.go(lambda n, c: "ok")
        self.assertEqual(self.cfgs[0].fixes_off, frozenset({"source-dirs"}))
        self.assertEqual(self.cfgs[0].test_scope, "all")
        self.assertEqual(run["test_scope"], "all")
        self.cfgs.clear()
        self.go(lambda n, c: "ok", ex.RunOptions(test_scope="relevant"))   # a opção explícita vence a receita
        self.assertEqual(self.cfgs[0].test_scope, "relevant")

    def test_recipe_default_scope_is_not_written(self):
        recipes.save(self.home, BUG, "jaguar2", "bug", AttemptConfig(frozenset({"junit4"})))
        text = (self.home.recipes / "local" / "proj.toml").read_text(encoding="utf-8")
        self.assertNotIn("test_scope", text)
        self.assertNotIn("fixes_off", text)

    def test_recipe_fixes_off_dropped_when_explicitly_enabled(self):
        recipe = recipes.Recipe("*", "jaguar2", fixes=["junit4"], fixes_off=["source-dirs"])
        cfg = ex._first_config(self.settings, recipe, ex.RunOptions(fixes_on=frozenset({"source-dirs"})))
        self.assertIn("source-dirs", cfg.fixes_on)
        self.assertNotIn("source-dirs", cfg.fixes_off)

    def test_prepare_failure_and_missing_tool(self):
        with mock.patch.object(ex, "prepare", side_effect=StageError("checkout_falhou", "sem rede")):
            rel = ex.execute(self.home, self.settings, BUG, "jaguar2", ex.RunOptions())
        run = load_run(self.home, rel)
        self.assertEqual((run["status"], run["diagnosis"]["cause"]), ("checkout_falhou", "sem rede"))

        def boom(*a, **k):
            raise MissingTool("jdk", "11", "JDK 11 não encontrado")
        with mock.patch.object(ex, "prepare", return_value=self.prep), mock.patch.object(ex, "run_jaguar2", boom):
            rel = ex.execute(self.home, self.settings, BUG, "jaguar2", ex.RunOptions())
        run = load_run(self.home, rel)
        self.assertEqual((run["status"], run["attempts"]), ("jdk_incompativel", 1))
        d = self.home.runs.joinpath(*rel.split("/"))
        for f in ("env.json", "command.txt", "attempts/1/attempt.json"):
            self.assertTrue((d / f).exists(), f)
        self.assertEqual(list((self.home.work / "w").iterdir()), [])

    def test_missing_tool_on_candidate_is_skipped(self):
        self.settings.set("run", "max_attempts", 20)

        def behave(n, c):
            if c.jdk == 8:
                return "missing"
            return "ok" if c.jdk == 17 else "sniffer"
        rel, run = self.go(behave)
        self.assertEqual(run["status"], "ok_testes_falhando")
        self.assertEqual(run["attempts"], len(run["attempt_configs"]))
        d = self.home.runs.joinpath(*rel.split("/"))
        metas = [json.loads(f.read_text(encoding="utf-8")) for f in sorted(d.glob("attempts/*/attempt.json"))]
        skipped = [s for s in metas if s.get("skipped")]
        self.assertEqual(len(skipped), 1)
        self.assertIn("JDK 8", skipped[0]["skipped"])
        self.assertTrue((d / "outputs" / "jaguar2.csv").exists())
        self.assertEqual(list((self.home.work / "w").iterdir()), [])

    def test_skipped_candidate_is_reported_in_warnings(self):
        self.settings.set("run", "max_attempts", 20)

        def behave(n, c):
            if c.jdk == 8:
                return "missing"
            return "ok" if c.jdk == 17 else "sniffer"
        rel, run = self.go(behave)
        self.assertTrue(any(w.startswith("tentativa ") and "ignorada: JDK 8 não encontrado" in w
                            for w in run["warnings"]), run["warnings"])

    def test_jdk_that_reached_tests_is_carried_to_later_candidates(self):
        self.settings.set("run", "max_attempts", 20)

        def behave(n, c):
            if n == 1:
                return "sniffer"
            return "nocsv" if c.jdk == 8 else "sniffer"
        self.go(behave)
        i = next(k for k, c in enumerate(self.cfgs) if c.jdk == 8)
        self.assertTrue(all(c.jdk is None for c in self.cfgs[:i]))
        later = [c for c in self.cfgs[i + 1:] if c.jdk != 17]
        self.assertTrue(later)
        self.assertTrue(all(c.jdk == 8 for c in later), [c.jdk for c in later])

    def test_missing_tool_on_every_candidate_keeps_last_real(self):
        self.settings.set("run", "max_attempts", 20)
        rel, run = self.go(lambda n, c: "sniffer" if n == 1 else "missing")
        self.assertEqual(run["status"], "plugin_incompativel")
        d = self.home.runs.joinpath(*rel.split("/"))
        self.assertTrue((d / "env.json").exists())

    def test_detected_fixes_when_first_attempt_fails_before_pom(self):
        calls = []

        def gen(mod, settings, jdk, *a, **k):
            calls.append(jdk)
            return mock.Mock(detected=["junit4"])
        with mock.patch.object(ex, "pom_generate", gen):
            self.go(lambda n, c: "reactor" if n == 1 else ("ok" if "skip-animal-sniffer" in c.fixes_on else "sniffer"))
        self.assertEqual(calls, [11])
        self.assertEqual(self.cfgs[1].fixes_on, frozenset({"junit4", "skip-animal-sniffer"}))

    def test_fixes_add_keeps_detected_fixes(self):
        with mock.patch.object(ex, "pom_generate", lambda *a, **k: mock.Mock(detected=["junit4"])):
            self.go(lambda n, c: "ok", ex.RunOptions(fixes_add=frozenset({"skip-rat"})))
        self.assertEqual(self.cfgs[0].fixes_on, frozenset({"junit4", "skip-rat"}))

    def test_fixes_add_respects_fixes_off(self):
        with mock.patch.object(ex, "pom_generate", lambda *a, **k: mock.Mock(detected=["junit4", "x"])):
            self.go(lambda n, c: "ok", ex.RunOptions(fixes_add=frozenset({"skip-rat"}),
                                                     fixes_off=frozenset({"x"})))
        self.assertEqual(self.cfgs[0].fixes_on, frozenset({"junit4", "skip-rat"}))

    def test_fixes_add_on_maven_kind_needs_no_detection(self):
        with mock.patch.object(ex, "pom_generate", side_effect=AssertionError("não deve detectar")),                 mock.patch.object(ex, "prepare", return_value=self.prep),                 mock.patch.object(ex, "run_maven", self.fake_runner(lambda n, c: "ok")):
            ex.execute(self.home, self.settings, BUG, "maven", ex.RunOptions(fixes_add=frozenset({"unblock-http"})))
        self.assertEqual(self.cfgs[0].fixes_on, frozenset({"unblock-http"}))

    def test_jaguar2_without_pom_short_circuits(self):
        (self.prep.pristine / "pom.xml").unlink()
        rel, run = self.go(lambda n, c: "ok")
        self.assertEqual(self.cfgs, [])
        self.assertEqual(run["status"], "projeto_invalido")
        self.assertEqual(run["diagnosis"]["cause"], "projeto sem pom.xml: Jaguar 2 não suportado (use Jaguar 1)")
        self.assertEqual(run["attempts"], 0)

    def test_jaguar2_flag_unsupported_short_circuits(self):
        self.prep.info["jaguar2_suportado"] = False
        rel, run = self.go(lambda n, c: "ok")
        self.assertEqual(self.cfgs, [])
        self.assertEqual(run["status"], "projeto_invalido")

    def test_maven_kind_without_pom_still_runs(self):
        (self.prep.pristine / "pom.xml").unlink()
        with mock.patch.object(ex, "prepare", return_value=self.prep),                 mock.patch.object(ex, "run_maven", self.fake_runner(lambda n, c: "ok")):
            ex.execute(self.home, self.settings, BUG, "maven", ex.RunOptions())
        self.assertEqual(len(self.cfgs), 1)

    def test_command_txt_lists_every_phase_with_env(self):
        base = self.fake_runner(lambda n, c: "ok")

        def with_phases(*a, **k):
            r = base(*a, **k)
            r.phases = [{"name": "reactor", "command": ["mvn", "install", "-pl", "a b"], "env": {"JAVA_HOME": "/jdk 11"}},
                        {"name": "jaguar2", "command": ["mvn", "test"], "env": {"JAVA_TOOL_OPTIONS": "-Dx=1"}}]
            return r
        with mock.patch.object(ex, "prepare", return_value=self.prep),                 mock.patch.object(ex, "run_jaguar2", with_phases):
            rel = ex.execute(self.home, self.settings, BUG, "jaguar2", ex.RunOptions())
        from sbfl.store import run_dir
        text = (run_dir(self.home, rel) / "command.txt").read_text(encoding="utf-8")
        self.assertIn("# fase: reactor", text)
        self.assertIn("# fase: jaguar2", text)
        self.assertIn("JAVA_HOME", text)
        self.assertIn("JAVA_TOOL_OPTIONS", text)
        self.assertIn("install", text)

    def test_recipe_params_deep_merge_with_opts(self):
        recipes.save(self.home, BUG, "jaguar2", "bug",
                     AttemptConfig(params={"override-version": {"plugins": {"a:b": "1"}}}))
        self.go(lambda n, c: "ok", ex.RunOptions(params={"override-version": {"plugins": {"c:d": "2"}}}))
        self.assertEqual(self.cfgs[0].params["override-version"]["plugins"], {"a:b": "1", "c:d": "2"})

    def test_unexpected_error_is_desconhecido(self):
        with mock.patch.object(ex, "prepare", return_value=self.prep), \
                mock.patch.object(ex, "run_jaguar2", side_effect=RuntimeError("x")):
            with self.assertRaises(RuntimeError):
                ex.execute(self.home, self.settings, BUG, "jaguar2", ex.RunOptions())
        from sbfl.store import list_runs
        runs = list_runs(self.home)
        self.assertEqual([r["status"] for r in runs], ["desconhecido"])
        self.assertEqual(list((self.home.work / "w").iterdir()), [])


class ProjetoInvalidoTest(Base):
    def test_projeto_sem_pom_nao_tenta_de_novo(self):
        log_text = ("[ERROR] The goal you specified requires a project to execute but there is no POM in "
                    "this directory (C:\\w). MissingProjectException\n")
        calls = []

        def runner(home, settings, prep, workdir, cfg, log, toolenv=None, on_line=None):
            calls.append(cfg)
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(log_text, encoding="utf-8")
            return AttemptResult(1, False, 1.0, log, {}, {}, ["mvn"], {}, {})

        with mock.patch.object(ex, "prepare", return_value=self.prep), \
                mock.patch.object(ex, "run_maven", runner):
            rel = ex.execute(self.home, self.settings, BUG, "maven", ex.RunOptions())
        run = load_run(self.home, rel)
        self.assertEqual(len(calls), 1)
        self.assertEqual(run["status"], "projeto_invalido")
        self.assertEqual(run["attempts"], 1)

    def test_jaguar1_sem_pom_e_projeto_invalido(self):
        (self.prep.pristine / "pom.xml").unlink()
        log_text = ("[ERROR] The goal you specified requires a project to execute but there is no POM in "
                    "this directory (C:\\w). MissingProjectException\n")

        def runner(home, settings, prep, workdir, cfg, log, toolenv=None, on_line=None):
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(log_text, encoding="utf-8")
            return AttemptResult(1, False, 1.0, log, {}, {}, ["mvn"], {}, {})

        with mock.patch.object(ex, "prepare", return_value=self.prep),                 mock.patch.object(ex, "run_jaguar1", runner):
            rel = ex.execute(self.home, self.settings, BUG, "jaguar1", ex.RunOptions())
        self.assertEqual(load_run(self.home, rel)["status"], "projeto_invalido")


class ProcOnLineTest(Base):
    def test_failing_callback_does_not_abort(self):
        import sys
        from sbfl import proc

        def bad(line):
            raise RuntimeError("cb")
        r = proc.run([sys.executable, "-c", "print('a'); print('b')"], log_path=self.tmp / "l.log", on_line=bad)
        self.assertEqual(r["rc"], 0)
        self.assertEqual((self.tmp / "l.log").read_text().split(), ["a", "b"])

    def test_log_phase_reaches_on_line(self):
        from sbfl.run_maven import log_phase
        got = []
        log_phase(self.tmp / "p.log", "pom", got.append)
        self.assertEqual(got, ["# jflp fase: pom"])

    def test_on_line_only_for_logged_lines(self):
        import sys
        from sbfl import proc
        got = []
        proc.run([sys.executable, "-c", "print('a'); print('ruido'); print('b')"], drop="ruido",
                 log_path=self.tmp / "l.log", on_line=got.append)
        self.assertEqual(got, ["a", "b"])


class FirstConfigTest(Base):
    def test_fixes_off_overrides_recipe_fixes(self):
        recipe = recipes.Recipe("*", "jaguar2", fixes=["source-dirs", "junit4"])
        cfg = ex._first_config(self.settings, recipe,
                               ex.RunOptions(fixes_on=frozenset({"compiler-java"}),
                                             fixes_off=frozenset({"source-dirs"})))
        self.assertEqual(cfg.fixes_on, frozenset({"junit4", "compiler-java"}))
        self.assertEqual(cfg.fixes_off, frozenset({"source-dirs"}))


class CandidatesTest(Base):
    def test_order_dedup_and_exclusions(self):
        diag = Diagnosis("plugin_incompativel", suggestions=[
            Suggestion(fix="exclude-duplicates", label="x"),
            Suggestion(fix="skip-animal-sniffer", label="y"),
            Suggestion(jdk=11, label="same as ... jdk")])
        self.settings.set("run", "max_attempts", 20)
        out = candidates(AttemptConfig(), diag, "jaguar2", self.settings, {"junit4"})
        self.assertEqual(out[0].fixes_on, frozenset({"junit4", "skip-animal-sniffer"}))
        self.assertNotIn(11, [c.jdk for c in out])   # 11 é o padrão do perfil jaguar2: duplicata descartada
        jdks = [i for i, c in enumerate(out) if c.jdk]
        sure = [i for i, c in enumerate(out) if "override-version" in c.fixes_on]
        comp = [i for i, c in enumerate(out) if "compiler-java" in c.fixes_on]
        self.assertLess(max(jdks), min(sure))
        self.assertLess(max(sure), min(comp))
        keys = [c.key() for c in out]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertNotIn(AttemptConfig().key(), keys)
        self.assertFalse(any("exclude-duplicates" in c.fixes_on for c in out))
        self.assertEqual([c.jdk for c in out if c.jdk], [8, 17])
        self.assertEqual(len(candidates(AttemptConfig(), diag, "jaguar2", load_settings(self.home),
                                        set())), 5)

    def test_jaguar2_never_gets_a_maven_override_candidate(self):
        diag = Diagnosis("dependencia_nao_resolvida", suggestions=[Suggestion(maven="3.6.3", label="m"),
                                                                    Suggestion(fix="unblock-http", label="u")])
        out = candidates(AttemptConfig(), diag, "jaguar2", self.settings, set())
        self.assertFalse(any(c.maven for c in out))
        self.assertIn("unblock-http", out[0].fixes_on)

    def test_maven_kind_has_no_pom_fixes(self):
        out = candidates(AttemptConfig(), Diagnosis("x", suggestions=[Suggestion(fix="junit4")]),
                         "maven", self.settings)
        self.assertTrue(all(not c.fixes_on for c in out))
        self.assertEqual([c.jdk for c in out], [8, 17, 11])
        j1 = candidates(AttemptConfig(), Diagnosis("x"), "jaguar1", self.settings)
        self.assertEqual([c.jdk for c in j1], [17, 11])


class LegadoCandidatesTest(Base):
    def _tools(self, jdk7=True, mvn305=True):
        from sbfl.toolchain.detect import Jdk, Maven
        jdks = [Jdk(Path("j7"), 7, "1.7.0_352", "managed")] if jdk7 else []
        mvns = [Maven(Path("m"), (3, 0, 5), "managed")] if mvn305 else []
        return mock.patch.multiple("sbfl.autosearch", find_jdks=lambda *a: jdks, find_mavens=lambda *a: mvns)

    def _cands(self, kind="maven", **tools):
        self.settings.set("run", "max_attempts", 20)
        self.settings.set("cobertura", "legado", True)
        self.settings.set("cobertura", "legado_offline", False)
        with self._tools(**tools):
            return candidates(AttemptConfig(params={"cobertura": True}), Diagnosis("x"), kind, self.settings)

    def test_candidates_incluem_legado_quando_ferramentas_existem(self):
        out = self._cands()
        self.assertEqual((out[-1].jdk, out[-1].maven), (7, "3.0.5"))
        self.assertFalse(out[-1].params.get("offline"))
        self.assertEqual(len([c for c in out if c.jdk == 7]), 1)
        self.settings.set("cobertura", "legado_offline", True)
        with self._tools():
            out = candidates(AttemptConfig(params={"cobertura": True}), Diagnosis("x"), "maven", self.settings)
        self.assertEqual([(c.jdk, c.maven, c.params.get("offline")) for c in out[-2:]],
                         [(7, "3.0.5", None), (7, "3.0.5", True)])
        self.assertTrue(out[-1].params["cobertura"])

    def test_candidates_sem_legado_sem_jdk7(self):
        self.assertFalse(any(c.jdk == 7 for c in self._cands(jdk7=False)))
        self.assertFalse(any(c.maven == "3.0.5" for c in self._cands(mvn305=False)))
        self.assertFalse(any(c.jdk == 7 for c in self._cands(kind="jaguar1")))

    def test_legado_so_na_cobertura(self):
        self.settings.set("run", "max_attempts", 20)
        with self._tools():
            out = candidates(AttemptConfig(), Diagnosis("x"), "maven", self.settings)
        self.assertFalse(any(c.jdk == 7 for c in out))

    def test_legado_respeita_max_attempts_e_params(self):
        self.settings.set("cobertura", "legado", True)
        self.settings.set("run", "max_attempts", 4)
        with self._tools():
            few = candidates(AttemptConfig(), Diagnosis("x"), "maven", self.settings)
            more = candidates(AttemptConfig(params={"cobertura": True, "max_attempts": 8}), Diagnosis("x"), "maven", self.settings)
        self.assertEqual(len(few), 3)
        self.assertEqual(more[-1].jdk, 7)


class TentativasCoberturaTest(Base):
    """Jobs de cobertura: os Jaguar usam cobertura.max_tentativas_jaguar; o Maven segue com o do lote."""

    def cob(self):
        return ex.RunOptions(params={"cobertura": True, "max_attempts": 8})

    def test_jaguar2_cobertura_para_em_4_mesmo_com_lote_pedindo_8(self):
        _, run = self.go(lambda n, c: "sniffer", self.cob())
        self.assertEqual(run["attempts"], 4)
        self.assertEqual(len(self.cfgs), 4)

    def test_setting_muda_o_limite_na_hora(self):
        self.settings.set("cobertura", "max_tentativas_jaguar", 2)
        _, run = self.go(lambda n, c: "sniffer", self.cob())
        self.assertEqual(run["attempts"], 2)

    def test_setting_e_limitado_de_1_a_8(self):
        self.settings.set("cobertura", "max_tentativas_jaguar", 50)
        self.assertEqual(attempt_cap(self.settings, {"cobertura": True}, "jaguar2"), 8)
        self.settings.set("cobertura", "max_tentativas_jaguar", 0)
        self.assertEqual(attempt_cap(self.settings, {"cobertura": True}, "jaguar1"), 1)

    def test_maven_cobertura_continua_com_8(self):
        params = {"cobertura": True, "max_attempts": 8}
        self.assertEqual(attempt_cap(self.settings, params, "maven"), 8)
        self.settings.set("cobertura", "max_tentativas_jaguar", 2)
        self.assertEqual(attempt_cap(self.settings, params, "maven"), 8)

    def test_maven_cobertura_roda_ate_8_tentativas(self):
        self.settings.set("cobertura", "max_tentativas_jaguar", 2)
        self.settings.set("cobertura", "legado", False)
        with mock.patch.object(ex, "run_maven", self.fake_runner(lambda n, c: "sniffer")), \
                mock.patch.object(ex, "prepare", return_value=self.prep), \
                mock.patch("sbfl.autosearch.JDKS", (8, 11, 17, 21, 6, 5, 4, 3, 2, 1)):
            ex.execute(self.home, self.settings, BUG, "maven", self.cob())
        self.assertGreater(len(self.cfgs), 2)
        self.assertLessEqual(len(self.cfgs), 8)

    def test_fora_da_cobertura_vale_run_max_attempts(self):
        self.settings.set("cobertura", "max_tentativas_jaguar", 2)
        _, run = self.go(lambda n, c: "sniffer")
        self.assertEqual(run["attempts"], 6)
        self.assertEqual(attempt_cap(self.settings, {}, "jaguar2"), 6)

    def test_candidates_nao_monta_lista_maior_que_o_limite(self):
        first = AttemptConfig(params={"cobertura": True, "max_attempts": 8})
        self.assertEqual(len(candidates(first, Diagnosis("x"), "jaguar2", self.settings)), 3)
        self.settings.set("cobertura", "max_tentativas_jaguar", 2)
        self.assertEqual(len(candidates(first, Diagnosis("x"), "jaguar2", self.settings)), 1)


class ReuseAndPruneTest(Base):
    """Cópia de trabalho reaproveitada entre tentativas e poda das tentativas que não mudariam o resultado."""

    def runner_with(self, steps, seen=None):
        """steps(n, cfg, workdir) -> AttemptResult; `seen` recebe (workdir, havia target/ ao começar)."""
        def run(home, settings, prep, workdir, cfg, log, toolenv=None, on_line=None):
            self.cfgs.append(cfg)
            if seen is not None:
                seen.append((workdir, (workdir / "target").exists()))
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(SNIFFER, encoding="utf-8")
            return steps(len(self.cfgs), cfg, workdir)
        return run

    def go_runner(self, runner, opts, lines=None):
        copies = []
        real = ex.working_copy

        def counting(prep, home):
            copies.append(1)
            return real(prep, home)
        opts.log_cb = lines.append if lines is not None else None
        with mock.patch.object(ex, "prepare", return_value=self.prep), \
                mock.patch.object(ex, "working_copy", counting), \
                mock.patch.object(ex, "run_jaguar2", runner):
            rel = ex.execute(self.home, self.settings, BUG, "jaguar2", opts)
        return load_run(self.home, rel), self.home.runs.joinpath(*rel.split("/")), len(copies)

    def steps_best_then_worse(self, n, cfg, workdir):
        env = {"jdk": {"major": 11}}
        if n == 2:      # chegou aos testes: a melhor, mas não é sucesso
            rep = workdir / "target" / "surefire-reports"
            rep.mkdir(parents=True)
            (rep / "TEST-a.xml").write_text("<testsuite/>")
            pom = workdir / "jaguar2-pom.xml"
            pom.write_text("<project/>")
            return AttemptResult(1, False, 1.0, workdir / "x", {"surefire": rep, "pom": pom}, {}, ["mvn"], env, {})
        return AttemptResult(1, False, 1.0, workdir / "x", {}, {}, ["mvn"], env, {})

    def test_uma_copia_so_e_saidas_da_melhor_tentativa(self):
        self.settings.set("run", "max_attempts", 4)
        seen = []
        run, d, copies = self.go_runner(self.runner_with(self.steps_best_then_worse, seen),
                                        ex.RunOptions(archive_checkout=False))
        self.assertEqual(copies, 1)
        self.assertGreaterEqual(len(self.cfgs), 3)
        self.assertEqual(len({w for w, _ in seen}), 1)
        self.assertTrue(all(not t for _, t in seen), seen)       # target/ sumiu entre as tentativas
        self.assertEqual(run["attempts"], len(self.cfgs))
        self.assertTrue((d / "outputs" / "surefire-reports" / "TEST-a.xml").exists())
        self.assertTrue((d / "pom" / "jaguar2-pom.xml").exists())
        self.assertEqual(list((self.home.work / "w").iterdir()), [])
        stage = self.home.work / "s"
        self.assertEqual(list(stage.iterdir()) if stage.exists() else [], [])

    def test_empate_vai_para_a_mais_recente(self):
        self.settings.set("run", "max_attempts", 3)

        def steps(n, cfg, workdir):
            rep = workdir / "target" / "surefire-reports"
            rep.mkdir(parents=True)
            (rep / f"TEST-{n}.xml").write_text("<testsuite/>")
            return AttemptResult(1, False, 1.0, workdir / "x", {"surefire": rep}, {}, ["mvn"],
                                 {"jdk": {"major": 11}}, {})
        run, d, copies = self.go_runner(self.runner_with(steps), ex.RunOptions(archive_checkout=False))
        self.assertEqual(copies, 1)
        self.assertEqual([f.name for f in (d / "outputs" / "surefire-reports").iterdir()], ["TEST-3.xml"])

    def test_com_arquivamento_mantem_uma_copia_por_tentativa(self):
        self.settings.set("run", "max_attempts", 3)
        run, d, copies = self.go_runner(self.runner_with(self.steps_best_then_worse),
                                        ex.RunOptions(archive_checkout=True))
        self.assertEqual(copies, len(self.cfgs))
        self.assertGreaterEqual(copies, 3)
        self.assertTrue((d / "outputs" / "surefire-reports" / "TEST-a.xml").exists())
        self.assertIn("checkout", run)
        self.assertEqual(list((self.home.work / "w").iterdir()), [])

    def test_falha_no_reactor_pula_candidatas_so_de_pom(self):
        self.settings.set("run", "max_attempts", 3)

        def steps(n, cfg, workdir):
            return AttemptResult(1, False, 1.0, workdir / "x", {}, {}, ["mvn"], {"jdk": {"major": 11}},
                                 {"fase": "reactor"})
        lines = []
        diag = Diagnosis("plugin_incompativel", suggestions=[Suggestion(fix="skip-animal-sniffer", label="a"),
                                                             Suggestion(fix="unblock-http", label="b")])
        with mock.patch.object(ex, "classify", return_value=diag):
            run, d, copies = self.go_runner(self.runner_with(steps), ex.RunOptions(archive_checkout=False), lines)
        # skip-animal-sniffer é só POM (pulada); unblock-http muda o settings do reactor (roda)
        self.assertTrue(any("unblock-http" in c.fixes_on for c in self.cfgs[1:]))
        self.assertFalse(any("skip-animal-sniffer" in c.fixes_on for c in self.cfgs[1:]))
        self.assertEqual(len(self.cfgs), 3)          # as puladas não gastam o limite
        self.assertTrue(any(ln.startswith("# jflp tentativa pulada: ") for ln in lines), lines)
        self.assertEqual(run["attempts"], 3)
        self.assertEqual(len(list(d.glob("attempts/*/attempt.json"))), 3)

    def test_falha_antes_dos_testes_pula_so_versao_do_surefire(self):
        self.settings.set("run", "max_attempts", 20)

        def steps(n, cfg, workdir):
            return AttemptResult(1, False, 1.0, workdir / "x", {}, {}, ["mvn"], {"jdk": {"major": 11}}, {})
        self.go_runner(self.runner_with(steps), ex.RunOptions(archive_checkout=False))
        from sbfl.autosearch import SUREFIRE
        for c in self.cfgs:
            self.assertNotIn(SUREFIRE, ((c.params.get("override-version") or {}).get("plugins") or {}))
        self.assertTrue(any("compiler-java" in c.fixes_on for c in self.cfgs))

    def test_reactor_key_e_variante_so_de_surefire(self):
        from sbfl.autosearch import SUREFIRE, reactor_key, surefire_only_variant
        a = AttemptConfig()
        self.assertEqual(reactor_key(a, 11, "3.9"), reactor_key(AttemptConfig(jdk=11), 8, "3.9"))
        self.assertNotEqual(reactor_key(a, 11, "3.9"), reactor_key(a, 8, "3.9"))
        self.assertNotEqual(reactor_key(a, 11, "3.9"),
                            reactor_key(AttemptConfig(params={"reactor_flags": ["-Drat.skip"]}), 11, "3.9"))
        self.assertNotEqual(reactor_key(a, 11, "3.9"),
                            reactor_key(AttemptConfig(fixes_on=frozenset({"unblock-http"})), 11, "3.9"))
        self.assertEqual(reactor_key(a, 11, "3.9"),
                         reactor_key(AttemptConfig(fixes_on=frozenset({"junit4"})), 11, "3.9"))
        # correções que alcançam o reactor: flags próprias (FIX_FLAGS) e troca de javac/JDK do reactor
        for fix in ("rat-flags", "exec-skip", "javac7", "scr-jdk7"):
            self.assertNotEqual(reactor_key(a, 8, "3.9"),
                                reactor_key(AttemptConfig(fixes_on=frozenset({fix})), 8, "3.9"), fix)
        sf = AttemptConfig(fixes_on=frozenset({"override-version"}),
                           params={"override-version": {"plugins": {SUREFIRE: "3.0.0"}}})
        env = (11, "3.9")
        self.assertTrue(surefire_only_variant(sf, a, set(), env, env))
        sf2 = AttemptConfig(fixes_on=frozenset({"junit4", "override-version"}), params=sf.params)
        self.assertTrue(surefire_only_variant(sf2, AttemptConfig(fixes_on=frozenset({"junit4"})), set(), env, env))
        self.assertTrue(surefire_only_variant(sf2, a, {"junit4"}, env, env))      # a = "as detectadas"
        self.assertFalse(surefire_only_variant(sf, AttemptConfig(jdk=8), set(), env, env))
        other = AttemptConfig(fixes_on=frozenset({"override-version"}),
                              params={"override-version": {"plugins": {"x:y": "1"}}})
        self.assertFalse(surefire_only_variant(other, a, set(), env, env))


if __name__ == "__main__":
    unittest.main()
