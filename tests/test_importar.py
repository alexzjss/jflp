from __future__ import annotations

import contextlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from sbfl import cobertura as cb
from sbfl import compare, importar, store
from sbfl import queue as jq
from sbfl.home import Home, load_settings

FIX = Path(__file__).parent / "fixtures" / "importar" / "raiz"
ANTIGO = ("commons-math", "bugs-dot-jar_MATH-1005_91d280b7")
NOVO = ("maven", "bugs-dot-jar_MNG-1797_5d99b35c")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = Home(self.tmp / "home").ensure()
        self.settings = load_settings(self.home)
        self.raiz = self.tmp / "raiz"
        shutil.copytree(FIX, self.raiz)
        self.bid = self.lote(ANTIGO, NOVO)

    def lote(self, *bugs) -> int:
        with contextlib.closing(jq._connect(self.home)) as con, con:
            bid = con.execute("INSERT INTO batches(name, created, kinds, options, state, meta) VALUES(?,?,?,?,?,?)",
                              (cb.NOME_LOTE, "2026-10-30T10:00:00", json.dumps(list(cb.KINDS)), "{}", "active",
                               json.dumps({"cobertura": True}))).lastrowid
            idx = 0
            for proj, nome in bugs:
                for k in cb.KINDS:
                    con.execute("INSERT INTO jobs(batch_id, idx, bug_key, bug_extra, kind, state) "
                                "VALUES(?,?,?,?,?,'pending')",
                                (bid, idx, f"bugsjar/{proj}/{nome}", json.dumps({"repo": str(self.tmp / proj)}), k))
                    idx += 1
        return bid

    def job(self, bug, kind):
        key = f"bugsjar/{bug[0]}/{bug[1]}"
        return next(j for j in jq.batch_jobs(self.home, self.bid) if j["bug_key"] == key and j["kind"] == kind)

    def run_json(self, bug, kind):
        return store.load_run(self.home, self.job(bug, kind)["run_rel"])

    def importar(self):
        return importar.importar_pipeline(self.home, self.settings, self.raiz)


