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
from sbfl import store
from sbfl.adapters.base import Bug
from sbfl.diffgt import FaultLine

TRUTH = [FaultLine("org.x.Foo", 4, "removed").to_dict()]
FALHA_MAVEN = [{"test": "org.x.FooTest#bar", "message": "esperava 1", "type": "junit.framework.AssertionFailedError",
                "frame": {"cls": "org.x.Foo", "method": "bar", "file": "Foo.java", "line": 4}}]


def _ranking(n: int, defeito_pos: int) -> list[tuple]:
    """n linhas de org.x.Other com suspeição decrescente; o defeito (org.x.Foo:4) na posição `defeito_pos`."""
    rows = [("org.x.Other", i, round(1.0 - i / 1000, 6)) for i in range(1, n + 1)]
    rows[defeito_pos - 1] = ("org.x.Foo", 4, rows[defeito_pos - 1][2])
    return rows


def _grava_run(home, ramo: str, kind: str, status: str = "ok_testes_falhando", outputs=None, **extra) -> str:
    w = store.RunWriter(home, Bug("bugsjar", "maven", ramo, {}), kind)
    for rel, text in (outputs or {}).items():
        w.write_text(rel, text)
    w.finish({"status": status, "started": "2026-01-01T10:00:00", "truth": TRUTH, **extra})
    return w.rel


def _grava_j2(home, ramo: str, rows) -> str:
    txt = "".join(f"{c.replace('.', '/')},{ln},1,0,0,2,{s}\n" for c, ln, s in rows)
    return _grava_run(home, ramo, "jaguar2", outputs={"outputs/jaguar2.csv": txt})


def _grava_j1(home, ramo: str, rows) -> str:
    xml = "<root>" + "".join(
        f'<requirements name="{c}" location="{ln}" suspicious-value="{s}" cef="1" cep="0" cnf="0" cnp="2"/>'
        for c, ln, s in rows) + "</root>"
    return _grava_run(home, ramo, "jaguar1", outputs={"outputs/jaguar1.xml": xml})


