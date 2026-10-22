from __future__ import annotations

import contextlib
import sqlite3
import unittest

from sbfl import queue as jq
from sbfl import store
from sbfl.adapters.base import Bug
from tests.web_helpers import HAVE_WEB, WebFixture


@unittest.skipUnless(HAVE_WEB, "fastapi/httpx não instalados")
class ResultadosWeb(WebFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.worker = jq.Worker(self.home, self.settings)     # sem iniciar a thread: só o banco
        self.app.state.worker = self.worker
        self.c = self.authed()

    def sql(self, q):
        with contextlib.closing(sqlite3.connect(str(self.home.db_path))) as con, con:
            con.execute(q)

    def fake_run(self, rel, status="ok", kind="jaguar2", seconds=75.0):
        store._index(self.home, rel, {"kind": kind, "status": status, "started": "2026-01-02T10:00:00",
                                      "seconds": seconds, "tests": {"total": 3}})

    def test_empty_home_renders_with_calls_to_action(self):
        r = self.c.get("/resultados")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Em andamento", r.text)
        self.assertIn("Lotes recentes", r.text)
        self.assertIn("Execuções recentes", r.text)
        self.assertIn("Nenhuma execução ainda", r.text)
        self.assertIn("Nenhum lote ainda", r.text)
        self.assertIn("Rodar algo", r.text)

    def test_runs_batches_and_filters(self):
        self.fake_run("b/proj/1/20260102-100000-jaguar2", "ok")
        self.fake_run("b/proj/2/20260102-100001-jaguar2", "compilacao_falhou")
        self.fake_run("b/outro/3/20260102-100002-jaguar1", "sem_teste_falhando", kind="jaguar1")
        bid = jq.create_batch(self.home, "meu lote", [Bug("b", "proj", "1", {"dir": "/x"})], ["jaguar2"], {})
        self.sql("UPDATE jobs SET state='done', run_rel='b/proj/1/20260102-100000-jaguar2' WHERE batch_id=%d" % bid)
        r = self.c.get("/resultados")
        self.assertIn("/run/b/proj/1/20260102-100000-jaguar2", r.text)
        self.assertIn("erro de compilação", r.text)
        self.assertIn("meu lote", r.text)
        self.assertIn("1/1", r.text)
        self.assertIn('href="/lote/%d"' % bid, r.text)
        self.assertIn("1 ok", r.text)
        f = self.c.get("/resultados/execucoes", params={"tom": "erro"})
        self.assertIn("proj/2", f.text)
        self.assertNotIn("proj/1", f.text)
        f = self.c.get("/resultados/execucoes", params={"ferramenta": "jaguar1"})
        self.assertIn("outro/3", f.text)
        self.assertNotIn("proj/2", f.text)
        f = self.c.get("/resultados/execucoes", params={"q": "OUTRO"})
        self.assertIn("outro/3", f.text)
        f = self.c.get("/resultados/execucoes", params={"q": "inexistente"})
        self.assertIn("combina com o filtro", f.text)

    def test_running_and_pending_cards_and_badge(self):
        self.assertEqual(self.c.get("/fila/contagem").text, "")
        bid = jq.create_batch(self.home, "l", [Bug("local", "p", str(i), {"dir": "/x"}) for i in range(3)],
                              ["jaguar2"], {})
        self.sql("UPDATE jobs SET state='running' WHERE idx=0 AND batch_id=%d" % bid)
        r = self.c.get("/resultados")
        self.assertIn("/mini", r.text)
        self.assertIn("/cancelar", r.text)
        self.assertNotIn("Rodar algo", r.text.split("Lotes recentes")[0])
        self.assertIn(">3<", self.c.get("/fila/contagem").text)

    def test_nav_items_and_active_states(self):
        r = self.c.get("/resultados").text
        self.assertIn('href="/rodar"', r)
        self.assertIn('href="/ambiente"', r)
        self.assertIn("<details", r)
        for href in ("/comparar", "/receitas", "/exportar", "/executar", "/bugs", "/lotes", "/fila"):
            self.assertIn('href="%s"' % href, r)
        self.assertIn('<a href="/resultados" class="ativo">', r)
        self.assertNotIn('<details class="mais" open', r)

    def test_nav_highlight_mapping_and_mais_open(self):
        fila = self.c.get("/fila").text
        self.assertIn('<a href="/resultados" class="ativo">', fila)
        self.assertIn('<details class="mais" open', fila)
        self.assertIn('<a href="/fila" class="ativo">', fila)
        lotes = self.c.get("/lotes").text
        self.assertIn('<a href="/resultados" class="ativo">', lotes)
        exe = self.c.get("/executar").text
        self.assertIn('<a href="/rodar" class="ativo">', exe)
        self.assertNotIn('<a href="/resultados" class="ativo">', exe)
        amb = self.c.get("/ambiente").text
        self.assertIn('<a href="/ambiente" class="ativo">', amb)
        self.assertNotIn('<details class="mais" open', amb)
