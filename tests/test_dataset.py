from __future__ import annotations

import csv
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from sbfl import dataset
from sbfl.adapters.base import Bug
from sbfl.home import Home
from sbfl.store import RunWriter

COLS = "benchmark,project,bug,status,test_scope,n_tests,n_failed,jdk,maven,jaguar2_version,recipe,path"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = Home(self.tmp / "Pasta Ção").ensure()

    def make_run(self, bug, status, kind="jaguar2", outputs=True):
        w = RunWriter(self.home, bug, kind)
        if outputs:
            (w.dir / "outputs" / "surefire-reports").mkdir(parents=True)
            (w.dir / "outputs" / "jaguar2.csv").write_text("classe,linha\na/B,3\n")
            (w.dir / "outputs" / "jaguar2.xml").write_text("<report/>")
            (w.dir / "outputs" / "surefire-reports" / "r.xml").write_text("<x/>")
        w.write_json("env.json", {"toolenv": {"jdk": {"major": 11}, "maven": {"version": "3.9.9"}}})
        w.finish({"status": status, "test_scope": "relevant",
                  "tests": {"total": 10, "failed": 2, "error": 1}, "recipe": {"match": "d4j/Lang/*"}})
        return w.rel


class DatasetTests(Base):
    def test_dataset_manifest_columns(self):
        bug = Bug("d4j", "Lang", "1")
        self.make_run(bug, "ok_testes_falhando")
        out = dataset.export(self.home, "meu dataset!", [bug])
        self.assertEqual(out.name, "meu_dataset_")
        with open(out / "manifest.csv", encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(",".join(rows[0]), COLS)
        self.assertEqual(rows[1], ["d4j", "Lang", "1", "ok_testes_falhando", "relevant", "10", "3",
                                   "11", "3.9.9", "0.0.2", "d4j/Lang/*", "d4j/Lang/1"])
        base = out / "d4j" / "Lang" / "1"
        for f in ("jaguar2.csv", "jaguar2.xml", "env.json", "surefire-reports/r.xml"):
            self.assertTrue((base / f).exists(), f)
        meta = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["schema_version"], 1)
        self.assertTrue((out / "README.md").read_text(encoding="utf-8").startswith("# Dataset"))

    def test_dataset_includes_failed_bugs_status(self):
        good, bad = Bug("d4j", "Lang", "1"), Bug("d4j", "Lang", "2")
        self.make_run(good, "ok")
        self.make_run(bad, "compilacao_falhou", outputs=False)
        out = dataset.export(self.home, "ds", [good, bad])
        with open(out / "manifest.csv", encoding="utf-8", newline="") as f:
            rows = {r["bug"]: r for r in csv.DictReader(f)}
        self.assertEqual(rows["2"]["status"], "compilacao_falhou")
        self.assertEqual(rows["2"]["path"], "")
        self.assertFalse((out / "d4j" / "Lang" / "2").exists())
        self.assertEqual(rows["1"]["status"], "ok")

    def test_refuses_overwrite(self):
        bug = Bug("d4j", "Lang", "1")
        self.make_run(bug, "ok")
        dataset.export(self.home, "ds", [bug])
        with self.assertRaises(ValueError):
            dataset.export(self.home, "ds", [bug])


if __name__ == "__main__":
    unittest.main()
