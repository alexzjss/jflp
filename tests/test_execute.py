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
from sbfl.autosearch import candidates
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
        self.assertEqual(run["status"], "desconhecido")
        self.assertEqual(run["diagnosis"]["cause"], "projeto sem pom.xml: Jaguar 2 não suportado (use Jaguar 1)")
        self.assertEqual(run["attempts"], 0)

    def test_jaguar2_flag_unsupported_short_circuits(self):
        self.prep.info["jaguar2_suportado"] = False
        rel, run = self.go(lambda n, c: "ok")
        self.assertEqual(self.cfgs, [])
        self.assertEqual(run["status"], "desconhecido")

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


if __name__ == "__main__":
    unittest.main()
