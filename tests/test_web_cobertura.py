from __future__ import annotations

import contextlib
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bugsjar_fixture import make_bugsjar_git
from web_helpers import HAVE_WEB, WebFixture

from sbfl import cobertura as cb
from sbfl import queue as jq


@unittest.skipUnless(HAVE_WEB, "fastapi/httpx não instalados")
class Base(WebFixture, unittest.TestCase):
    _molde: Path | None = None

    @classmethod
    def _colecao(cls) -> Path:
        if Base._molde is None:
            base = Path(tempfile.mkdtemp())
            unittest.addModuleCleanup(shutil.rmtree, base, True)
            (base / "colecao").mkdir()
            for nome, ids in (("maven", ("A-1", "A-2")), ("outro", ("B-1", "B-2"))):
                repo = make_bugsjar_git(base / ("src_" + nome), ids=ids)
                shutil.move(str(repo), str(base / "colecao" / nome))
            Base._molde = base / "colecao"
        return Base._molde

    def setUp(self):
        super().setUp()
        self.worker.currents.return_value = []
        self.root = self.tmp / "colecao"
        shutil.copytree(self._colecao(), self.root)
        self.c = self.authed()

    def iniciar(self) -> int:
        return cb.sincronizar(self.home, self.settings, self.root)["batch_id"]


