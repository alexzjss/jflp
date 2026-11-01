from __future__ import annotations

import contextlib
import json
import shutil
import sqlite3
import tempfile
import time
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from unittest import mock

from sbfl import cobertura as cb
from sbfl import planilha as pl
from sbfl import queue as jq
from sbfl.home import Home, load_settings
from sbfl.queue import Worker
from tests import test_bugview as tbv
from tests.bugsjar_fixture import make_bugsjar_git


def wait_for(cond, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


_MOLDE: dict = {}


def tearDownModule():
    if "base" in _MOLDE:
        shutil.rmtree(_MOLDE["base"], onerror=tbv._rm)
    tbv.tearDownModule()


def molde() -> Path:
    """Coleção com 2 projetos x 2 bugs, criada uma vez (git é lento) e copiada a cada teste."""
    if "p" not in _MOLDE:
        base = Path(tempfile.mkdtemp(prefix="jflp_pl_"))
        _MOLDE["base"] = base
        (base / "colecao").mkdir()
        for nome, ids in (("maven", ("A-1", "A-2")), ("outro", ("B-1", "B-2"))):
            repo = make_bugsjar_git(base / ("src_" + nome), ids=ids)
            shutil.move(str(repo), str(base / "colecao" / nome))
        _MOLDE["p"] = base / "colecao"
    return _MOLDE["p"]


class Relogio:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


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


class Cobertura(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = Home(self.tmp / "home").ensure()
        self.settings = load_settings(self.home)
        self.root = self.tmp / "colecao"
        shutil.copytree(molde(), self.root)
        self.bid = cb.sincronizar(self.home, self.settings, self.root)["batch_id"]
        self.k0 = self.jobs()[0]["bug_key"]

    def jobs(self):
        return jq.batch_jobs(self.home, self.bid)

    def sql(self, q, *a):
        with contextlib.closing(jq._connect(self.home)) as con, con:
            return con.execute(q, a)

    def concluir(self, key, status="ok", cfgs=None):
        from sbfl.store import _connect as sc
        for j in self.jobs():
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

    def enviador(self, post, url="https://exemplo.test/exec", **kw):
        self.settings.set("planilha", "url", url)
        self.settings.set("planilha", "token", "tok")
        for k, v in (("reconciliar_cada", None), ("espera_inicial", 0.01), ("espera_maxima", 0.04),
                     ("espera_ociosa", 0.01)):
            kw.setdefault(k, v)
        return pl.Enviador(self.home, self.settings, post=post, **kw)


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

    def test_maven_que_nao_rodou_nao_compara_com_o_catalogo(self):
        self.concluir(self.k0, "compilacao_falhou")
        bug = next(b for b in pl._bugs_do_lote(self.home, [self.k0]))
        d = dict(zip(pl.COLUNAS, pl.linha(self.home, bug)))
        self.assertEqual(d["Bateu com o catálogo?"], "")
        self.assertEqual(d["Situação"], "falhou")


class TestLinhaCompleta(tbv.Base):
    def test_colunas_de_uma_execucao_concluida(self):
        bid = self.lote(["MNG-1"], ["maven", "jaguar1", "jaguar2"])
        self.sql_meta(bid)
        self.finish(bid, "MNG-1", "maven", self.maven("MNG-1", ["org.x.FooTest#a", "org.x.BoomTest#b"]))
        self.finish(bid, "MNG-1", "jaguar2", self.j2("MNG-1", tbv.linhas(60, 1)))
        self.finish(bid, "MNG-1", "jaguar1", self.j1("MNG-1", tbv.linhas(60, 3)))
        key = self.bug("MNG-1").key
        bug = pl._bugs_do_lote(self.home, [key])[0]
        d = dict(zip(pl.COLUNAS, pl.linha(self.home, bug)))
        self.assertEqual((d["Projeto"], d["Bug"], d["Resumo"]), ("maven", "MNG-1", "resumo do MNG-1"))
        self.assertEqual(d["Link JIRA"], "https://issues.apache.org/jira/browse/MNG-1")
        self.assertEqual((d["Maven testes"], d["Maven falhas"], d["Maven erros"]), (10, 2, 0))
        self.assertIn("org.x.FooTest#a", d["Testes que falharam"])
        self.assertEqual(d["Bateu com o catálogo?"], "sim")
        self.assertEqual((d["J1 posição do patch"], d["J2 posição do patch"]), ("3", "1"))
        self.assertEqual(d["J2 topo"], "Foo:4")
        self.assertEqual((d["Veredito J1"], d["Veredito J2"]), ("✓ achou", "✓ achou"))
        self.assertIn("em comum", d["Concordância J1×J2"])
        self.assertEqual(d["Situação"], "OK")

    def sql_meta(self, bid):
        with contextlib.closing(jq._connect(self.home)) as con, con:
            con.execute("UPDATE batches SET meta=? WHERE id=?", ('{"cobertura": true}', bid))


class TestOutbox(Cobertura):
    def test_enfileirar_upsert(self):
        pl.enfileirar(self.home, [self.k0])
        pl.enfileirar(self.home, [self.k0, self.k0])
        self.assertEqual(list(self.outbox()), [self.k0])
        self.assertEqual(pl.pendentes(self.home), 1)

    def test_enfileirar_todos(self):
        self.assertEqual(pl.enfileirar_todos(self.home), 4)
        self.assertEqual(pl.linhas_total(self.home), 4)

    def test_tabela_antiga_ganha_a_coluna_assinatura(self):
        with contextlib.closing(jq._connect(self.home)) as con, con:
            con.execute("CREATE TABLE planilha_outbox(bug_key TEXT PRIMARY KEY, linha TEXT, atualizado TEXT, "
                        "enviado TEXT)")
            con.execute("INSERT INTO planilha_outbox VALUES('x','[]','a',NULL)")
        pl.enfileirar(self.home, [self.k0])
        self.assertEqual(set(self.outbox()), {"x", self.k0})
        self.assertIsNotNone(self.outbox()[self.k0]["assinatura"])

    def test_montar_linhas_nao_segura_transacao_de_escrita(self):
        real = pl.linha
        visto = []

        def espia(home, bug, resumo=None):
            outra = sqlite3.connect(str(home.db_path), timeout=0.2)
            try:
                outra.execute("BEGIN IMMEDIATE")      # falharia (database is locked) se houvesse escrita aberta
                outra.execute("ROLLBACK")
                visto.append(True)
            finally:
                outra.close()
            return real(home, bug, resumo=resumo)
        with mock.patch.object(pl, "linha", espia):
            pl.enfileirar_todos(self.home)
        self.assertEqual(len(visto), 4)

    def test_gravacao_concorrente_remonta_a_linha(self):
        pl.enfileirar(self.home, [self.k0])
        real, chamadas = pl.linha, []

        def espia(home, bug, resumo=None):
            chamadas.append(1)
            if len(chamadas) == 1:      # outro escritor mexe na linha enquanto esta é montada
                self.sql("UPDATE planilha_outbox SET atualizado='outro' WHERE bug_key=?", self.k0)
            return real(home, bug, resumo=resumo)
        with mock.patch.object(pl, "linha", espia):
            pl.enfileirar(self.home, [self.k0])
        self.assertEqual(len(chamadas), 2)
        self.assertNotEqual(self.outbox()[self.k0]["atualizado"], "outro")

    def test_leitura_da_caixa_vem_antes_do_retrato_dos_bugs(self):
        ordem = []
        real_ler, real_bugs = pl._ler, pl._bugs_do_lote
        with mock.patch.object(pl, "_ler", lambda *a: (ordem.append("ler"), real_ler(*a))[1]),                 mock.patch.object(pl, "_bugs_do_lote", lambda *a: (ordem.append("bugs"), real_bugs(*a))[1]):
            pl.enfileirar(self.home, [self.k0])
            pl.reconciliar(self.home)
            pl.enfileirar_todos(self.home)
        self.assertEqual(ordem, ["ler", "bugs"] * 3)

    def test_falha_ao_montar_linha_e_contada_e_mostrada(self):
        with mock.patch.object(pl, "linha", side_effect=ValueError("quebrou")):
            pl.enfileirar(self.home, [self.k0])
        self.assertEqual(self.outbox(), {})
        s = self.enviador(Fake()).status()
        self.assertEqual(s["falhas_linha"], 1)
        self.assertIn("quebrou", s["erro_linha"])
        pl.enfileirar(self.home, [self.k0])      # voltou a funcionar: a falha some
        self.assertEqual(self.enviador(Fake()).status()["falhas_linha"], 0)


class TestReconciliar(Cobertura):
    def test_carga_inicial_e_so_o_que_mudou(self):
        self.assertEqual(pl.reconciliar(self.home), 4)      # todos, os não rodados como pendente
        self.assertEqual(pl.reconciliar(self.home), 0)
        self.concluir(self.k0, "ok")                        # mudança fora dos workers
        self.assertEqual(pl.reconciliar(self.home), 1)
        self.assertEqual(json.loads(self.outbox()[self.k0]["linha"])[4], "OK")
        self.assertEqual(pl.reconciliar(self.home), 0)

    def test_enviador_reconcilia_ao_iniciar_e_periodicamente(self):
        post = Fake()
        rel = Relogio()
        env = self.enviador(post, reconciliar_cada=600, relogio=rel)
        env.ciclo()
        self.assertEqual(len(post.corpos[0][1]["linhas"]), 4)
        self.assertEqual(pl.pendentes(self.home), 0)
        self.concluir(self.k0, "ok")
        env.ciclo()                                  # ainda dentro dos 10 min
        self.assertEqual(len(post.corpos), 1)
        rel.t += 601
        env.ciclo()
        self.assertEqual([l[0] for l in post.corpos[1][1]["linhas"]], [self.k0])

    def test_sem_url_nao_reconcilia(self):
        env = self.enviador(Fake(), url="", reconciliar_cada=600)
        env.ciclo()
        self.assertEqual(self.outbox(), {})


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
                con.execute("INSERT INTO planilha_outbox(bug_key, linha, atualizado) VALUES(?,?,?)",
                            (f"k{i}", "[]", f"2026-01-01#{i:06d}"))
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
        rel = Relogio()
        env = self.enviador(post, relogio=rel)
        esperas = []
        for _ in range(3):
            esperas.append(env.ciclo())
            rel.t += esperas[-1]
        self.assertEqual(esperas, [0.01, 0.02, 0.04])       # dobra até o teto
        s = env.status()
        self.assertEqual(s["pendentes"], 1)
        self.assertIn("token", s["erro"])
        env.ciclo()
        self.assertEqual(env.status()["pendentes"], 0)
        self.assertIsNone(env.status()["erro"])

    def test_acordar_nao_fura_a_espera_do_erro(self):
        pl.enfileirar(self.home, [self.k0])
        post = Fake(urllib.error.URLError("sem internet"), {"ok": True})
        rel = Relogio()
        env = self.enviador(post, relogio=rel, espera_inicial=100)
        self.assertEqual(env.ciclo(), 100)
        env.acordar()
        self.assertEqual(env.ciclo(), 100)                  # ainda dentro da espera: não reenviou
        self.assertEqual(len(post.corpos), 1)
        env.acordar(forcar=True)                            # "Reenviar tudo"
        env.ciclo()
        self.assertEqual(len(post.corpos), 2)
        self.assertEqual(pl.pendentes(self.home), 0)

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

    def test_cabecalho_mudou_reenvia_tudo(self):
        pl.enfileirar(self.home, [self.k0])
        post = Fake({"ok": True, "cabecalho_mudou": True}, {"ok": True})
        env = self.enviador(post, espera_ociosa=100)
        self.assertEqual(env.ciclo(), 0)
        self.assertEqual(pl.pendentes(self.home), 4)        # tudo voltou para a fila
        env.ciclo()                                         # reenvio imediato, sem esperar
        self.assertEqual(len(post.corpos[1][1]["linhas"]), 4)
        self.assertEqual(pl.pendentes(self.home), 0)

    def test_token_existe_sem_url(self):
        self.assertEqual(self.settings.get("planilha", "url"), "")
        t = pl.garantir_token(self.settings)
        self.assertGreaterEqual(len(t), 20)
        self.assertEqual(pl.garantir_token(self.settings), t)       # estável
        self.assertEqual(load_settings(self.home).get("planilha", "token"), t)      # gravado no config.toml
        pl.salvar_url(self.settings, "https://exemplo.test/exec")
        self.assertEqual(self.settings.get("planilha", "token"), t)
        self.assertEqual(load_settings(self.home).get("planilha", "url"), "https://exemplo.test/exec")

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
        j = self.jobs()[i]
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
        self.sql("UPDATE batches SET meta=NULL WHERE id=?", self.bid)
        w._finish_job(j, None, None, 1.0)
        self.assertEqual(visto, [])


if __name__ == "__main__":
    unittest.main()
