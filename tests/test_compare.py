import gzip
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from sbfl import compare, store
from sbfl.adapters.base import Bug
from sbfl.diffgt import FaultLine
from sbfl.home import Home
from sbfl.outputs import LineScore


def _rm(func, path, exc_info):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def ls(cls, line, cef, cep, susp, cnf=0, cnp=0):
    return LineScore(cls, line, cef, cep, cnf, cnp, susp)


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jflp_cmp_"))
        self.home = Home(self.tmp).ensure()

    def tearDown(self):
        shutil.rmtree(self.tmp, onerror=_rm)

    def test_compare_tests_generic(self):
        a = {"x.T#a": "passed", "x.T#b": "failed", "x.T#c": "skipped", "x.T#d": "error"}
        b = {"x.T#a": "passed", "x.T#b": "failed", "x.T#e": "passed", "x.T#d": "passed"}
        r = compare.compare_tests(a, b)
        self.assertEqual(r["only_a"], [])        # o skipped não conta como "só em A"
        self.assertEqual(r["only_b"], ["x.T#e"])
        self.assertEqual(r["outcome_differs"], [("x.T#d", "error", "passed")])
        self.assertEqual(r["a_failing"], ["x.T#b", "x.T#d"])
        self.assertEqual(r["b_failing"], ["x.T#b"])
        self.assertEqual(r["a_counts"]["skipped"], 1)
        self.assertEqual(r["b_counts"], {"passed": 3, "failed": 1})

    def test_compare_spectra_only_and_diffs(self):
        a = [ls("a.B", 1, 1, 0, 1.0), ls("a.B$I", 2, 2, 0, 0.5), ls("a.B", 3, 1, 1, 0.3)]
        b = [ls("a.B", 1, 1, 0, 1.0 + 1e-9), ls("a.B", 2, 2, 1, 0.5), ls("a.C", 9, 1, 0, 0.1)]
        r = compare.compare_spectra(a, b)
        self.assertEqual(r["only_a"], [("a.B", 3)])
        self.assertEqual(r["only_b"], [("a.C", 9)])
        self.assertEqual(r["both"], 2)
        self.assertEqual(len(r["diffs"]), 1)      # a linha 1 difere só abaixo da tolerância
        d = r["diffs"][0]
        self.assertEqual((d["cls"], d["line"], d["a"]["cep"], d["b"]["cep"]), ("a.B", 2, 0, 1))
        self.assertIn("susp > 0", r["note"])

    def test_fault_positions_ties(self):
        sc = [ls("a.B", 3, 1, 0, 1.0, 0, 3), ls("a.B", 9, 1, 0, 1.0, 0, 3), ls("a.B", 5, 1, 2, 0.5, 0, 1)]
        r = compare.fault_positions(sc, [FaultLine("a.B", 9, "removed"), FaultLine("a.B", 77, "removed")])
        self.assertEqual((r["total"], r["not_covered"]), (3, 1))
        row = r["lines"][0]
        self.assertEqual((row["line"], row["rank_best"], row["rank_worst"], row["tied"]), (9, 1, 2, 2))
        self.assertEqual((row["cef"], row["cep"], row["cnf"], row["cnp"], row["susp"]), (1, 0, 0, 3, 1.0))

    def _run(self, kind, bug_id, status, started, outputs=None, truth=None):
        w = store.RunWriter(self.home, Bug("bm", "pj", bug_id), kind)
        for rel, text in (outputs or {}).items():
            w.write_text(rel, text)
        doc = {"status": status, "started": started}
        if truth is not None:
            doc["truth"] = truth
        w.finish(doc)
        return w.rel

    def test_batch_summary_uses_last_run_per_bug(self):
        self._run("jaguar2", "1", "compilacao_falhou", "2026-01-01T10:00:00")
        self._run("jaguar2", "1", "ok", "2026-01-02T10:00:00")        # a última vence
        self._run("jaguar2", "2", "ok_testes_falhando", "2026-01-01T10:00:00")
        self._run("jaguar2", "3", "timeout", "2026-01-01T10:00:00")
        self._run("jaguar2", "4", "timeout", "2026-01-01T10:00:00")
        self._run("jaguar1", "1", "ok", "2026-01-01T10:00:00")
        rows = compare.batch_summary(self.home, store.list_runs(self.home))
        self.assertEqual([(r["kind"]) for r in rows], ["jaguar1", "jaguar2"])
        j2 = rows[1]
        self.assertEqual((j2["bugs"], j2["ok"], j2["ok_testes_falhando"]), (4, 1, 1))
        self.assertEqual(j2["falhas"], {"timeout": 2})
        self.assertEqual(j2["taxa_sucesso"], 0.5)
        self.assertEqual(rows[0]["taxa_sucesso"], 1.0)
        self.assertEqual(compare.batch_summary(self.home, []), [])
        csv_text = compare.summary_csv(rows)
        self.assertEqual(csv_text.splitlines()[0],
                         "benchmark,project,kind,bugs,ok,ok_testes_falhando,falhas,taxa_sucesso")
        self.assertEqual(csv_text.splitlines()[2], "bm,pj,jaguar2,4,1,1,2,0.5")
        self.assertNotIn("\r", csv_text)

    def test_compare_runs_maven_vs_jaguar2(self):
        sf = ('<testsuite><testcase classname="x.T" name="a"/>'
              '<testcase classname="x.T" name="b"><failure/></testcase></testsuite>')
        truth = [FaultLine("a.B", 9, "removed").to_dict()]
        m = self._run("maven", "1", "ok_testes_falhando", "2026-01-01T10:00:00",
                      {"outputs/surefire-reports/TEST-x.T.xml": sf}, truth)
        j = self._run("jaguar2", "1", "ok_testes_falhando", "2026-01-01T11:00:00",
                      {"outputs/surefire-reports/TEST-x.T.xml": sf.replace('name="a"/>', 'name="a"><failure/></testcase>'),
                       "outputs/jaguar2.csv": "a/B,9,1,0,0,3,1.0\na/B,5,1,2,0,1,0.5\n"}, truth)
        r = compare.compare_runs(self.home, m, j)
        self.assertIsNone(r["spectra"])
        self.assertIsNone(r["faults_a"])
        self.assertEqual(r["tests"]["outcome_differs"], [("x.T#a", "passed", "failed")])
        self.assertEqual(r["faults_b"]["lines"][0]["rank_best"], 1)
        self.assertEqual(compare.run_scores(self.home, m), None)
        # Jaguar 1 usa o log; Jaguar 2 cai no XML sem o CSV
        j1 = self._run("jaguar1", "1", "ok", "2026-01-01T12:00:00", truth=truth,
                       outputs={"outputs/jaguar1.xml": '<F><requirements location="9" cef="1" cep="0" cnf="0" cnp="3" '
                                                       'name="a.B" suspicious-value="1.0"/></F>'})
        d = store.run_dir(self.home, j1) / "outputs"
        os.chmod(d, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        with gzip.open(d / "jaguar1.log.gz", "wt") as f:
            f.write("# sbfl attempt 1\n10:01 INFO JaguarLogger - Test a(x.T) : Passed\n")
        self.assertEqual(compare.run_tests(self.home, j1), {"x.T#a": "passed"})
        r2 = compare.compare_runs(self.home, j, j1)
        self.assertEqual(r2["spectra"]["both"], 1)
        self.assertEqual(r2["spectra"]["only_a"], [("a.B", 5)])


if __name__ == "__main__":
    unittest.main()
