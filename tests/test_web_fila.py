from __future__ import annotations

import contextlib
import sqlite3
import unittest
from unittest import mock

from sbfl import queue as jq
from sbfl.adapters.base import Bug
from tests.web_helpers import HAVE_WEB, WebFixture

HX = {"hx-request": "true"}


@unittest.skipUnless(HAVE_WEB, "fastapi/httpx não instalados")
class FilaWeb(WebFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.worker = jq.Worker(self.home, self.settings)     # sem iniciar a thread: só o banco
        self.app.state.worker = self.worker
        self.c = self.authed()

    def batch(self, n=2, name="lote"):
        return jq.create_batch(self.home, name, [Bug("local", "p", str(i), {"dir": f"/x/{i}"}) for i in range(n)],
                               ["jaguar2"], {})

    def sql(self, q):
        with contextlib.closing(sqlite3.connect(str(self.home.db_path))) as con, con:
            con.execute(q)

    def test_cancelar_job_delegates_to_worker(self):
        bid = self.batch(1)
        jid = jq.batch_jobs(self.home, bid)[0]["id"]
        with mock.patch.object(self.worker, "cancel_job", return_value="andamento") as cj:
            r = self.c.post(f"/job/{jid}/cancelar", data={"proximo": "/rodar"}, follow_redirects=False)
        cj.assert_called_once_with(jid)
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/rodar"))
        r = self.c.post(f"/job/{jid}/cancelar", data={"proximo": "//evil.example"}, follow_redirects=False)
        self.assertEqual(r.headers["location"], "/resultados")  # nada de redirecionar para fora
        self.assertEqual(self.c.post("/job/9999/cancelar").status_code, 404)

    def test_excluir_lote(self):
        busy, fin = self.batch(1, "ativo"), self.batch(1, "feito")
        r = self.c.post(f"/lote/{busy}/excluir", headers=HX)   # pendente: recusa
        self.assertEqual(r.status_code, 409)
        self.assertIn("cancele antes", r.text)
        self.sql(f"UPDATE jobs SET state='done' WHERE batch_id={fin}")
        self.sql(f"UPDATE batches SET state='done' WHERE id={fin}")
        r = self.c.post(f"/lote/{fin}/excluir", follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/resultados"))
        self.assertEqual([b["id"] for b in jq.list_batches(self.home)], [busy])
        self.assertEqual(self.c.post(f"/lote/{fin}/excluir").status_code, 404)
        self.assertEqual(self.c.post(f"/lote/{busy}/foo").status_code, 404)   # a rota genérica /lote/{bid}/{acao} saiu
