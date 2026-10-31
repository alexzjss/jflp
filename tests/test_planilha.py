from __future__ import annotations

import contextlib
import json
import unittest
import urllib.error
from datetime import datetime

from sbfl import planilha as pl
from sbfl import queue as jq
from sbfl.queue import Worker

from test_cobertura import Base
from test_queue import wait_for


class Fake:
    """Cliente HTTP falso: guarda os corpos recebidos e devolve as respostas na ordem."""

    def __init__(self, *respostas):
        self.respostas, self.corpos = list(respostas), []

    def __call__(self, url, body):
        self.corpos.append((url, body))
        r = self.respostas.pop(0) if self.respostas else {"ok": True}
        if isinstance(r, Exception):
            raise r
        return r


class Cobertura(Base):
    _meu_molde = None

    @classmethod
    def _criar_molde(cls):
        """Molde próprio: o do test_cobertura é apagado/alterado pelos testes daquele módulo."""
        if Cobertura._meu_molde is None:
            Base._molde = None
            Cobertura._meu_molde = Base._criar_molde.__func__(Base)
        return Cobertura._meu_molde

    def setUp(self):
        super().setUp()
        self.bid = self.sync()["batch_id"]
        self.k0 = self.jobs(self.bid)[0]["bug_key"]

    def concluir(self, key, status="ok", cfgs=None):
        from sbfl.store import _connect as sc
        for j in self.jobs(self.bid):
            if j["bug_key"] != key:
                continue
            rel = f"r/{j['id']}"
            with contextlib.closing(sc(self.home)) as con, con:
                con.execute("INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (rel, "bugsjar", "p", "b", j["kind"], status,
                             datetime.now().isoformat(timespec="seconds"), 5.0, 1, 0, None))
            self.sql("UPDATE jobs SET state='done', run_rel=? WHERE id=?", rel, j["id"])
            if cfgs is not None:
                d = self.home.runs / "r" / str(j["id"])
                d.mkdir(parents=True, exist_ok=True)
                (d / "run.json").write_text(json.dumps({"status": status, "attempt_configs": cfgs,
                                                        "attempts": len(cfgs)}), encoding="utf-8")

    def outbox(self):
        with contextlib.closing(pl._con(self.home)) as con:
            return {r["bug_key"]: r for r in con.execute("SELECT * FROM planilha_outbox")}

    def enviador(self, post, url="https://exemplo.test/exec"):
        self.settings.set("planilha", "url", url)
        self.settings.set("planilha", "token", "tok")
        return pl.Enviador(self.home, self.settings, post=post, espera_inicial=0.01, espera_maxima=0.04,
                           espera_ociosa=0.01)


class TestLinha(Cobertura):
    def test_linha_tem_todas_as_colunas(self):
        bug = next(b for b in pl._bugs_do_lote(self.home, None) if b["key"] == self.k0)
        row = pl.linha(self.home, bug)
        self.assertEqual(len(row), len(pl.COLUNAS))
        self.assertEqual(len(pl.COLUNAS), 24)
        d = dict(zip(pl.COLUNAS, row))
        self.assertEqual(d["Situação"], "pendente")
        self.assertTrue(d["Link JIRA"].startswith("https://issues.apache.org/jira/browse/"))
        self.assertEqual(d["Origem"], "app")

    def test_perfil_pela_ultima_tentativa(self):
        self.concluir(self.k0, "ok", cfgs=[{"jdk": 11}, {"jdk": 7}])
        bug = next(b for b in pl._bugs_do_lote(self.home, [self.k0]))
        self.assertEqual(dict(zip(pl.COLUNAS, pl.linha(self.home, bug)))["Perfil"], "legado")
        self.assertEqual(pl._perfil({"maven": {"attempt_configs": [{"jdk": 7, "params": {"offline": True}}]}}),
                         "legado offline")
        self.assertEqual(pl._perfil({"maven": {"attempt_configs": [{"jdk": 11}]}}), "moderno")


class TestOutbox(Cobertura):
    def test_enfileirar_upsert(self):
        pl.enfileirar(self.home, [self.k0])
        pl.enfileirar(self.home, [self.k0, self.k0])
        self.assertEqual(list(self.outbox()), [self.k0])
        self.assertEqual(pl.pendentes(self.home), 1)

    def test_enfileirar_todos(self):
        self.assertEqual(pl.enfileirar_todos(self.home), 4)
        self.assertEqual(pl.linhas_total(self.home), 4)


