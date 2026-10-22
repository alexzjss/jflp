from __future__ import annotations

import re
import shutil
import tempfile
import unittest
from pathlib import Path

from sbfl import bugview as bv
from sbfl import queue as jq
from sbfl import store
from tests.bugsjar_fixture import make_bugsjar_git, ramo_de
from tests.test_bugview import FOO, Base, _git, _rm, linhas
from tests.web_helpers import HAVE_WEB, WebFixture

HX = {"HX-Request": "true"}
_REPO: dict = {}


def repo_web() -> Path:
    """Repositório git falso (como o de test_bugview) só para estes testes, criado uma vez."""
    if "p" not in _REPO:
        base = Path(tempfile.mkdtemp(prefix="jflp_lvrepo_"))
        _REPO["base"] = base
        repo = make_bugsjar_git(base, ids=("MNG-1", "MNG-2"))
        for i in ("MNG-1", "MNG-2"):
            _git(repo, "checkout", "-q", ramo_de(i))
            (repo / "maven-core/src/main/java/org/x/Foo.java").write_text(FOO, encoding="utf-8")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", "fonte")
        _git(repo, "checkout", "-q", "master")
        _REPO["p"] = repo
    return _REPO["p"]


def tearDownModule():
    if "base" in _REPO:
        shutil.rmtree(_REPO["base"], onerror=_rm)


J2 = [("org.x.Foo", 4, 0.9), ("org.x.Other", 1, 0.8), ("org.x.Other", 2, 0.7), ("org.x.Zed", 9, 0.6)] + \
     [("org.x.P", i, 0.5 - i / 1000) for i in range(1, 70)]
J1 = [("org.x.Other", 1, 0.95), ("org.x.Other", 2, 0.9), ("org.x.Foo", 4, 0.85), ("org.x.Q", 5, 0.5)] + \
     [("org.x.R", i, 0.4 - i / 1000) for i in range(1, 70)]