class TestImportar(Base):
    def test_importa_formato_antigo(self):
        r = self.importar()
        self.assertEqual(r["erros"], [])
        m = self.run_json(ANTIGO, "maven")
        self.assertEqual(m["status"], "ok_testes_falhando")
        self.assertEqual(m["origem"], "importado")
        self.assertEqual(m["test_scope"], "all")
        self.assertEqual(m["tests"], {"total": 5035, "passed": 4984, "failed": 1, "error": 1, "skipped": 49})
        self.assertEqual([f["test"] for f in m["failures"]],
                         ["org.apache.commons.math3.util.FastMathTest#checkMissingFastMathClasses",
                          "org.apache.commons.math3.util.MathArraysTest#testLinearCombinationWithSingleElementArray"])
        j1 = self.run_json(ANTIGO, "jaguar1")
        self.assertEqual(j1["kind"], "jaguar1")
        self.assertEqual(j1["status"], "ok_testes_falhando")

    def test_importa_formato_run_bug2(self):
        self.importar()
        m = self.run_json(NOVO, "maven")
        self.assertEqual(m["status"], "ok_testes_falhando")     # BUILD FAILURE, mas rodou 167 testes e 1 falhou
        self.assertEqual(m["tests"]["total"], 167)
        self.assertEqual(m["tests"]["failed"], 1)
        j = self.job(NOVO, "jaguar1")
        self.assertEqual((j["state"], j["origem"]), ("done", "importado"))
        self.assertTrue(store.run_dir(self.home, j["run_rel"]).joinpath("outputs", "jaguar1.xml").is_file())

    def test_resultado_do_importar(self):
        self.assertEqual(self.importar(), {"maven": 2, "jaguar1": 2, "ignorados": 0, "erros": []})

    def test_ignora_origin(self):
        origem = self.raiz / "maven" / "origin" / NOVO[1]
        shutil.copytree(self.raiz / "maven" / NOVO[1], origem)
        r = self.importar()
        self.assertEqual((r["maven"], r["jaguar1"], r["ignorados"], r["erros"]), (2, 2, 0, []))
        self.assertEqual(len(store.list_runs(self.home, kind="maven")), 2)

    def test_idempotente(self):
        self.importar()
        n = len(store.list_runs(self.home))
        r = self.importar()
        self.assertEqual((r["maven"], r["jaguar1"]), (0, 0))
        self.assertEqual(r["ignorados"], 4)
        self.assertEqual(len(store.list_runs(self.home)), n)

    def test_jaguar2_fica_pendente(self):
        self.importar()
        for b in (ANTIGO, NOVO):
            j = self.job(b, "jaguar2")
            self.assertEqual((j["state"], j["run_rel"]), ("pending", None))

    def test_marca_jobs_como_feitos(self):
        self.importar()
        j = self.job(ANTIGO, "maven")
        self.assertEqual((j["state"], j["origem"]), ("done", "importado"))
        self.assertTrue(j["run_rel"])

    def test_run_importado_tem_scores(self):
        self.importar()
        rel = self.job(ANTIGO, "jaguar1")["run_rel"]
        scores = compare.run_scores(self.home, rel)
        self.assertTrue(scores)
        self.assertEqual(scores[0].cls, "org.apache.commons.math3.stat.correlation.StorelessCovariance")

    def test_bug_fora_do_lote_e_ignorado(self):
        shutil.copytree(self.raiz / "maven" / NOVO[1], self.raiz / "maven" / "bugs-dot-jar_OUTRO_00000000")
        r = self.importar()
        self.assertEqual((r["maven"], r["ignorados"], r["erros"]), (2, 1, []))

    def test_varios_modulos_copia_o_maior(self):
        d = self.raiz / "maven" / NOVO[1]
        linhas = (d / "control-flow-1.xml").read_text(encoding="utf-8").rstrip().split("\n")
        # cabeçalho (2 linhas) + 1 requisito (3 linhas) + fechamento
        (d / "control-flow-2.xml").write_text("\n".join(linhas[:5] + linhas[-1:]), encoding="utf-8")
        self.importar()
        run = self.run_json(NOVO, "jaguar1")
        self.assertTrue(any("control-flow-2.xml" in w for w in run["warnings"]))
        scores = compare.run_scores(self.home, self.job(NOVO, "jaguar1")["run_rel"])
        self.assertEqual(len(scores), 6)

    def test_sem_xml_deixa_jaguar1_pendente(self):
        (self.raiz / "maven" / NOVO[1] / "control-flow-1.xml").unlink()
        self.importar()
        self.assertEqual(self.job(NOVO, "jaguar1")["state"], "pending")
        self.assertEqual(self.job(NOVO, "maven")["state"], "done")

    def test_status_do_maven(self):
        casos = [({"maven": "TIMEOUT", "maven_tests": ""}, "timeout"),
                 ({"maven": "SEM CHECKOUT"}, "checkout_falhou"),
                 ({"maven": "BUILD FAILURE", "maven_tests": "run=0 fail=0 err=0 skip=0"}, "compilacao_falhou"),
                 ({"maven": "BUILD SUCCESS", "maven_tests": "run=10 fail=0 err=0 skip=1"}, "sem_teste_falhando")]
        for campos, esperado in casos:
            with self.subTest(esperado):
                self.assertEqual(importar.status_maven(campos, ""), esperado)

    def test_lote_inexistente(self):
        with contextlib.closing(jq._connect(self.home)) as con, con:
            con.execute("DELETE FROM batches")
        r = self.importar()
        self.assertEqual((r["maven"], r["jaguar1"]), (0, 0))
        self.assertTrue(r["erros"])


if __name__ == "__main__":
    unittest.main()
