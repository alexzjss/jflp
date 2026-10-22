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

    def test_empty_home_renders_with_call_to_action(self):
        r = self.c.get("/resultados")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Nenhum lote ainda", r.text)
        self.assertIn("Rodar algo", r.text)
        for removido in ("Execuções recentes", "Em andamento", "/resultados/execucoes"):
            self.assertNotIn(removido, r.text)
        self.assertEqual(self.c.get("/resultados/execucoes").status_code, 404)

    def test_batches_listed_newest_first_with_state_and_delete(self):
        a = jq.create_batch(self.home, "meu lote", [Bug("b", "proj", "1", {"dir": "/x"})], ["jaguar2"], {})
        b = jq.create_batch(self.home, "outro", [Bug("b", "proj", str(i), {"dir": "/x"}) for i in (1, 2)],
                            ["jaguar2", "maven"], {})
        self.sql("UPDATE jobs SET state='done' WHERE batch_id=%d" % a)
        self.sql("UPDATE batches SET state='done' WHERE id=%d" % a)
        t = self.c.get("/resultados").text
        self.assertIn('href="/lote/%d"' % a, t)
        self.assertLess(t.index("#%d outro" % b), t.index("#%d meu lote" % a))
        self.assertIn("Jaguar 2, Maven", t)
        self.assertIn("terminado", t)
        self.assertIn("rodando", t)
        self.assertIn('hx-post="/lote/%d/excluir"' % a, t)         # encerrado: pode excluir
        self.assertNotIn('hx-post="/lote/%d/excluir"' % b, t)      # em andamento: não
        self.assertIn('<td class="num">2</td>', t)         # lote b: 2 bugs

    def test_only_recent_batches_get_verdict_counts(self):
        ids = [jq.create_batch(self.home, f"l{i}", [Bug("b", "proj", "1", {"dir": "/x"})], ["maven"], {})
               for i in range(22)]
        t = self.c.get("/resultados").text
        self.assertIn("sem dado</span>", t)          # os 20 mais recentes têm contagem
        self.assertEqual(t.count('class="vd vd-sem_dado"'), 20)
        self.assertEqual(len(ids), 22)

    def test_badge_counts_running_and_pending(self):
        self.assertEqual(self.c.get("/fila/contagem").text, "")
        bid = jq.create_batch(self.home, "l", [Bug("local", "p", str(i), {"dir": "/x"}) for i in range(3)],
                              ["jaguar2"], {})
        self.sql("UPDATE jobs SET state='running' WHERE idx=0 AND batch_id=%d" % bid)
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
