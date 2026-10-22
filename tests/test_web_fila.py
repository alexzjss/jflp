from __future__ import annotations

import contextlib
import sqlite3
import time
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

    def states(self, bid):
        return [j["state"] for j in jq.batch_jobs(self.home, bid)]

    def sql(self, q):
        with contextlib.closing(sqlite3.connect(str(self.home.db_path))) as con, con:
            con.execute(q)

    def test_fila_page_lists_running_and_pending_in_order(self):
        a, b = self.batch(2, "primeiro"), self.batch(1, "segundo")
        self.sql("UPDATE jobs SET state='running' WHERE idx=0 AND batch_id=%d" % a)
        with mock.patch.object(self.worker, "current",
                               return_value={"job_id": 1, "started": time.time() - 125}):
            r = self.c.get("/fila")
        self.assertEqual(r.status_code, 200)
        self.assertIn("local/p/0", r.text)
        self.assertIn("2 min", r.text)
        self.assertIn("/job/1/ao-vivo", r.text)
        self.assertIn("Cancelar", r.text)
        self.assertIn("Limpar fila", r.text)
        self.assertIn("Cancelar tudo", r.text)
        self.assertIn("hx-confirm", r.text)
        self.assertLess(r.text.index("local/p/1"), r.text.index("segundo"))
        self.assertIn('href="/fila"', r.text)                 # item na barra lateral

    def test_fila_empty_and_estado_fragment(self):
        r = self.c.get("/fila/estado")
        self.assertEqual(r.status_code, 200)
        self.assertIn("A fila está vazia", r.text)
        self.assertIn("Nenhum job em execução", r.text)

    def test_remover_job_htmx_and_plain(self):
        bid = self.batch(2)
        j0, j1 = [j["id"] for j in jq.batch_jobs(self.home, bid)]
        r = self.c.post(f"/job/{j0}/remover", headers=HX)
        self.assertEqual(r.status_code, 204)
        self.assertEqual(r.headers["hx-refresh"], "true")
        r = self.c.post(f"/job/{j1}/remover", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.states(bid), ["cancelled", "cancelled"])
        self.assertEqual(jq.batch_status(self.home, bid)["state"], "done")
        r = self.c.post(f"/job/{j1}/remover", headers=HX)      # já não está pendente
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.c.post("/job/9999/remover").status_code, 404)

    def test_cancelar_job_delegates_to_worker(self):
        bid = self.batch(1)
        jid = jq.batch_jobs(self.home, bid)[0]["id"]
        with mock.patch.object(self.worker, "cancel_job", return_value="andamento") as cj:
            r = self.c.post(f"/job/{jid}/cancelar", data={"proximo": "/executar/5"}, follow_redirects=False)
        cj.assert_called_once_with(jid)
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/executar/5"))
        r = self.c.post(f"/job/{jid}/cancelar", data={"proximo": "//evil.example"}, follow_redirects=False)
        self.assertEqual(r.headers["location"], "/fila")       # nada de redirecionar para fora
        self.assertEqual(self.c.post("/job/9999/cancelar").status_code, 404)

    def test_limpar_and_cancelar_tudo(self):
        a, b = self.batch(2), self.batch(1)
        r = self.c.post("/fila/limpar", headers=HX)
        self.assertEqual(r.status_code, 204)
        self.assertEqual(self.states(a) + self.states(b), ["cancelled"] * 3)
        with mock.patch.object(self.worker, "cancel_everything") as ce:
            self.c.post("/fila/cancelar-tudo", follow_redirects=False)
        ce.assert_called_once()

    def test_excluir_lote(self):
        busy, fin = self.batch(1, "ativo"), self.batch(1, "feito")
        r = self.c.post(f"/lote/{busy}/excluir", headers=HX)   # pendente: recusa
        self.assertEqual(r.status_code, 409)
        self.assertIn("cancele antes", r.text)
        self.sql(f"UPDATE jobs SET state='done' WHERE batch_id={fin}")
        self.sql(f"UPDATE batches SET state='done' WHERE id={fin}")
        r = self.c.post(f"/lote/{fin}/excluir", follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/lotes"))
        self.assertEqual([b["id"] for b in jq.list_batches(self.home)], [busy])
        self.assertEqual(self.c.post(f"/lote/{fin}/excluir").status_code, 404)
        self.assertEqual(self.c.post(f"/lote/{busy}/foo").status_code, 400)   # rota genérica intacta

    def test_buttons_in_batch_lotes_and_cards(self):
        a, b = self.batch(2), self.batch(1, "feito")
        self.sql(f"UPDATE jobs SET state='running' WHERE idx=0 AND batch_id={a}")
        self.sql(f"UPDATE jobs SET state='done' WHERE batch_id={b}")
        self.sql(f"UPDATE batches SET state='done' WHERE id={b}")
        r = self.c.get(f"/lote/{a}/progresso")
        self.assertIn("Cancelar job", r.text)
        self.assertIn("Remover", r.text)
        r = self.c.get("/lotes")
        self.assertIn(f"/lote/{b}/excluir", r.text)
        self.assertNotIn(f"/lote/{a}/excluir", r.text)
        r = self.c.get(f"/executar/{a}/cartoes")
        self.assertIn("/cancelar", r.text)