def _celulas(html: str, key: str) -> list[str]:
    """Texto normalizado das células (td) da linha cujo checkbox tem o valor `key`."""
    i = html.index(f'value="{key}"')
    linha = html[html.rindex("<tr>", 0, i):html.index("</tr>", i)]
    return [" ".join(re.sub(r"<[^>]+>", " ", c).split()) for c in re.findall(r"<td>(.*?)</td>", linha, re.S)]


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

    def preparar(self):
        """A-1 com Maven (1 falha) e Jaguar 1/2 concluídos; B-1 importado, com o Jaguar 1 caído."""
        bid = self.iniciar()
        jobs = jq.batch_jobs(self.home, bid)
        a1 = next(j["bug_key"] for j in jobs if "A-1" in j["bug_key"])
        b1 = next(j["bug_key"] for j in jobs if "B-1" in j["bug_key"])
        ra1, rb1 = a1.split("/", 2)[2], b1.split("/", 2)[2]
        rel_m = _grava_run(self.home, ra1, "maven", failures=FALHA_MAVEN,
                           tests={"total": 10, "passed": 9, "failed": 1, "error": 0, "skipped": 0})
        rel_1 = _grava_j1(self.home, ra1, _ranking(60, 3))
        rel_2 = _grava_j2(self.home, ra1, _ranking(60, 1))
        rel_b = _grava_run(self.home, rb1, "jaguar1", status="jaguar_crash")
        with contextlib.closing(jq._connect(self.home)) as con, con:
            for kind, rel in (("maven", rel_m), ("jaguar1", rel_1), ("jaguar2", rel_2)):
                con.execute("UPDATE jobs SET state='done', run_rel=? WHERE batch_id=? AND bug_key=? AND kind=?",
                            (rel, bid, a1, kind))
            con.execute("UPDATE jobs SET state='done', run_rel=? WHERE batch_id=? AND bug_key=? AND kind=?",
                        (rel_b, bid, b1, "jaguar1"))
            con.execute("UPDATE jobs SET origem='importado' WHERE batch_id=? AND bug_key=?", (bid, b1))
        return bid, a1, b1

    def test_tabela_celulas_exatas(self):
        _, a1, b1 = self.preparar()
        html = self.c.get("/cobertura/bugs").text
        self.assertEqual(_celulas(html, a1)[1:8],
                         [a1.split("/", 2)[2] + " maven", "Concluído", "10 · 1 falhas", "pos. 3", "pos. 1",
                          "9/10", ""])
        self.assertEqual(_celulas(html, b1)[1:8],
                         [b1.split("/", 2)[2] + " outro", "Pendente", "Pendente", "Falhou", "na fila", "—",
                          "Jaguar 1: jaguar_crash importado"])
        self.assertIn("importado", _celulas(html, b1)[7])

    def test_detalhe_do_bug(self):
        _, a1, _ = self.preparar()
        r = self.c.get("/cobertura/bug", params={"key": a1})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Maven", r.text)
        self.assertIn("FooTest", r.text)          # teste que falhou (run do Maven)
        self.assertIn("Foo.java:4", r.text)       # linha do erro guardada na execução
        self.assertIn("Jaguar 2", r.text)
        self.assertEqual(self.c.get("/cobertura/bug", params={"key": "bugsjar/x/y/z"}).status_code, 404)

    def test_acao_sem_selecao_avisa(self):
        self.iniciar()
        r = self.c.post("/cobertura/bugs/acao", data={"acao": "refazer", "projeto": "maven"},
                        follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("aviso=nenhum", r.headers["location"])
        self.assertIn("Nenhum bug marcado", self.c.get("/cobertura/bugs?aviso=nenhum").text)


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
        r = self.c.post("/ambiente/planilha", data={"url": url}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].startswith("/ambiente?"))
        self.assertTrue(r.headers["location"].endswith("#planilha"))
        self.assertEqual(self.settings.get("planilha", "url"), url)
        self.assertEqual(self.settings.get("planilha", "token"), token)
        self.assertIn("URL da planilha salva.", self.c.get(r.headers["location"]).text)

    def test_ambiente_url_invalida_mantem_o_digitado(self):
        r = self.c.post("/ambiente/planilha", data={"url": "http://ruim"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("precisa começar com https://", r.text)
        self.assertIn('value="http://ruim"', r.text)

    def test_ambiente_salva_cobertura(self):
        html = self.c.get("/ambiente").text
        self.assertIn('name="workers"', html)
        r = self.c.post("/ambiente/cobertura", data={"workers": "6", "work_dir": "", "legado": "on"},
                        follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].endswith("#cobertura"))
        self.assertEqual(self.settings.get("cobertura", "workers"), 6)
        self.assertTrue(self.settings.get("cobertura", "legado"))
        self.assertFalse(self.settings.get("cobertura", "legado_offline"))
        self.assertIn("valem depois de reiniciar", self.c.get(r.headers["location"]).text)

    def test_cobertura_erros_mantem_valores_digitados(self):
        r = self.c.post("/ambiente/cobertura", data={"workers": "99", "work_dir": "E:/x", "legado": "on"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Os workers vão de 1 a 12", r.text)
        self.assertIn('value="99"', r.text)
        self.assertIn('value="E:/x"', r.text)
        self.assertNotEqual(self.settings.get("cobertura", "workers"), 99)

    def test_cobertura_workers_nao_numerico_da_erro_amigavel(self):
        r = self.c.post("/ambiente/cobertura", data={"workers": "abc", "work_dir": ""})
        self.assertEqual(r.status_code, 400)
        self.assertIn("digite um número inteiro", r.text)
        self.assertNotIn('"detail"', r.text)      # nada de JSON 422
        r = self.c.post("/ambiente/cobertura", data={"workers": "2", "work_dir": "relativo"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("caminho absoluto", r.text)

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
            r = self.c.post("/ambiente/planilha/reenviar", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        m.assert_called_once_with(self.home)
        env.acordar.assert_called_once_with(forcar=True)
        self.assertIn("7 linhas voltaram", self.c.get(r.headers["location"]).text)

    def test_reenviar_tudo_sem_enviador(self):
        with mock.patch("sbfl.planilha.enfileirar_todos", return_value=0) as m:
            r = self.c.post("/ambiente/planilha/reenviar", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        m.assert_called_once()


if __name__ == "__main__":
    unittest.main()
