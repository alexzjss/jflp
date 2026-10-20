import json
import tempfile
import unittest
from pathlib import Path

from sbfl.diagnose import RULES, classify
from sbfl.run_maven import AttemptResult


def res(tmp, log="", outputs=None, tests=None, timed_out=False, rc=1, extra=None):
    lp = Path(tmp) / "log.txt"
    if log is not None:
        lp.write_text(log, encoding="utf-8")
    return AttemptResult(rc, timed_out, 1.0, lp, outputs or {}, tests or {}, extra=extra or {})


class DiagnoseTest(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = self._t.name
        self.addCleanup(self._t.cleanup)

    def c(self, log, kind="maven", **kw):
        return classify(kind, res(self.tmp, log, **kw))

    def test_rules(self):
        cases = [
            ("Unsupported class file major version 61", "jdk_incompativel"),
            ("error: invalid target release: 17", "jdk_incompativel"),
            ("[ERROR] animal-sniffer failed", "plugin_incompativel"),
            ("[ERROR] Failed to execute goal maven-surefire-plugin:2.12.4:test", "plugin_incompativel"),
            ("[ERROR] The forked VM terminated without saying properly goodbye", "plugin_incompativel"),
            ("[ERROR] maven-compiler-plugin:3.1:compile Fatal error compiling", "plugin_incompativel"),
            ("Could not resolve dependencies for project", "dependencia_nao_resolvida"),
            ("Blocked mirror for repositories: [x (http://x)]", "dependencia_nao_resolvida"),
            ("COMPILATION ERROR :", "compilacao_falhou"),
            ("Tests run: 0, Failures: 0", "testes_nao_encontrados"),
            ("No tests were executed!", "testes_nao_encontrados"),
        ]
        for log, status in cases:
            self.assertEqual(self.c(log).status, status, log)
        self.assertGreaterEqual(len(RULES), 8)

    def test_suggestion_ids(self):
        d = self.c("[ERROR] Failed to execute goal maven-surefire-plugin:2.12.4:test")
        self.assertEqual(d.suggestions[0].fix, "override-version")
        self.assertEqual(d.suggestions[0].params,
                         {"plugins": {"org.apache.maven.plugins:maven-surefire-plugin": "3.2.5"}})
        d = self.c("[ERROR] maven-compiler-plugin:3.1 Fatal error compiling")
        self.assertEqual((d.suggestions[0].fix, d.suggestions[0].params), ("compiler-java", {"version": "3.1"}))
        d = self.c("Blocked mirror for repositories")
        self.assertEqual((d.suggestions[0].fix, d.suggestions[0].maven), (None, "3.6.3"))
        d = self.c("No tests were executed")
        self.assertEqual(d.suggestions[-1].params, {"test_scope": "all"})
        self.assertIsNone(d.suggestions[-1].fix)

    def test_duplicates(self):
        log = ("[ERROR] /w/src/X.java:[3,8] duplicate class: a.b.X\n"
               "error: duplicate class: a.b.C\n")
        d = self.c(log)
        self.assertEqual(d.status, "classes_duplicadas")
        self.assertEqual(d.suggestions[0].fix, "exclude-duplicates")
        files = d.suggestions[0].params["files"]
        self.assertIn("a/b/C.java", files)
        self.assertIn("X.java", files)  # /w/src/X.java vira relativo à raiz src/

    def test_evidence_limits(self):
        log = "\n".join(["x"] * 20 + ["COMPILATION ERROR " + "y" * 500] + ["z"] * 20)
        d = self.c(log)
        self.assertLessEqual(len(d.evidence), 8)
        self.assertTrue(all(len(e) <= 300 for e in d.evidence))
        self.assertTrue(any("COMPILATION ERROR" in e for e in d.evidence))

    def test_test_failures_are_success(self):
        sf = Path(self.tmp) / "surefire"
        sf.mkdir()
        (sf / "r.xml").write_text("<x/>")
        d = self.c("", outputs={"surefire": sf}, tests={"A#a": "failed", "A#b": "passed"})
        self.assertEqual(d.status, "ok_testes_falhando")
        self.assertEqual(d.suggestions, [])

    def test_empty_jaguar2_csv_no_failures_is_ok_with_warning(self):
        csv = Path(self.tmp) / "j.csv"
        csv.write_text("")
        d = self.c("", kind="jaguar2", outputs={"jaguar2_csv": csv}, tests={"A#a": "passed"})
        self.assertEqual(d.status, "ok")
        self.assertIn("nenhum teste falhou: relatório vazio (defeito não reproduzido)", d.warnings)

    def test_jaguar2_unreadable_csv(self):
        csv = Path(self.tmp) / "j.csv"
        csv.write_text("a/B,1,1,0,0,0,0,5\n")
        d = self.c("", kind="jaguar2", outputs={"jaguar2_csv": csv})
        self.assertEqual(d.status, "jaguar_sem_saida")
        self.assertEqual(d.cause, "relatório do Jaguar 2 ilegível")
        self.assertTrue(d.evidence)

    def test_jaguar2_no_csv(self):
        d = self.c("BUILD SUCCESS", kind="jaguar2", rc=0)
        self.assertEqual(d.status, "jaguar_sem_saida")

    def test_timeout_wins(self):
        self.assertEqual(self.c("COMPILATION ERROR", timed_out=True).status, "timeout")

    def test_phases(self):
        d = self.c("x", kind="jaguar1", extra={"fase": "jaguar1_lib", "erro": "sem lib"})
        self.assertEqual((d.status, d.cause), ("jaguar_sem_saida", "sem lib"))
        d = self.c("nada", kind="jaguar2", extra={"fase": "pom", "erro": "pom ruim"})
        self.assertEqual((d.status, d.cause), ("desconhecido", "pom ruim"))
        d = self.c("[ERROR] animal-sniffer", kind="jaguar2", extra={"fase": "pom", "erro": "pom ruim"})
        self.assertEqual(d.status, "plugin_incompativel")
        d = self.c("x", kind="jaguar1", extra={"meta_status": "jaguar_timeout"})
        self.assertEqual(d.status, "timeout")
        d = self.c("x", kind="jaguar1", extra={"meta_status": "no_tests"})
        self.assertEqual(d.status, "testes_nao_encontrados")
        d = self.c("x", kind="jaguar1", extra={"meta_status": "jaguar_crash"})
        self.assertEqual(d.status, "jaguar_crash")

    def test_missing_log_and_to_dict(self):
        r = res(self.tmp, log=None)
        r.log = Path(self.tmp) / "nao-existe.log"
        d = classify("maven", r)
        self.assertEqual(d.status, "desconhecido")
        json.dumps(d.to_dict())

    def test_surefire_header_does_not_hide_no_tests(self):
        log = ("[INFO] --- maven-surefire-plugin:2.12.4:test (default-test) @ proj ---\n"
               "[INFO] No tests to run.\n[INFO] BUILD SUCCESS\n")
        self.assertEqual(self.c(log).status, "testes_nao_encontrados")

    def test_passed_animal_sniffer_does_not_hide_compile_error(self):
        log = ("[INFO] --- animal-sniffer-maven-plugin:1.0:check (x) @ proj ---\n"
               "[INFO] Checking unresolved references\n"
               "[INFO] --- maven-compiler-plugin:3.8.1:testCompile ---\n"
               "[ERROR] COMPILATION ERROR :\n[ERROR] /w/T.java:[3,1] cannot find symbol\n"
               "[INFO] BUILD FAILURE\n")
        self.assertEqual(self.c(log).status, "compilacao_falhou")

    def test_jaguar2_no_csv_wins_over_headers(self):
        log = ("[INFO] --- maven-surefire-plugin:2.12.4:test ---\n[INFO] BUILD SUCCESS\n")
        d = self.c(log, kind="jaguar2", rc=0)
        self.assertEqual(d.status, "jaguar_sem_saida")

    def test_jaguar2_no_csv_error_line_rule_wins(self):
        log = "[ERROR] Failed to execute goal maven-surefire-plugin:2.12.4:test: The forked VM terminated\n"
        d = self.c(log, kind="jaguar2", rc=0)
        self.assertEqual(d.status, "plugin_incompativel")

    def test_error_block_nearest_wins(self):
        log = ("[INFO] No tests to run.\n[ERROR] COMPILATION ERROR :\n[ERROR] x\n"
               "[ERROR] Failed to execute goal maven-surefire-plugin:2.12.4:test\n")
        self.assertEqual(self.c(log).status, "compilacao_falhou")

    def test_duplicate_paths_relative(self):
        log = ("[ERROR] /x/src/main/java/a/b/X.java:[3,8] duplicate class: a.b.X\n"
               "[ERROR] /y/src/java/a/b/X.java:[3,8] duplicate class: a.b.X\n")
        d = self.c(log)
        self.assertEqual(d.suggestions[0].params["files"], ["a/b/X.java"])

    def test_jaguar2_csv_oserror(self):
        csv = Path(self.tmp) / "dir.csv"
        csv.mkdir()
        d = self.c("", kind="jaguar2", outputs={"jaguar2_csv": csv})
        self.assertEqual(d.status, "jaguar_sem_saida")


if __name__ == "__main__":
    unittest.main()
