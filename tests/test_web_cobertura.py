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


if __name__ == "__main__":
    unittest.main()
