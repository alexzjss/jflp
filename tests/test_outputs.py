import gzip, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sbfl.outputs import (LineScore, jaguar1_counts, reconcile_jaguar1, first_project_frame, parse_surefire_failures, ranking_csv, jaguar1_scores, parse_jaguar1_log, parse_jaguar2_csv,
                          parse_jaguar2_xml, parse_surefire, test_counts)


class OutputsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.t = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_csv_parsing(self):
        f = self.t / "jaguar2.csv"
        f.write_text("org/jsoup/helper/StringUtil,42,1,0,3,10,0.125988\n"
                     "\n"
                     "org/jsoup/Outer$Inner,7,2,1,0,4,0.5\n", encoding="utf-8")
        self.assertEqual(parse_jaguar2_csv(f), [
            LineScore("org.jsoup.helper.StringUtil", 42, 1, 3, 0, 10, 0.125988),
            LineScore("org.jsoup.Outer$Inner", 7, 2, 0, 1, 4, 0.5),
        ])

    def test_csv_empty(self):
        f = self.t / "empty.csv"
        f.write_text("", encoding="utf-8")
        self.assertEqual(parse_jaguar2_csv(f), [])

    def test_csv_malformed_names_line(self):
        f = self.t / "bad.csv"
        f.write_text("a/B,1,1,0,0,0,0.1\na/B,2,1\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "linha 2"):
            parse_jaguar2_csv(f)

    def test_xml_parsing(self):
        f = self.t / "jaguar2.xml"
        f.write_text('<report><tests fail="1" pass="4"/>'
                     '<package name="org/jsoup/helper">'
                     '<sourcefile name="StringUtil.java">'
                     '<line nr="42" cef="1" cep="3" susp="0.125988"/>'
                     '<line nr="50" cef="0" cep="2" susp="0"/>'
                     '</sourcefile></package></report>', encoding="utf-8")
        tests, scores = parse_jaguar2_xml(f)
        self.assertEqual(tests, {"fail": 1, "pass": 4})
        self.assertEqual(scores, [
            LineScore("org.jsoup.helper.StringUtil", 42, 1, 3, None, None, 0.125988),
            LineScore("org.jsoup.helper.StringUtil", 50, 0, 2, None, None, 0.0),
        ])

    def test_surefire_statuses(self):
        rep = self.t / "sf"
        rep.mkdir()
        (rep / "TEST-x.FooTest.xml").write_text(
            '<testsuite><testcase classname="x.FooTest" name="a"/>'
            '<testcase classname="x.FooTest" name="b"><failure message="m"/></testcase>'
            '<testcase classname="x.FooTest" name="c"><skipped/></testcase>'
            '<testcase classname="x.FooTest" name="d"><error/></testcase></testsuite>')
        self.assertEqual(parse_surefire(rep), {"x.FooTest#a": "passed", "x.FooTest#b": "failed",
                                               "x.FooTest#c": "skipped", "x.FooTest#d": "error"})

    def test_jaguar1_log_last_attempt_only(self):
        log = self.t / "j.log.gz"
        lines = ["# sbfl attempt 1", "10:00 DEBUG JaguarLogger - Test z(x.Crash) : Passed",
                 "# sbfl attempt 2", "10:01 DEBUG JaguarLogger - Test a(x.FooTest) : Passed",
                 "10:01 INFO JaguarLogger - Test b(x.FooTest) : Failed"]
        with gzip.open(log, "wt", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.assertEqual(parse_jaguar1_log(log), {"x.FooTest#a": "passed", "x.FooTest#b": "failed"})

    def test_counts(self):
        res = {"a#x": "passed", "a#y": "failed", "a#z": "error", "a#w": "skipped", "a#v": "passed"}
        self.assertEqual(test_counts(res),
                         {"passed": 2, "failed": 1, "error": 1, "skipped": 1, "total": 5})
        self.assertEqual(test_counts({}),
                         {"passed": 0, "failed": 0, "error": 0, "skipped": 0, "total": 0})

    def test_jaguar1_scores_maps_req(self):
        xml = self.t / "r.xml"
        xml.write_text('<F><requirements location="3" cef="1" cep="0" cnf="0" cnp="3" '
                       'name="a.B" suspicious-value="1.0"/></F>', encoding="utf-8")
        self.assertEqual(jaguar1_scores(xml), [LineScore("a.B", 3, 1, 0, 0, 3, 1.0)])


if __name__ == "__main__":
    unittest.main(verbosity=2)


STACK = """java.lang.AssertionError: boom
\tat org.junit.Assert.fail(Assert.java:88)
\tat java.base/java.util.Objects.requireNonNull(Objects.java:208)
\tat sun.reflect.X.invoke(X.java:1)
\tat a.util.Calc.soma(Calc.java:42)
\tat a.CalcTest.testSoma(CalcTest.java:10)
\tat org.apache.maven.surefire.junit4.JUnit4Provider.execute(JUnit4Provider.java:365)
"""


class ProvavelErroTest(unittest.TestCase):
    def test_primeiro_quadro_do_projeto_prefere_codigo_de_producao(self):
        self.assertEqual(first_project_frame(STACK, "a.CalcTest"),
                         {"cls": "a.util.Calc", "method": "soma", "file": "Calc.java", "line": 42})

    def test_so_quadros_do_teste_devolve_o_do_teste(self):
        st = "x: y\n\tat org.junit.Assert.fail(Assert.java:1)\n\tat a.CalcTest.t(CalcTest.java:7)\n"
        self.assertEqual(first_project_frame(st, "a.CalcTest")["line"], 7)
        self.assertIsNone(first_project_frame("sem pilha", "a.CalcTest"))

    def test_quadro_sem_numero_de_linha(self):
        f = first_project_frame("\tat a.B.m(Native Method)\n", "a.T")
        self.assertEqual((f["cls"], f["line"]), ("a.B", None))

    def test_falhas_do_surefire(self):
        d = Path(tempfile.mkdtemp())
        (d / "TEST-a.CalcTest.xml").write_text(
            '<testsuite><testcase classname="a.CalcTest" name="ok"/>'
            '<testcase classname="a.CalcTest" name="testSoma"><failure message="boom" type="java.lang.AssertionError">'
            + STACK + '</failure></testcase>'
            '<testcase classname="a.CalcTest" name="e"><error type="java.lang.NullPointerException">'
            'java.lang.NullPointerException\n\tat a.util.Calc.x(Calc.java:5)\n</error></testcase></testsuite>',
            encoding="utf-8")
        got = parse_surefire_failures(d)
        self.assertEqual([g["test"] for g in got], ["a.CalcTest#testSoma", "a.CalcTest#e"])
        self.assertEqual(got[0]["message"], "boom")
        self.assertEqual(got[0]["frame"]["line"], 42)
        self.assertEqual(got[1]["message"], "java.lang.NullPointerException")   # sem atributo message: 1ª linha
        self.assertEqual(got[1]["frame"]["cls"], "a.util.Calc")

    def test_ranking_csv(self):
        sc = [LineScore("a.B", 2, 1, 0, 0, 1, 0.5), LineScore("a.A", 9, 1, 0, 0, 1, 0.9),
              LineScore("a.C", 1, 1, 0, 0, 1, 0.5)]
        self.assertEqual(ranking_csv(sc).splitlines(),
                         ["class,line,score,rank", "a.A,9,0.9,1", "a.B,2,0.5,2", "a.C,1,0.5,2"])


class Jaguar1ContagemTest(unittest.TestCase):
    """O log do Jaguar sai pelo stdout e um teste pode trocá-lo; o XML é a contagem confiável."""
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.x = Path(self._tmp.name) / "jaguar1.xml"
        self.x.write_text(
            '<F><requirements location="1" cef="1" cep="0" cnf="0" cnp="258" name="a.B" suspicious-value="1.0"/>'
            '<requirements location="2" cef="0" cep="258" cnf="1" cnp="0" name="a.B" suspicious-value="0.0"/>'
            '<requirements location="3" cef="0" cep="100" cnf="1" cnp="158" name="a.C" suspicious-value="0.0"/></F>',
            encoding="utf-8")

    def test_contagem_pelo_xml(self):
        self.assertEqual(jaguar1_counts(self.x), {"failed": 1, "passed": 258, "total": 259})

    def test_xml_sem_linhas_nao_tem_contagem(self):
        self.x.write_text('<FlatFaultClassification heuristic="OCHIAI"/>', encoding="utf-8")
        self.assertIsNone(jaguar1_counts(self.x))

    def test_log_cortado_usa_a_contagem_do_xml_e_registra_falha_sem_nome(self):
        log = {f"a.T#t{i}": "passed" for i in range(19)}
        tests, counts, cortado = reconcile_jaguar1(log, jaguar1_counts(self.x))
        self.assertTrue(cortado)
        self.assertEqual(counts, {"total": 259, "passed": 258, "failed": 1, "error": 0, "skipped": 0})
        self.assertEqual([k for k, v in tests.items() if v == "failed"], ["(falha sem nome 1)"])
        self.assertEqual(sum(v == "passed" for v in tests.values()), 19)     # nomes do log preservados

    def test_falha_ganha_o_nome_do_teste_relevante_conhecido(self):
        tests, _, _ = reconcile_jaguar1({"a.T#ok": "passed"}, {"failed": 1, "passed": 5, "total": 6},
                                        ["a.PluginManagerTest", "a.PluginManagerTest#testX"])
        self.assertEqual(tests["a.PluginManagerTest#testX"], "failed")

    def test_log_completo_nao_e_marcado_como_cortado_nem_duplica_falhas(self):
        log = {"a.T#f": "failed", "a.T#p": "passed"}
        tests, counts, cortado = reconcile_jaguar1(log, {"failed": 1, "passed": 1, "total": 2})
        self.assertFalse(cortado)
        self.assertEqual(tests, log)
        self.assertEqual((counts["failed"], counts["passed"]), (1, 1))

    def test_sem_xml_vale_o_log(self):
        tests, counts, cortado = reconcile_jaguar1({"a.T#f": "failed"}, None)
        self.assertEqual((counts["failed"], counts["total"], cortado), (1, 1, False))
