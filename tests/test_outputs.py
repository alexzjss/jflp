import gzip, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sbfl.outputs import (LineScore, jaguar1_scores, parse_jaguar1_log, parse_jaguar2_csv,
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
