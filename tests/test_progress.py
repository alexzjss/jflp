from __future__ import annotations

import json
import unittest

from sbfl.progress import Progress, parse_goal

MAVEN_LONG = [
    "# jflp tentativa 1",
    "# jflp fase: pom",
    "# jflp fase: maven",
    "[INFO] Scanning for projects...",
    "[INFO] Building Commons Math 3.3-SNAPSHOT",
    "[INFO] Downloading from central: https://repo.maven.apache.org/maven2/a/plexus-io-1.0.jar",
    "[INFO] Downloaded from central: https://repo.maven.apache.org/maven2/a/plexus-io-1.0.jar (51 kB at 139 kB/s)",
    "[INFO] --- maven-compiler-plugin:3.8.0:compile (default-compile) @ commons-math3 ---",
    "[INFO] Compiling 795 source files to C:\\proj\\target\\classes",
    "[INFO] --- maven-surefire-plugin:2.14.1:test (default-test) @ commons-math3 ---",
    "[INFO] Running org.apache.commons.math3.util.MathArraysTest",
    "[ERROR] Tests run: 41, Failures: 0, Errors: 1, Skipped: 0, Time elapsed: 0.518 s <<< FAILURE! "
    "-- in org.apache.commons.math3.util.MathArraysTest",
    "[INFO] Running demo.OtherTest",
    "[INFO] Tests run: 12, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.1 s - in demo.OtherTest",
    "Results :",
    "Tests run: 53, Failures: 0, Errors: 1, Skipped: 0",
]


def fed(lines, kind="maven") -> Progress:
    p = Progress(kind, clock=lambda: 100.0)
    for ln in lines:
        p.feed(ln)
    return p


class TomTest(unittest.TestCase):
    def test_estilo_por_status(self):
        from sbfl import status as st
        self.assertEqual(st.tom("ok"), "ok")
        self.assertEqual(st.tom("ok_testes_falhando"), "ok")
        self.assertEqual(st.tom("sem_teste_falhando"), "alerta")
        self.assertEqual(st.tom("timeout"), "erro")
        self.assertEqual(st.tom(""), "erro")
        self.assertFalse(st.is_success("sem_teste_falhando"))


