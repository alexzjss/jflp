from __future__ import annotations

import contextlib
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from sbfl import queue as jq
from sbfl.adapters.base import Bug
from sbfl.home import Home, load_settings


class Reservas(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = Home(self.tmp / "home").ensure()
        self.settings = load_settings(self.home)
        self.settings.set("cobertura", "min_livre_gb", 0)      # o disco do teste não pausa o lote

    def lote(self, n=2, cobertura=True):
        bugs = [Bug("local", "p", str(i), {"dir": f"/x/{i}"}) for i in range(n)]
        bid = jq.create_batch(self.home, "t", bugs, ["jaguar2"], {})
        if cobertura:
            self.sql("UPDATE batches SET meta=? WHERE id=?", json.dumps({"cobertura": True}), bid)
        return bid

    def sql(self, q, *a):
        with contextlib.closing(jq._connect(self.home)) as con, con:
            return con.execute(q, a).fetchall()

    def worker(self, hook=None, restante=None):
        w = jq.Worker(self.home, self.settings, owns_lock=False)
        w.reservar_remoto, w.restante_remoto = hook, restante
        return w

    def estados(self):
        return {r["bug_key"]: (r["state"], r["remoto_ate"])
                for r in self.sql("SELECT bug_key, state, remoto_ate FROM jobs ORDER BY idx")}


class TestReservas(Reservas):
    def test_negada_volta_a_pendente_e_pega_o_proximo(self):
        self.lote()
        vistos, avisos = [], []
        def hook(k):
            vistos.append(k)
            return len(vistos) > 1
        w = self.worker(hook, lambda k: 120.0)
        w.on_bug_change = avisos.append
        t0 = time.time()
        job, _ = w._next_job()
        self.assertEqual(len(vistos), 2)
        self.assertEqual(job["bug_key"], vistos[1])
        est = self.estados()
        self.assertEqual(est[vistos[0]][0], "pending")
        self.assertAlmostEqual(est[vistos[0]][1], t0 + 120, delta=5)
        self.assertEqual(est[vistos[1]], ("running", None))
        self.assertEqual(avisos, [])

    def test_todas_negadas_devolve_none_e_usa_ttl_padrao(self):
        self.lote(1)
        w = self.worker(lambda k: False)
        self.assertIsNone(w._next_job())
        (estado, ate), = self.estados().values()
        self.assertEqual(estado, "pending")
        self.assertAlmostEqual(ate, time.time() + 1800, delta=5)
        self.assertIsNone(w._next_job())

    def test_remoto_ate_no_futuro_exclui_e_no_passado_libera(self):
        self.lote(1)
        w = self.worker()
        self.sql("UPDATE jobs SET remoto_ate=?", time.time() + 60)
        self.assertIsNone(w._next_job())
        self.sql("UPDATE jobs SET remoto_ate=?", time.time() - 1)
        self.assertIsNotNone(w._next_job())

    def test_gancho_que_falha_conta_como_liberado(self):
        self.lote(1)
        w = self.worker(lambda k: 1 / 0)
        self.assertIsNotNone(w._next_job())
        self.assertIn("ZeroDivisionError", w._last_error)
        self.assertEqual(list(self.estados().values()), [("running", None)])

    def test_lote_que_nao_e_de_cobertura_nao_chama_o_gancho(self):
        self.lote(1, cobertura=False)
        chamadas = []
        w = self.worker(lambda k: chamadas.append(k) or False)
        self.assertIsNotNone(w._next_job())
        self.assertEqual(chamadas, [])

    def test_sem_gancho_segue_normal(self):
        self.lote(1)
        self.assertIsNotNone(self.worker()._next_job())

    def test_pool_repassa_os_ganchos(self):
        f, g = (lambda k: True), (lambda k: 1.0)
        pool = jq.WorkerPool(self.home, self.settings, n=2, reservar_remoto=f, restante_remoto=g)
        self.assertTrue(pool._workers)
        for w in pool._workers:
            self.assertIs(w.reservar_remoto, f)
            self.assertIs(w.restante_remoto, g)


if __name__ == "__main__":
    unittest.main()