class TestEnviador(Cobertura):
    def test_enviador_envia_e_marca(self):
        pl.enfileirar(self.home, [self.k0])
        post = Fake({"ok": True})
        env = self.enviador(post)
        env.ciclo()
        url, body = post.corpos[0]
        self.assertEqual(url, "https://exemplo.test/exec")
        self.assertEqual((body["token"], body["colunas"]), ("tok", pl.COLUNAS))
        self.assertEqual(body["linhas"][0][0], self.k0)
        self.assertEqual(len(body["linhas"][0]), len(pl.COLUNAS) + 1)
        self.assertEqual(pl.pendentes(self.home), 0)
        s = env.status()
        self.assertTrue(s["configurada"])
        self.assertIsNotNone(s["ultimo_ok"])
        self.assertIsNone(s["erro"])

    def test_enviador_lotes_de_100(self):
        with contextlib.closing(pl._con(self.home)) as con, con:
            for i in range(150):
                con.execute("INSERT INTO planilha_outbox VALUES(?,?,?,NULL)", (f"k{i}", "[]", f"2026-01-01#{i:06d}"))
        post = Fake({"ok": True}, {"ok": True})
        env = self.enviador(post)
        self.assertEqual(env.ciclo(), 0)
        self.assertEqual(len(post.corpos[0][1]["linhas"]), 100)
        env.ciclo()
        self.assertEqual(len(post.corpos[1][1]["linhas"]), 50)
        self.assertEqual(pl.pendentes(self.home), 0)

    def test_enviador_erro_mantem_fila_e_mostra_status(self):
        pl.enfileirar(self.home, [self.k0])
        post = Fake(urllib.error.URLError("sem internet"), ValueError("página HTML"),
                    {"ok": False, "erro": "token"}, {"ok": True})
        env = self.enviador(post)
        esperas = [env.ciclo() for _ in range(3)]
        self.assertEqual(esperas, [0.01, 0.02, 0.04])       # dobra até o teto
        s = env.status()
        self.assertEqual(s["pendentes"], 1)
        self.assertIn("token", s["erro"])
        env.ciclo()
        self.assertEqual(env.status()["pendentes"], 0)
        self.assertIsNone(env.status()["erro"])

    def test_enviador_sem_url_nao_envia(self):
        pl.enfileirar(self.home, [self.k0])
        post = Fake()
        env = self.enviador(post, url="")
        env.ciclo()
        self.assertEqual(post.corpos, [])
        s = env.status()
        self.assertFalse(s["configurada"])
        self.assertEqual(s["pendentes"], 1)

    def test_linha_alterada_durante_o_envio_continua_pendente(self):
        pl.enfileirar(self.home, [self.k0])

        def post(url, body):
            pl.enfileirar(self.home, [self.k0])      # o bug mudou enquanto enviava
            return {"ok": True}
        self.enviador(post).ciclo()
        self.assertEqual(pl.pendentes(self.home), 1)

    def test_token_gerado_ao_salvar_a_url(self):
        pl.salvar_url(self.settings, " https://exemplo.test/exec ")
        self.assertEqual(self.settings.get("planilha", "url"), "https://exemplo.test/exec")
        self.assertGreaterEqual(len(self.settings.get("planilha", "token")), 20)

    def test_thread_acorda_e_para(self):
        pl.enfileirar(self.home, [self.k0])
        env = self.enviador(Fake({"ok": True}))
        env._ociosa = 60
        env.start()
        env.acordar()
        self.assertTrue(wait_for(lambda: pl.pendentes(self.home) == 0))
        env.stop()
        env.join(5)
        self.assertFalse(env.is_alive())


class TestHookDoWorker(Cobertura):
    def worker(self, visto):
        w = Worker(self.home, self.settings, owns_lock=False)
        w.on_bug_change = visto.append
        return w

    def job(self, i=0):
        j = self.jobs(self.bid)[i]
        self.sql("UPDATE jobs SET state='running' WHERE id=?", j["id"])
        return j

    def test_hook_do_worker_enfileira(self):
        j = self.job()
        w = self.worker([])
        w.on_bug_change = lambda k: pl.enfileirar(self.home, [k])
        w._finish_job(j, None, "erro", 1.0)
        self.assertIn(j["bug_key"], self.outbox())

    def test_hook_em_descarte_e_devolucao(self):
        visto = []
        w = self.worker(visto)
        j = self.job()
        w._requeue_job(j)
        w._discard_job(j, None, 1.0)
        self.assertEqual(visto, [j["bug_key"]] * 2)

    def test_hook_que_falha_nao_chega_ao_worker(self):
        w = self.worker([])
        w.on_bug_change = lambda k: 1 / 0
        j = self.job()
        w._finish_job(j, None, None, 1.0)      # não levanta
        self.assertIn("ZeroDivisionError", w._last_error)

    def test_hook_ignora_lote_que_nao_e_de_cobertura(self):
        visto = []
        w = self.worker(visto)
        j = self.job()
        with contextlib.closing(jq._connect(self.home)) as con, con:
            con.execute("UPDATE batches SET meta=NULL WHERE id=?", (self.bid,))
        w._finish_job(j, None, None, 1.0)
        self.assertEqual(visto, [])


if __name__ == "__main__":
    unittest.main()