@unittest.skipUnless(HAVE_WEB, "fastapi/httpx não instalados")
class VistaDoLote(WebFixture, Base):
    def setUp(self):
        if shutil.which("git") is None:
            self.skipTest("git não instalado")
        WebFixture.setUp(self)
        self.repo = repo_web()
        bv.limpar_cache()
        self.c = self.authed()

    def completo(self):
        """Lote com MNG-1 terminado (Maven + J1 + J2) e MNG-2 ainda na fila."""
        bid = self.lote(["MNG-1", "MNG-2"], ["maven", "jaguar1", "jaguar2"])
        self.finish(bid, "MNG-1", "maven", self.maven("MNG-1", ["org.x.FooTest#testA", "org.x.ExtraTest#t"]))
        self.finish(bid, "MNG-1", "jaguar2", self.j2("MNG-1", J2))
        self.finish(bid, "MNG-1", "jaguar1", self.j1("MNG-1", J1))
        return bid

    def test_pagina_do_lote_tem_tabela_cabecalho_e_acoes(self):
        bid = self.completo()
        r = self.c.get(f"/lote/{bid}")
        self.assertEqual(r.status_code, 200)
        for t in ("MNG-1", "MNG-2", "Maven", "Jaguar 1", "Jaguar 2", "Gabarito", "J2 pos. 1 · J1 pos. 3", "2 falhas", "pos. 1", "pos. 3", "Baixar CSV", "Cancelar lote", "na fila"):
            self.assertIn(t, r.text)
        self.assertIn(f"/lote/{bid}/resultado.csv", r.text)
        self.assertIn(f'hx-post="/lote/{bid}/parar"', r.text)
        self.assertIn('aria-expanded="false"', r.text)
        self.assertIn(f"/lote/{bid}/bug?key=", r.text)
        self.assertRegex(r.text, r'class="vd vd-achou"[^>]*>✓')
        self.assertRegex(r.text, r'vd-achou" title="achou"')            # contagem no cabeçalho
        self.assertEqual(self.c.get("/lote/999").status_code, 404)

    def test_celulas_em_andamento_usam_o_mini_e_a_tabela_se_atualiza_por_assinatura(self):
        bid = self.completo()
        self.finish(bid, "MNG-2", "jaguar2", state="running")
        r = self.c.get(f"/lote/{bid}")
        running = [j for j in jq.batch_jobs(self.home, bid) if j["state"] == "running"][0]
        self.assertIn(f'hx-get="/job/{running["id"]}/mini"', r.text)
        self.assertIn("load, every 1s", r.text)
        sig = re.search(r'"sig": "([0-9a-f]+)"', r.text).group(1)
        self.assertIn('hx-trigger="every 2s"', r.text)
        self.assertEqual(self.c.get(f"/lote/{bid}/tabela", params={"sig": sig}).status_code, 204)
        full = self.c.get(f"/lote/{bid}/tabela")
        self.assertEqual(full.status_code, 200)
        self.assertNotIn("<html", full.text)
        self.finish(bid, "MNG-2", "jaguar2", state="cancelled")
        self.assertEqual(self.c.get(f"/lote/{bid}/tabela", params={"sig": sig}).status_code, 200)
        self.assertEqual(self.c.get("/lote/999/tabela").status_code, 404)

    def test_lote_terminado_nao_atualiza_nem_oferece_cancelar(self):
        bid = self.lote(["MNG-1"], ["jaguar2"])
        self.finish(bid, "MNG-1", "jaguar2", self.j2("MNG-1", linhas(30, 1)))
        r = self.c.get(f"/lote/{bid}")
        self.assertNotIn("every 2s", r.text)
        self.assertNotIn("Cancelar lote", r.text)
        self.assertNotIn("<th scope=\"col\">Maven</th>", r.text)       # só as ferramentas usadas

    def test_resumos_chegam_a_parte_fora_de_banda(self):
        bid = self.completo()
        page = self.c.get(f"/lote/{bid}")
        self.assertIn(f'hx-get="/lote/{bid}/resumos"', page.text)
        self.assertNotIn("resumo do MNG-1", page.text)                # o git não é lido na vista que se atualiza
        r = self.c.get(f"/lote/{bid}/resumos")
        from sbfl.web.routes_lote import uid
        self.assertIn(f'<span id="res-{uid(self.bug("MNG-1").key)}" hx-swap-oob="innerHTML">resumo do MNG-1</span>', r.text)
        self.assertIn(f'id="res-{uid(self.bug("MNG-1").key)}"', page.text)

    def test_detalhe_do_bug(self):
        bid = self.completo()
        r = self.c.get(f"/lote/{bid}/bug", params={"key": self.bug("MNG-1").key})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("<html", r.text)
        t = r.text
        # Maven
        self.assertIn("2 testes falharam", t)
        self.assertIn("FooTest#testA", t)
        self.assertIn("junit.framework.AssertionFailedError", t)
        self.assertIn("esperava 1", t)
        self.assertIn("org.x.Foo.bar (Foo.java:4)", t)
        self.assertIn("Falhou o esperado?", t)
        self.assertIn("a mais: ExtraTest; faltando: BoomTest", t)
        # Jaguar: estrela no defeito, destaque nos comuns, ver mais, link para o código
        self.assertIn("★", t)
        self.assertRegex(t, r'<tr class="defeito comum"')
        self.assertRegex(t, r'<tr class="comum"')
        self.assertIn("data-mais", t)
        self.assertIn("ver mais (40)", t)
        self.assertEqual(len(re.findall(r'class="[^"]*lv-extra[^"]*" hidden', t)), 80)
        self.assertRegex(t, r'href="/run/[^"]+/codigo\?classe=org\.x\.Foo#L4"')
        self.assertIn("Detalhes da execução", t)
        # concordância
        self.assertIn("Concordância Jaguar 1 × Jaguar 2", t)
        self.assertIn("<strong>3 de 10</strong>", t)
        self.assertIn("Jaguar 2 achou antes", t)
        # catálogo
        self.assertIn("resumo do MNG-1", t)
        self.assertIn('href="https://issues.apache.org/jira/browse/MNG-1" target="_blank" rel="noopener"', t)
        self.assertIn("<code>Foo.bar</code>", t)
        self.assertIn("Trecho do patch do desenvolvedor", t)
        self.assertIn('<span class="menos">-        // TODO velho</span>', t)
        self.assertIn('<span class="mais">+        }</span>', t)
        self.assertIn("BoomTest", t)
        self.assertEqual(self.c.get(f"/lote/{bid}/bug", params={"key": "x/y/z"}).status_code, 404)
        self.assertEqual(self.c.get("/lote/999/bug", params={"key": "x"}).status_code, 404)

    def test_detalhe_de_execucao_com_falha_mostra_causa_e_link(self):
        bid = self.lote(["MNG-1"], ["jaguar2", "maven"])
        rel = self.grava("MNG-1", "jaguar2", status="jaguar_sem_saida", diagnosis={"cause": "o plugin não gravou nada"})
        self.finish(bid, "MNG-1", "jaguar2", rel)
        r = self.c.get(f"/lote/{bid}/bug", params={"key": self.bug("MNG-1").key})
        self.assertIn("o Jaguar não gerou resultado", r.text)
        self.assertIn("o plugin não gravou nada", r.text)
        self.assertIn(f'href="/run/{rel}"', r.text)
        self.assertNotIn("Concordância", r.text)
        self.assertIn("na fila", r.text)        # o Maven ainda não rodou

    def test_csv_para_baixar(self):
        bid = self.completo()
        r = self.c.get(f"/lote/{bid}/resultado.csv")
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers["content-disposition"])
        self.assertIn(f"lote-{bid}.csv", r.headers["content-disposition"])
        self.assertTrue(r.headers["content-type"].startswith("text/csv"))
        self.assertTrue(r.text.startswith("projeto,bug,ramo,resumo"))
        self.assertIn("maven,MNG-1", r.text)
        self.assertEqual(self.c.get("/lote/999/resultado.csv").status_code, 404)

    def test_cancelar_lote(self):
        bid = self.completo()
        self.finish(bid, "MNG-2", "maven", state="running")
        r = self.c.post(f"/lote/{bid}/parar", headers=HX)
        self.assertEqual(r.status_code, 200)
        self.assertIn('id="lote-vista"', r.text)
        self.worker.cancel.assert_called_once_with(bid)
        rodando = [j["id"] for j in jq.batch_jobs(self.home, bid) if j["state"] == "running"]
        self.worker.cancel_job.assert_called_once_with(rodando[0])
        r = self.c.post(f"/lote/{bid}/parar", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.c.post("/lote/999/parar").status_code, 404)

    def test_codigo_sem_checkout_le_o_fonte_do_ramo(self):
        bid = self.lote(["MNG-1"], ["jaguar2"])
        rel = self.j2("MNG-1", J2)
        self.finish(bid, "MNG-1", "jaguar2", rel)
        r = self.c.get(f"/run/{rel}/codigo", params={"classe": "org.x.Foo"})        # via o job que gravou a execução
        self.assertEqual(r.status_code, 200)
        self.assertIn('id="L4"', r.text)
        self.assertIn("// TODO velho", r.text)
        self.assertIn("maven-core/src/main/java/org/x/Foo.java", r.text)
        self.assertNotIn("não guardou o checkout", r.text)
        # a classe com mais suspeita é aberta mesmo sem o parâmetro
        self.assertIn("class Foo", self.c.get(f"/run/{rel}/codigo").text)
        # via o bug cadastrado na sessão (sem job)
        r2 = self.j2("MNG-2", J2)
        self.app.state.local_bugs[self.bug("MNG-2").key] = self.bug("MNG-2")
        self.assertIn("// TODO velho", self.c.get(f"/run/{r2}/codigo", params={"classe": "org.x.Foo"}).text)

    def test_codigo_de_execucao_maven_mostra_o_fonte_sem_cores(self):
        bid = self.lote(["MNG-1"], ["maven"])
        rel = self.maven("MNG-1", ["org.x.FooTest#a"])
        self.finish(bid, "MNG-1", "maven", rel)
        r = self.c.get(f"/run/{rel}/codigo", params={"classe": "org.x.Foo"})
        self.assertIn('id="L4"', r.text)
        self.assertNotIn("com-score", r.text)
        self.assertNotIn("codigo-classes", r.text)
        sem = self.c.get(f"/run/{rel}/codigo")
        self.assertIn("não tem pontuações", sem.text)

    def test_codigo_sem_checkout_e_sem_repositorio_continua_avisando(self):
        rel = self.j2("MNG-1", J2)            # nenhum job nem bug cadastrado: o repositório é desconhecido
        r = self.c.get(f"/run/{rel}/codigo", params={"classe": "org.x.Foo"})
        self.assertIn("não guardou o checkout", r.text)

    def test_resultados_lista_lotes_com_vereditos(self):
        bid = self.completo()
        vazio = jq.create_batch(self.home, "so local", [store_bug()], ["maven"], {})
        r = self.c.get("/resultados")
        self.assertEqual(r.status_code, 200)
        t = r.text
        self.assertNotIn("Execuções recentes", t)
        self.assertNotIn("Em andamento", t)
        self.assertIn(f'href="/lote/{bid}"', t)
        self.assertIn("Maven, Jaguar 1, Jaguar 2", t)
        self.assertRegex(t, r'<td class="num">2</td>')                 # bugs no lote
        self.assertRegex(t, r'vd vd-achou" aria-hidden="true">✓</span> 1')
        self.assertRegex(t, r'vd vd-sem_dado" aria-hidden="true">—</span> 1')
        self.assertIn("rodando", t)
        self.assertIn("so local", t)
        self.assertLess(t.index(f"#{vazio} "), t.index(f"#{bid} "))   # mais recente primeiro
        # lote encerrado: botão de excluir; em andamento: sem botão
        self.assertNotIn(f'hx-post="/lote/{bid}/excluir"', t)
        import contextlib
        with contextlib.closing(jq._connect(self.home)) as con, con:
            con.execute("UPDATE jobs SET state='done' WHERE batch_id=?", (vazio,))
            con.execute("UPDATE batches SET state='done' WHERE id=?", (vazio,))
        t = self.c.get("/resultados").text
        self.assertIn(f'hx-post="/lote/{vazio}/excluir"', t)
        self.assertIn("terminado", t)


def store_bug():
    from sbfl.adapters.base import Bug
    return Bug("local", "proj", "dir-1", {"path": "/x"})


if __name__ == "__main__":
    unittest.main()