class TestCobertura(Base):
    def test_sem_cobertura_mostra_inicio(self):
        self.settings.set("bugsjar", "root", str(self.root))
        r = self.c.get("/cobertura")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Começar cobertura", r.text)
        self.assertIn(str(self.root).replace("&", "&amp;"), r.text.replace("&#39;", "'"))

    def test_iniciar_sincroniza_e_redireciona(self):
        r = self.c.post("/cobertura/iniciar", data={"pasta": str(self.root)}, follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/cobertura"))
        bid = cb.batch_id(self.home)
        self.assertIsNotNone(bid)
        self.assertEqual(len(jq.batch_jobs(self.home, bid)), 12)

    def test_iniciar_pasta_invalida(self):
        r = self.c.post("/cobertura/iniciar", data={"pasta": str(self.tmp / "nada")})
        self.assertEqual(r.status_code, 400)
        self.assertIsNone(cb.batch_id(self.home))

    def test_raiz_vai_para_cobertura_com_pasta_configurada(self):
        self.settings.set("bugsjar", "root", str(self.root))
        r = self.c.get("/", follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/cobertura"))

    def test_painel_mostra_contagens_e_projetos(self):
        self.iniciar()
        html = self.c.get("/cobertura").text
        self.assertIn("0 de 4 bugs concluídos (0%)", html)
        self.assertIn("Rodando", html)
        self.assertIn("Refazer falhados (0)", html)
        self.assertIn("Pausar após o bug atual", html)
        self.assertIn("/cobertura/bugs?projeto=maven", html)
        self.assertIn("/cobertura/bugs?projeto=outro", html)
        self.assertIn("0 de 4 bugs concluídos", self.c.get("/cobertura/painel").text)

    def test_painel_rodando_agora(self):
        self.iniciar()
        job = jq.batch_jobs(self.home, cb.batch_id(self.home))[0]
        self.worker.currents.return_value = [{"job_id": job["id"], "bug": job["bug_key"], "kind": job["kind"],
                                              "started": 0.0, "index": 0}]
        html = self.c.get("/cobertura/painel").text
        self.assertIn("Rodando agora", html)
        self.assertIn("cb-chip-rodando", html)
        self.assertIn("Jaguar 2", html)

    def test_selos(self):
        bid = self.iniciar()
        self.assertIn("cb-selo-rodando", self.c.get("/cobertura/painel").text)
        jq.get_batch(self.home, bid)
        with mock.patch("sbfl.queue.get_batch", return_value={"state": "paused"}):
            self.assertIn(">Pausado<", self.c.get("/cobertura/painel").text)
            self.worker.currents.return_value = [{"job_id": 1, "bug": "x/y/z", "kind": "maven", "started": 0.0}]
            self.assertIn("Pausando…", self.c.get("/cobertura/painel").text)
            self.assertIn("Continuar", self.c.get("/cobertura/painel").text)
        self.worker.status.return_value = {"alive": False, "last_error": None, "current": None}
        self.assertIn(">Parado<", self.c.get("/cobertura/painel").text)

    def test_pausar_continuar_parar_chamam_pool(self):
        bid = self.iniciar()
        for rota in ("pausar", "continuar", "parar"):
            r = self.c.post(f"/cobertura/{rota}", follow_redirects=False)
            self.assertEqual((r.status_code, r.headers["location"]), (303, "/cobertura"))
        self.worker.pause.assert_called_once_with(bid)
        self.worker.resume.assert_called_once_with(bid)
        self.worker.interromper.assert_called_once_with()

    def test_refazer_falhados(self):
        bid = self.iniciar()
        with mock.patch("sbfl.cobertura.refazer", return_value=3) as m:
            r = self.c.post("/cobertura/refazer-falhados", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        m.assert_called_once_with(self.home, bid)

    def test_sincronizar_e_idempotente(self):
        self.settings.set("bugsjar", "root", str(self.root))
        bid = self.iniciar()
        with mock.patch("sbfl.cobertura.sincronizar", wraps=cb.sincronizar) as m:
            r = self.c.post("/cobertura/sincronizar", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        m.assert_called_once()
        self.assertEqual(len(jq.batch_jobs(self.home, bid)), 12)

    def test_sidebar_sem_rodar(self):
        html = self.c.get("/cobertura").text
        nav = html[html.index('<nav class="lateral">'):html.index("</nav>")]
        self.assertEqual(re.findall(r'<a href="(/[^"]*)"', nav), ["/cobertura", "/resultados", "/ambiente"])
        self.assertEqual(self.c.get("/rodar").status_code, 200)

    def test_painel_sem_enviador_nao_mostra_planilha(self):
        self.iniciar()
        self.assertNotIn("Planilha", self.c.get("/cobertura/painel").text)

    def test_painel_planilha_ok_nao_configurada_e_erro(self):
        self.iniciar()
        env = mock.Mock()
        self.app.state.enviador = env
        env.status.return_value = {"configurada": False, "ultimo_ok": None, "pendentes": 0, "erro": None}
        html = self.c.get("/cobertura/painel").text
        self.assertIn("Planilha não configurada", html)
        self.assertIn('href="/ambiente#planilha"', html)
        env.status.return_value = {"configurada": True, "ultimo_ok": "2020-01-01T00:00:00", "pendentes": 0,
                                   "erro": None}
        with mock.patch("sbfl.planilha.linhas_total", return_value=42):
            html = self.c.get("/cobertura/painel").text
        self.assertIn("Planilha Google sincronizada há", html)
        self.assertIn("· 42 linhas", html)

    def test_painel_planilha_erro_em_vermelho(self):
        self.iniciar()
        env = mock.Mock()
        env.status.return_value = {"configurada": True, "ultimo_ok": None, "pendentes": 7, "erro": "HTTP 500"}
        self.app.state.enviador = env
        html = self.c.get("/cobertura/painel").text
        self.assertRegex(html, r'class="cb-planilha erro"')
        self.assertIn("Planilha sem enviar há", html)
        self.assertIn("7 pendentes · HTTP 500", html)


    def test_ritmo_e_faltam_com_virgula(self):
        self.iniciar()
        base = cb.painel(self.home, cb.batch_id(self.home))
        for restante, texto in ((9000, "faltam ≈ 2,5 h"), (0, "faltam ≈ 0,0 h")):
            with mock.patch("sbfl.cobertura.painel", return_value={**base, "ritmo_h": 4.0, "restante_s": restante}):
                html = self.c.get("/cobertura/painel").text
            self.assertIn("ritmo 4,0 bugs/h", html)
            self.assertIn(texto, html)
            self.assertIn("workers", html)

    def test_raiz_redireciona_com_lote_e_sem_pasta(self):
        self.iniciar()
        self.assertEqual(self.settings.get("bugsjar", "root", ""), "")
        r = self.c.get("/", follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/cobertura"))

    def test_selo_concluida_sem_pausar(self):
        self.iniciar()
        with mock.patch("sbfl.queue.get_batch", return_value={"state": "done"}):
            html = self.c.get("/cobertura/painel").text
        self.assertIn(">Concluída<", html)
        self.assertNotIn("Pausar após", html)
        self.assertIn("Refazer falhados", html)

    def test_selo_cancelado_e_outra_instancia(self):
        self.iniciar()
        with mock.patch("sbfl.queue.get_batch", return_value={"state": "cancelled"}):
            self.assertIn(">Parado<", self.c.get("/cobertura/painel").text)
        self.worker.status.return_value = {"alive": True, "other_instance": True}
        html = self.c.get("/cobertura/painel").text
        self.assertIn(">Parado<", html)
        self.assertIn("outra janela do JFLP Studio está rodando a fila", html)

    def test_chips_por_situacao(self):
        bid = self.iniciar()
        jobs = jq.batch_jobs(self.home, bid)
        k0 = jobs[0]["bug_key"]
        mine = {j["kind"]: j for j in jobs if j["bug_key"] == k0}
        with contextlib.closing(jq._connect(self.home)) as con, con:
            con.execute("UPDATE jobs SET state='done' WHERE id=?", (mine["maven"]["id"],))   # sem run: falhou
            con.execute("UPDATE jobs SET state='running' WHERE id=?", (mine["jaguar1"]["id"],))
        self.worker.currents.return_value = [{"job_id": mine["jaguar1"]["id"], "bug": k0, "kind": "jaguar1",
                                              "started": 0.0}]
        html = self.c.get("/cobertura/painel").text
        self.assertIn("cb-chip-falhou", html)
        self.assertIn("cb-chip-rodando", html)
        self.assertIn("cb-chip-pendente", html)

    def test_ultimos_sem_hora_nao_mostra_traco(self):
        bid = self.iniciar()
        k0 = jq.batch_jobs(self.home, bid)[0]["bug_key"]
        with contextlib.closing(jq._connect(self.home)) as con, con:
            con.execute("UPDATE jobs SET state='done' WHERE batch_id=? AND bug_key=?", (bid, k0))
        html = self.c.get("/cobertura/painel").text
        self.assertIn("Últimos concluídos", html)
        self.assertNotIn("há —", html)

    def test_painel_cancelado_sem_pausar_e_concluido_sem_parar(self):
        self.iniciar()
        with mock.patch("sbfl.queue.get_batch", return_value={"state": "cancelled"}):
            html = self.c.get("/cobertura/painel").text
        self.assertNotIn("Pausar após", html)
        self.assertIn("Parar agora", html)
        with mock.patch("sbfl.queue.get_batch", return_value={"state": "done"}):
            html = self.c.get("/cobertura/painel").text
        self.assertNotIn("Parar agora", html)

    def test_ver_do_projeto_abre_a_tabela_filtrada(self):
        self.iniciar()
        html = self.c.get("/cobertura").text
        self.assertIn('href="/cobertura/bugs?projeto=maven"', html)
        r = self.c.get("/cobertura/bugs?projeto=maven")
        self.assertEqual(r.status_code, 200)


class TestTabela(Base):
    def chaves(self, html: str) -> list[str]:
        return re.findall(r'<input type="checkbox" name="keys" value="([^"]+)"', html)

    def test_tabela_filtra_por_situacao_e_projeto(self):
        self.iniciar()
        html = self.c.get("/cobertura/bugs?projeto=maven").text
        self.assertEqual(len(self.chaves(html)), 2)
        self.assertIn("A-1", html)
        self.assertNotIn("B-1", html)
        self.assertEqual(len(self.chaves(self.c.get("/cobertura/bugs?situacao=pendente").text)), 4)
        self.assertEqual(self.chaves(self.c.get("/cobertura/bugs?situacao=ok").text), [])
        self.assertIn("Nenhum bug com esses filtros", self.c.get("/cobertura/bugs?situacao=ok").text)

    def test_tabela_busca(self):
        self.iniciar()
        html = self.c.get("/cobertura/bugs?q=B-2").text
        self.assertEqual(len(self.chaves(html)), 1)
        self.assertIn("B-2", html)
        self.assertEqual(self.chaves(self.c.get("/cobertura/bugs?q=nada-disso").text), [])

    def test_tabela_pagina(self):
        self.iniciar()
        with mock.patch("sbfl.web.routes_cobertura.PAGINA", 3):
            html = self.c.get("/cobertura/bugs?pagina=2").text
        self.assertEqual(len(self.chaves(html)), 1)
        self.assertIn("Página 2 de 2", html)
        self.assertIn("← anterior", html)
        self.assertNotIn("próxima →", html)

    def test_tabela_mostra_colunas_e_filtros_com_contagem(self):
        bid = self.iniciar()
        html = self.c.get("/cobertura/bugs").text
        for titulo in ("Bug", "Situação", "Maven", "Jaguar 1", "Jaguar 2", "J1×J2", "Motivo / perfil"):
            self.assertIn(titulo, html)
        self.assertIn("Pendentes <b>4</b>", html)
        self.assertIn("Não executáveis <b>0</b>", html)

    def test_acao_refazer_selecionados(self):
        bid = self.iniciar()
        jobs = jq.batch_jobs(self.home, bid)
        alvo = jobs[0]["bug_key"]
        with contextlib.closing(jq._connect(self.home)) as con, con:
            con.execute("UPDATE jobs SET state='done' WHERE batch_id=? AND bug_key=?", (bid, alvo))
        r = self.c.post("/cobertura/bugs/acao", data={"acao": "refazer", "keys": [alvo], "projeto": "maven"},
                        follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].startswith("/cobertura/bugs?"))
        self.assertIn("projeto=maven", r.headers["location"])
        estados = {j["state"] for j in jq.batch_jobs(self.home, bid) if j["bug_key"] == alvo}
        self.assertEqual(estados, {"pending"})

    def test_acao_nao_executavel(self):
        bid = self.iniciar()
        alvo = jq.batch_jobs(self.home, bid)[0]["bug_key"]
        r = self.c.post("/cobertura/bugs/acao", data={"acao": "nao_executavel", "keys": [alvo]},
                        follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        jobs = [j for j in jq.batch_jobs(self.home, bid) if j["bug_key"] == alvo]
        self.assertTrue(all(j["forcado"] == "nao_executavel" for j in jobs))
        self.assertTrue(all(j["state"] == "done" for j in jobs))

    def test_acao_priorizar(self):
        bid = self.iniciar()
        r = self.c.post("/cobertura/bugs/acao", data={"acao": "priorizar", "keys": [
            next(j["bug_key"] for j in jq.batch_jobs(self.home, bid) if "B-2" in j["bug_key"])]},
            follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        jobs = jq.batch_jobs(self.home, bid)
        primeiro = min(jobs, key=lambda j: j["idx"])
        self.assertIn("B-2", primeiro["bug_key"])

    def test_acao_desconhecida_recusada(self):
        self.iniciar()
        r = self.c.post("/cobertura/bugs/acao", data={"acao": "apagar", "keys": ["x"]}, follow_redirects=False)
        self.assertEqual(r.status_code, 400)

    def test_detalhe_do_bug(self):
        bid = self.iniciar()
        alvo = jq.batch_jobs(self.home, bid)[0]["bug_key"]
        r = self.c.get("/cobertura/bug", params={"key": alvo})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Maven", r.text)
        self.assertEqual(self.c.get("/cobertura/bug", params={"key": "bugsjar/x/y/z"}).status_code, 404)


class TestAmbiente(Base):
    def setUp(self):
        super().setUp()
        for p in self.no_tools():
            p.start()
            self.addCleanup(p.stop)

    def test_ambiente_salva_planilha_e_gera_token(self):
        html = self.c.get("/ambiente").text
        self.assertIn('id="planilha"', html)
        token = self.settings.get("planilha", "token", "")
        self.assertTrue(token)
        self.assertIn(token, html)
        self.assertIn("copiar", html)
        url = "https://script.google.com/macros/s/abc/exec"
        r = self.c.post("/ambiente/planilha", data={"url": url})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.settings.get("planilha", "url"), url)
        self.assertEqual(self.settings.get("planilha", "token"), token)
        self.assertEqual(self.c.post("/ambiente/planilha", data={"url": "http://ruim"}).status_code, 400)

    def test_ambiente_salva_cobertura(self):
        html = self.c.get("/ambiente").text
        self.assertIn('name="workers"', html)
        r = self.c.post("/ambiente/cobertura", data={"workers": "6", "work_dir": "", "legado": "on"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.settings.get("cobertura", "workers"), 6)
        self.assertTrue(self.settings.get("cobertura", "legado"))
        self.assertFalse(self.settings.get("cobertura", "legado_offline"))
        self.assertEqual(self.c.post("/ambiente/cobertura", data={"workers": "99"}).status_code, 400)
        self.assertEqual(self.c.post("/ambiente/cobertura", data={"workers": "2", "work_dir": "relativo"}).status_code,
                         400)

    def test_ambiente_botoes_legados(self):
        html = self.c.get("/ambiente").text
        self.assertIn("tipo=jdk&v=7", html)
        self.assertIn("tipo=maven&v=3.0.5", html)
        d = mock.Mock(name="x", url="https://x", size=None, algo="sha256", checksum="ab", dest=self.tmp)
        d.name = "JDK 7 (Zulu)"
        with mock.patch("sbfl.web.routes_env.plan_jdk", return_value=d):
            self.assertIn("JDK 7 (Zulu)", self.c.get("/ambiente/plano?tipo=jdk&v=7").text)

    def test_reenviar_tudo(self):
        env = mock.Mock()
        env.status.return_value = {"configurada": True, "ultimo_ok": None, "pendentes": 0, "erro": None,
                                   "falhas_linha": 0, "erro_linha": None}
        self.app.state.enviador = env
        with mock.patch("sbfl.planilha.enfileirar_todos", return_value=7) as m:
            r = self.c.post("/ambiente/planilha/reenviar")
        self.assertEqual(r.status_code, 200)
        m.assert_called_once_with(self.home)
        env.acordar.assert_called_once_with(forcar=True)

    def test_reenviar_tudo_sem_enviador(self):
        with mock.patch("sbfl.planilha.enfileirar_todos", return_value=0) as m:
            r = self.c.post("/ambiente/planilha/reenviar")
        self.assertEqual(r.status_code, 200)
        m.assert_called_once()


if __name__ == "__main__":
    unittest.main()
