from __future__ import annotations

import contextlib
import json
import queue as _q
import re
import unittest
from unittest import mock

from sbfl import queue as jq
from sbfl.adapters.base import Bug
from sbfl.progress import Progress
from tests.web_helpers import HAVE_WEB, WebFixture

LINHAS = ["# jflp tentativa 1", "[INFO] --- maven-surefire-plugin:2.14.1:test (default-test) @ a ---",
          "[INFO] Tests run: 5, Failures: 1, Errors: 0, Skipped: 0, Time elapsed: 0.1 s <<< FAILURE! - in a.BTest",
          "[ERROR] Failed to execute goal X"]


def sse_events(text: str) -> list[tuple[str, str]]:
    """[(evento, dados)] de um corpo text/event-stream ('message' para linhas sem evento)."""
    out = []
    for bloco in text.split("\n\n"):
        if not bloco.strip() or bloco.startswith(":"):
            continue
        ev, dados = "message", []
        for ln in bloco.split("\n"):
            if ln.startswith("event: "):
                ev = ln[7:]
            elif ln.startswith("data: "):
                dados.append(ln[6:])
        out.append((ev, "\n".join(dados)))
    return out


@unittest.skipUnless(HAVE_WEB, "fastapi/httpx não instalados")
class ProgressoWeb(WebFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.worker = jq.Worker(self.home, self.settings)     # sem iniciar a thread: só o banco
        self.app.state.worker = self.worker
        self.c = self.authed()

    def batch(self, kinds=("jaguar2",), n=1):
        return jq.create_batch(self.home, "lote", [Bug("local", "p", str(i), {"dir": f"/x/{i}"}) for i in range(n)],
                               list(kinds), {})

    def sql(self, q, *a):
        with contextlib.closing(jq._connect(self.home)) as con, con:
            con.execute(q, a)

    def start(self, jid, kind="jaguar2", lines=LINHAS):
        """Põe o job como running e o progresso no worker, como o _run_job faz."""
        self.sql("UPDATE jobs SET state='running' WHERE id=?", jid)
        prog = Progress(kind)
        for ln in lines:
            prog.feed(ln)
        with self.worker._lock:
            self.worker._current = {"job_id": jid, "batch_id": 1, "bug": "local/p/0", "kind": kind,
                                    "started": 1.0}
            self.worker._progress = prog

    # ---- página ao vivo
    def test_pagina_ao_vivo_estrutura(self):
        self.batch(("jaguar2",))
        html = self.c.get("/job/1/ao-vivo").text
        for chave in ("preparar", "compilar", "testes", "analise", "gravar"):
            self.assertIn(f'data-passo="{chave}"', html)
        for ident in ("av-passos", "t-rod", "c-down", "c-mod", "av-chips", "av-erro-txt", "av-resumo", "av-cancelar"):
            self.assertIn(f'id="{ident}"', html)
        self.assertIn("addEventListener(\"estado\"", html)
        self.assertIn('hx-post="/job/1/cancelar"', html)
        self.assertIn("hx-confirm", html)
        self.assertIn("<details", html)
        self.assertNotIn("<details open", html)                    # log fechado por padrão
        self.assertRegex(html, r'id="av-ocultar"[^>]*checked')     # ocultar downloads marcado

    def test_maven_puro_nao_tem_etapa_de_ranking(self):
        self.batch(("maven",))
        html = self.c.get("/job/1/ao-vivo").text
        self.assertNotIn('data-passo="analise"', html)

    def test_job_inexistente(self):
        self.assertEqual(self.c.get("/job/99/ao-vivo").status_code, 404)

    # ---- SSE
    def test_stream_envia_estado_e_fim(self):
        self.batch()
        self.start(1)
        sub = _q.Queue()
        sub.put("linha nova")
        sub.put(None)
        real_get = sub.get

        def get(block=True, timeout=None):
            item = real_get(block, timeout)
            if item is None:      # como o worker: grava o resultado e só então publica o sentinela
                self.sql("UPDATE jobs SET state='done', run_rel='local/p/0/x' WHERE id=1")
            return item

        sub.get = get
        with mock.patch.object(self.worker, "subscribe", return_value=sub):
            r = self.c.get("/job/1/stream")
        ev = sse_events(r.text)
        nomes = [e for e, _ in ev]
        self.assertIn("estado", nomes)
        self.assertEqual(nomes[-1], "fim")
        self.assertEqual(ev[-1][1], "local/p/0/x")
        self.assertLess(nomes.index("message"), len(nomes))
        estado = json.loads([d for e, d in ev if e == "estado"][-1])
        self.assertEqual(estado["testes"]["rodados"], 5)
        self.assertEqual(estado["testes"]["falhas"], 1)
        self.assertEqual(estado["fase"], "rodando testes")
        self.assertIn("Failed to execute goal", estado["ultimo_erro"])
        self.assertIn("estimativa", estado)
        self.assertEqual(estado["passos"], ["preparar", "compilar", "testes", "analise", "gravar"])
        # o último estado vem antes do `fim`
        self.assertLess(max(i for i, (e, _) in enumerate(ev) if e == "estado"), len(ev) - 1)

    def test_stream_estado_usa_estimativa_da_ultima_execucao(self):
        self.batch()
        self.start(1)
        with mock.patch("sbfl.web.routes_bugs.store.list_runs",
                        return_value=[{"n_tests": 41, "seconds": 90.0}]):
            self.sql("UPDATE jobs SET state='done', run_rel='r' WHERE id=1")   # termina já: só o estado final
            sub = _q.Queue()
            sub.put(None)
            with mock.patch.object(self.worker, "subscribe", return_value=sub):
                self.sql("UPDATE jobs SET state='running' WHERE id=1")
                real = sub.get

                def get(block=True, timeout=None):
                    item = real(block, timeout)
                    self.sql("UPDATE jobs SET state='done', run_rel='r' WHERE id=1")
                    return item
                sub.get = get
                r = self.c.get("/job/1/stream")
        est = json.loads([d for e, d in sse_events(r.text) if e == "estado"][-1])["estimativa"]
        self.assertEqual(est, {"testes": 41, "segundos": 90.0})

    def test_stream_job_pronto_continua_sem_estado(self):
        self.batch()
        self.sql("UPDATE jobs SET state='done', run_rel='r/p/1/z' WHERE id=1")
        self.assertEqual(self.c.get("/job/1/stream").text, "event: fim\ndata: r/p/1/z\n\n")

    # ---- resumo do fim
    def test_resumo_de_job_cancelado_e_sem_pasta(self):
        self.batch()
        self.sql("UPDATE jobs SET state='cancelled' WHERE id=1")
        t = self.c.get("/job/1/resumo").text
        self.assertIn("Cancelado", t)
        self.assertIn('data-estado="cancelled"', t)
        self.sql("UPDATE jobs SET state='done', error='boom' WHERE id=1")
        self.assertIn("boom", self.c.get("/job/1/resumo").text)
        self.assertEqual(self.c.get("/job/99/resumo").status_code, 404)

    def test_resumo_com_execucao(self):
        self.batch()
        run = {"status": "ok_testes_falhando", "seconds": 93.4, "attempts": 2, "kind": "jaguar2",
               "tests": {"total": 41, "failed": 0, "error": 1}}
        self.sql("UPDATE jobs SET state='done', run_rel='local/p/0/x' WHERE id=1")
        with mock.patch("sbfl.web.routes_bugs.store.load_run", return_value=run):
            t = self.c.get("/job/1/resumo").text
        self.assertIn("ok_testes_falhando", t)
        self.assertIn('data-ok="1"', t)
        self.assertIn("<strong>41</strong> testes", t)
        self.assertIn("<strong>1</strong> com falha", t)
        self.assertIn("93 s", t)
        self.assertIn('href="/run/local/p/0/x"', t)
        self.assertIn('href="/run/local/p/0/x/codigo"', t)

    # ---- andamento resumido
    def test_mini_do_job_rodando_mostra_etapa_testes_e_tempo(self):
        self.batch()
        self.start(1)
        t = self.c.get("/job/1/mini").text
        self.assertIn("Tentativa 1 · rodando testes · 5 testes, 1 falha ·", t)
        self.assertIn("barra-fina", t)
        self.assertNotIn("<button", t)

    def test_mini_pendente_e_inexistente(self):
        self.batch()
        self.assertIn("Na fila", self.c.get("/job/1/mini").text)
        self.assertEqual(self.c.get("/job/99/mini").status_code, 404)

    def test_mini_sem_progresso_no_worker(self):
        self.batch()
        self.sql("UPDATE jobs SET state='running' WHERE id=1")
        self.assertIn("Em andamento", self.c.get("/job/1/mini").text)

    # ---- achado 9: a troca periódica não recria os botões
    def test_cartoes_204_quando_nada_mudou_e_miolo_sem_botoes(self):
        bid = self.batch(("maven", "jaguar2"))
        self.start(1, "maven")
        page = self.c.get(f"/executar/{bid}/cartoes")
        sig = re.search(r'"sig": "([0-9a-f]+)"', page.text).group(1)
        self.assertIn(f'/job/1/mini', page.text)
        r = self.c.get(f"/executar/{bid}/cartoes", params={"sig": sig})
        self.assertEqual(r.status_code, 204)
        self.sql("UPDATE jobs SET state='done', run_rel=NULL WHERE id=1")
        r = self.c.get(f"/executar/{bid}/cartoes", params={"sig": sig})      # mudou: troca tudo
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("<button", self.c.get("/job/1/mini").text)

    def test_fila_estado_204_com_mesma_assinatura(self):
        bid = self.batch(("maven",), n=2)
        self.start(1, "maven")
        page = self.c.get("/fila")
        sig = re.search(r'"sig": "([0-9a-f]+)"', page.text).group(1)
        self.assertIn("/job/1/mini", page.text)
        self.assertEqual(self.c.get("/fila/estado", params={"sig": sig}).status_code, 204)
        self.assertEqual(self.c.get("/fila/estado").status_code, 200)
        self.sql("UPDATE jobs SET state='cancelled' WHERE id=2")
        self.assertEqual(self.c.get("/fila/estado", params={"sig": sig}).status_code, 200)

    def test_lote_painel_204_com_mesma_assinatura(self):
        bid = self.batch(("maven",), n=2)
        self.start(1, "maven")
        full = self.c.get(f"/lote/{bid}/painel")
        self.assertEqual(full.status_code, 200)
        self.assertIn("Cancelar job", full.text)
        sig = re.search(r'"sig": "([0-9a-f]+)"', full.text).group(1)
        self.assertEqual(self.c.get(f"/lote/{bid}/painel", params={"sig": sig}).status_code, 204)
        self.sql("UPDATE jobs SET state='done' WHERE id=1")
        self.assertEqual(self.c.get(f"/lote/{bid}/painel", params={"sig": sig}).status_code, 200)
        self.assertEqual(self.c.get("/lote/99/painel").status_code, 404)
        self.assertIn("lote-vista", self.c.get(f"/lote/{bid}").text)


if __name__ == "__main__":
    unittest.main()