class ParseGoalTest(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(parse_goal("--- maven-compiler-plugin:3.8.0:compile (default-compile) @ x ---"),
                         ("compiler", "compile"))
        self.assertEqual(parse_goal("--- compiler:3.13.0:testCompile (default-testCompile) @ x ---"),
                         ("compiler", "testCompile"))
        self.assertEqual(parse_goal("--- org.jacoco:jacoco-maven-plugin:0.8.7:prepare-agent (a) @ x ---"),
                         ("jacoco", "prepare-agent"))
        self.assertIsNone(parse_goal("--- sem forma ---"))


class MavenProgressTest(unittest.TestCase):
    def test_counts_without_double_counting_summary(self):
        t = fed(MAVEN_LONG).to_dict()["testes"]
        self.assertEqual((t["rodados"], t["falhas"], t["erros"], t["ignorados"]), (53, 0, 1, 0))
        self.assertEqual(t["classes_falhas"], ["org.apache.commons.math3.util.MathArraysTest"])
        self.assertEqual(t["classe"], "demo.OtherTest")

    def test_phase_long_and_short_forms(self):
        p = fed(["# jflp tentativa 1", "[INFO] --- maven-compiler-plugin:3.8.0:compile (c) @ a ---"])
        self.assertEqual((p.to_dict()["fase"], p.to_dict()["passo"]), ("compilando", "compilar"))
        p.feed("[INFO] --- compiler:3.13.0:testCompile (default-testCompile) @ a ---")
        self.assertEqual(p.to_dict()["fase"], "compilando testes")
        p.feed("[INFO] --- jacoco:0.8.7:prepare-agent (x) @ a ---")
        self.assertEqual(p.to_dict()["fase"], "preparando cobertura")
        p.feed("[INFO] --- surefire:3.2.5:test (default-test) @ a ---")
        self.assertEqual((p.to_dict()["fase"], p.to_dict()["passo"]), ("rodando testes", "testes"))
        p.feed("[INFO] --- jacoco:0.8.7:report (r) @ a ---")
        self.assertEqual(p.to_dict()["fase"], "relatório de cobertura")
        p.feed("[INFO] --- maven-rat-plugin:0.7:check (rat) @ a ---")
        self.assertEqual(p.to_dict()["fase"], "rat:check")

    def test_phase_markers_and_steps(self):
        p = fed(["# jflp tentativa 1", "# jflp fase: reactor"])
        self.assertEqual(p.to_dict()["fase"], "preparando o reactor")
        self.assertEqual(p.to_dict()["passos"], ["preparar", "compilar", "testes", "gravar"])
        self.assertIn("analise", Progress("jaguar2").to_dict()["passos"])

    def test_reactor_module(self):
        p = fed(["[INFO] Building foo 1.0 [2/5]"]).to_dict()
        self.assertEqual(p["modulo"], {"nome": "foo", "i": 2, "n": 5})
        p = fed(["[INFO] Building Commons Math 3.3-SNAPSHOT"]).to_dict()
        self.assertEqual(p["modulo"], {"nome": "Commons Math", "i": None, "n": None})
        self.assertIsNone(fed(["[INFO] Building jar: C:\\x\\foo-1.0.jar"]).to_dict()["modulo"])

    def test_downloads(self):
        p = fed(MAVEN_LONG[:7]).to_dict()
        self.assertEqual(p["downloads"], {"n": 1, "bytes": 51000, "baixando": False})
        p = fed(["[INFO] Downloading from central: https://x/y.jar"]).to_dict()
        self.assertTrue(p["downloads"]["baixando"])
        p = fed(["[INFO] Downloading from central: https://x/y.jar", "[INFO] Compiling 3 source files"]).to_dict()
        self.assertFalse(p["downloads"]["baixando"])    # 404 sem par não deixa "baixando…" para sempre
        p = fed(["[INFO] Downloaded from c: https://x (1.5 MB at 2 MB/s)"]).to_dict()
        self.assertEqual(p["downloads"]["bytes"], 1500000)

    def test_compilation_last_wins(self):
        p = fed(["[INFO] Compiling 795 source files to C:\\a", "[INFO] Compiling 12 source files to C:\\b"])
        self.assertEqual(p.to_dict()["compilacao"], 12)

    def test_ansi_and_carriage_return(self):
        p = fed(["\x1b[1;34mINFO\x1b[m", "\x1b[0;32m[INFO] Compiling 7 source files\x1b[m",
                 "[INFO] Progress 1/9\r[INFO] Compiling 9 source files\r\n"])
        self.assertEqual(p.to_dict()["compilacao"], 9)

    def test_ultimo_erro_filters_help_noise(self):
        p = fed(["[ERROR] Failed to execute goal X: forked VM terminated",
                 "[ERROR] ",
                 "[ERROR] To see the full stack trace of the errors, re-run Maven with the -e switch.",
                 "[ERROR] Re-run Maven using the -X switch to enable full debug logging.",
                 "[ERROR] For more information about the errors and possible solutions, please read:",
                 "[ERROR] [Help 1] http://cwiki.apache.org/confluence/x"])
        self.assertTrue(p.to_dict()["ultimo_erro"].startswith("Failed to execute goal"))
        self.assertIsNone(fed(["[INFO] tudo certo"]).to_dict()["ultimo_erro"])

    def test_ultimo_erro_ignores_test_failures(self):
        # testes falhando são o bug (dado), não erro: as linhas [ERROR] do Surefire não viram "último erro"
        p = fed(["[INFO] Running a.BTest",
                 "[ERROR] Tests run: 41, Failures: 0, Errors: 1, Skipped: 0 <<< FAILURE! -- in a.BTest",
                 "[ERROR] testX(a.BTest)  Time elapsed: 0.1 s  <<< ERROR!",
                 "[ERROR] Errors: ",
                 "[ERROR]   BTest.testX:12 NullPointer",
                 "[ERROR] Tests run: 41, Failures: 0, Errors: 1, Skipped: 0",
                 "[ERROR] There are test failures."])
        self.assertIsNone(p.to_dict()["ultimo_erro"])

    def test_build_result(self):
        self.assertEqual(fed(["[INFO] BUILD FAILURE"]).to_dict()["build"], "FAILURE")


class JaguarProgressTest(unittest.TestCase):
    LINES = [
        "23:28:45.806 [main] DEBUG JaguarLogger - Test testAbsPositive(demo.CalcTest) : Passed",
        "23:28:46.116 [main] INFO  JaguarLogger - Test testAbsNegative(demo.CalcTest) : Failed",
        "23:28:46.439 [main] DEBUG JaguarLogger - Test # 2",
        "23:28:46.539 [main] DEBUG JaguarLogger - Rank calculation started...",
        "23:28:46.775 [main] INFO  JaguarLogger - Jaguar has finished!",
    ]

    def test_test_lines(self):
        d = fed(self.LINES, "jaguar1").to_dict()
        t = d["testes"]
        self.assertEqual((t["rodados"], t["falhas"], t["passaram"]), (2, 1, 1))
        self.assertEqual(t["classes_falhas"], ["demo.CalcTest"])
        self.assertEqual((d["passo"], d["fase"]), ("analise", "Jaguar concluído"))

    def test_rank_phase(self):
        d = fed(self.LINES[:4], "jaguar1").to_dict()
        self.assertEqual(d["fase"], "calculando o ranking")


class AttemptsTest(unittest.TestCase):
    def test_without_status_marker(self):
        d = fed(["# jflp tentativa 1", "# jflp fase: maven", "# jflp tentativa 2"]).to_dict()
        self.assertEqual([(a["n"], a["status"]) for a in d["tentativas"]], [(1, None), (2, None)])
        self.assertEqual((d["etapa"], d["tentativa"]), ("tentativa", 2))

    def test_with_status_marker(self):
        d = fed(["# jflp tentativa 1", "# jflp tentativa 1: plugin_incompativel", "# jflp tentativa 2"]).to_dict()
        self.assertEqual([(a["n"], a["status"]) for a in d["tentativas"]],
                         [(1, "plugin_incompativel"), (2, None)])
        self.assertFalse(d["falhou"])      # reiniciou na tentativa 2

    def test_failed_attempt_marks_step_failed(self):
        d = fed(["# jflp tentativa 1", "# jflp tentativa 1: compilacao_falhou"]).to_dict()
        self.assertTrue(d["falhou"])

    def test_sem_teste_falhando_is_aviso_not_failure_nor_success(self):
        p = fed(["# jflp tentativa 1", "# jflp tentativa 1: sem_teste_falhando"])
        d = p.to_dict()
        self.assertTrue(d["aviso"])
        self.assertFalse(d["falhou"])                     # não pinta o passo de vermelho
        self.assertEqual((d["etapa"], d["passo"]), ("tentativa", "preparar"))   # não avança para gravar
        self.assertEqual(d["tentativas"][0]["rotulo"], "concluída, mas nenhum teste falhou")
        p.finish("sem_teste_falhando")
        self.assertTrue(p.to_dict()["aviso"])
        self.assertFalse(p.to_dict()["falhou"])

    def test_success_moves_to_recording(self):
        d = fed(["# jflp tentativa 1", "# jflp tentativa 1: ok"]).to_dict()
        self.assertEqual((d["etapa"], d["passo"]), ("gravando", "gravar"))

    def test_skipped_attempt(self):
        d = fed(["# jflp tentativa 1", "# jflp tentativa 3 ignorada: JDK 17 não encontrado — baixe na tela Ambiente"]
                ).to_dict()
        skipped = [a for a in d["tentativas"] if a["n"] == 3][0]
        self.assertEqual(skipped["status"], "ignorada")
        self.assertIn("JDK 17", skipped["motivo"])

    def test_preparing_before_first_attempt(self):
        p = fed(["# jflp: sem pom.xml em C:\\x; reactor não instalado"])
        self.assertEqual(p.to_dict()["etapa"], "preparando")

    def test_per_attempt_counters_reset(self):
        p = fed(MAVEN_LONG)
        p.feed("# jflp tentativa 2")
        d = p.to_dict()
        self.assertEqual(d["testes"]["rodados"], 0)
        self.assertEqual(d["downloads"]["n"], 1)         # downloads são do job inteiro


class SerializationTest(unittest.TestCase):
    def test_json_clock_and_finish(self):
        now = [10.0]
        p = Progress("maven", clock=lambda: now[0])
        now[0] = 75.5
        self.assertEqual(p.to_dict()["decorrido"], 65.5)
        p.finish("ok")
        now[0] = 900.0
        d = p.to_dict()
        self.assertEqual((d["etapa"], d["decorrido"], d["inicio"]), ("fim", 65.5, 10.0))
        json.dumps(d)

    def test_garbage_lines_do_not_raise(self):
        p = Progress("maven")
        for ln in ("", "\r", "[", "[INFO]", "--- x ---", "Running ", "Tests run: x Time elapsed", None, 5,
                   "# jflp tentativa", "# jflp tentativa x: y", "Building"):
            p.feed(ln)
        json.dumps(p.to_dict())


if __name__ == "__main__":
    unittest.main()
