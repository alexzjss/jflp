from __future__ import annotations

import time
import unittest
from pathlib import Path
from unittest import mock

from sbfl.toolchain.detect import Jdk, Maven
from sbfl.toolchain.download import Download, DownloadError
from sbfl.web import routes_assistente as ra
from tests.web_helpers import HAVE_WEB, WebFixture

R = "sbfl.web.routes_assistente"


def jdk(major):
    return Jdk(Path(f"/jdk{major}"), major, f"{major}.0.1", "managed")


def mvn(v):
    return Maven(Path(f"/mvn{v}"), tuple(int(n) for n in v.split(".")), "managed")


@unittest.skipUnless(HAVE_WEB, "fastapi/httpx não instalados")
class Assistente(WebFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.jdks, self.mavens = [], []
        patches = [
            mock.patch(f"{R}.find_jdks", side_effect=lambda h, s: list(self.jdks)),
            mock.patch(f"{R}.find_mavens", side_effect=lambda h, s: list(self.mavens)),
            mock.patch("sbfl.toolchain.profiles.find_jdks", side_effect=lambda h, s: list(self.jdks)),
            mock.patch("sbfl.toolchain.profiles.find_mavens", side_effect=lambda h, s: list(self.mavens)),
            mock.patch(f"{R}._git", return_value=None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.c = self.authed()

    def dismiss(self, value):
        ra.gravar_estado_ui(self.home, assistente_dispensado=value)

    def fake_plans(self, home, tipo, v):
        return [Download(f"{tipo} {v}", f"https://x/{tipo}-{v}", 1048576, "abc", "sha256", True,
                         home.tools / f"{tipo}-{v}")]

    def wait_done(self):
        tid = next(iter(self.app.state.tasks))
        for _ in range(200):
            if self.app.state.tasks[tid]["done"]:
                return self.app.state.tasks[tid]
            time.sleep(0.02)
        self.fail("tarefa não terminou")

    # ---- checklist ----
    def test_checklist_reflete_perfis_faltando(self):
        r = self.c.get("/bem-vindo")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Falta", r.text)
        self.assertIn("JDK 11 não encontrado", r.text)
        self.assertIn("https://git-scm.com/downloads", r.text)
        self.assertNotIn("Pronto", r.text)

    def test_checklist_reflete_perfis_presentes(self):
        self.jdks, self.mavens = [jdk(11), jdk(8)], [mvn("3.9.9"), mvn("3.6.3")]
        with mock.patch(f"{R}._git", return_value="git version 2.45"):
            r = self.c.get("/bem-vindo")
        self.assertIn("JDK 11, Maven 3.9.9", r.text)
        self.assertIn("git version 2.45", r.text)
        self.assertNotIn("git-scm.com", r.text)
        # só a lib do Jaguar 1 e as dependências do Jaguar 2 seguem faltando
        self.assertIn("falta a biblioteca (lib) do Jaguar 1", r.text)
        self.assertIn("ainda não baixadas", r.text)

    # ---- redirecionamento do painel ----
    def test_painel_redireciona_quando_falta_e_nao_dispensado(self):
        self.dismiss(False)
        r = self.c.get("/", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/bem-vindo")

    def test_painel_nao_redireciona_se_dispensado(self):
        self.dismiss(True)
        self.assertEqual(self.c.get("/", follow_redirects=False).status_code, 200)

    def test_painel_nao_redireciona_com_perfil_ok(self):
        self.dismiss(False)
        self.jdks, self.mavens = [jdk(11)], [mvn("3.9.9")]
        self.assertEqual(self.c.get("/", follow_redirects=False).status_code, 200)

    def test_dispensar_grava_e_volta_ao_painel(self):
        self.dismiss(False)
        r = self.c.post("/bem-vindo/dispensar", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(ra.assistente_dispensado(self.home))
        self.assertEqual(self.c.get("/", follow_redirects=False).status_code, 200)

    # ---- estado-ui.json ----
    def test_estado_ui_corrompido_vale_padrao(self):
        path = self.home.root / "estado-ui.json"
        for lixo in ("{não é json", "[1, 2]", '"texto"', ""):
            path.write_text(lixo, encoding="utf-8")
            self.assertEqual(ra.ler_estado_ui(self.home), {})
            self.assertFalse(ra.assistente_dispensado(self.home))
        self.assertEqual(self.c.get("/", follow_redirects=False).status_code, 303)
        ra.gravar_estado_ui(self.home, assistente_dispensado=True)   # regrava por cima do lixo
        self.assertTrue(ra.assistente_dispensado(self.home))

    def test_estado_ui_ausente(self):
        (self.home.root / "estado-ui.json").unlink()
        self.assertFalse(ra.assistente_dispensado(self.home))

    # ---- plano ----
    def test_plano_lista_so_o_que_falta_e_respeita_escopo(self):
        self.jdks = [jdk(11)]
        with mock.patch(f"{R}._plans", side_effect=self.fake_plans) as pl:
            ess = self.c.get("/bem-vindo/plano?escopo=essencial").text
            tudo = self.c.get("/bem-vindo/plano?escopo=tudo").text
        self.assertRegex(ess, r"JDK 11</strong>\s*&mdash; <span class=\"ok\">já instalado")
        self.assertIn("https://x/maven-3.9.9", ess)
        self.assertNotIn("jdk-8", ess)
        self.assertNotIn("jaguar1", ess)
        self.assertIn("Dependências do Jaguar 2", ess)
        for chave in ("jdk-8", "maven-3.6.3", "jaguar1-"):
            self.assertIn(f"https://x/{chave}", tudo)
        self.assertNotIn("https://x/jdk-11", tudo)
        self.assertIn("Tamanho: 1.0 MB", tudo)
        self.assertIn('name="escopo" value="tudo"', tudo)
        self.assertNotIn(("jdk", "11"), [c.args[1:] for c in pl.call_args_list])

    def test_plano_com_erro_de_rede_num_item(self):
        def plans(home, tipo, v):
            if tipo == "jdk":
                raise DownloadError("sem rede")
            return self.fake_plans(home, tipo, v)
        with mock.patch(f"{R}._plans", side_effect=plans):
            t = self.c.get("/bem-vindo/plano?escopo=essencial").text
        self.assertIn("sem rede", t)
        self.assertIn("https://x/maven-3.9.9", t)

    # ---- preparar ----
    def test_preparar_executa_em_ordem_pula_presentes_e_segue_apos_falha(self):
        self.jdks = [jdk(8)]            # JDK 8 já existe
        chamadas = []

        def fetch(d, progress=None):
            chamadas.append(d.name)
            progress and progress(512, 1024)
            if d.name == "maven 3.9.9":
                raise DownloadError("checksum diferente")

        with mock.patch(f"{R}._plans", side_effect=self.fake_plans), \
                mock.patch(f"{R}.fetch", side_effect=fetch), \
                mock.patch(f"{R}.prefetch_jaguar2", return_value=True) as pf:
            r = self.c.post("/bem-vindo/preparar", data={"escopo": "tudo"})
            self.assertEqual(r.status_code, 200)
            task = self.wait_done()
        self.assertEqual(chamadas, ["jdk 11", "maven 3.9.9", "maven 3.6.3", "jaguar1 "])
        self.assertEqual([s["state"] for s in task["steps"]],
                         ["ok", "failed", "skipped", "ok", "ok", "ok"])
        self.assertEqual(pf.call_count, 1)
        self.assertEqual(task["failed"], 1)
        page = self.c.get(f"/bem-vindo/progresso/{next(iter(self.app.state.tasks))}").text
        self.assertIn("falhou: checksum diferente", page)
        self.assertIn("1 falha", page)

    def test_preparar_sucesso_marca_dispensado(self):
        self.dismiss(False)
        with mock.patch(f"{R}._plans", side_effect=self.fake_plans), \
                mock.patch(f"{R}.fetch"), \
                mock.patch(f"{R}.prefetch_jaguar2", return_value=True):
            self.c.post("/bem-vindo/preparar", data={"escopo": "essencial"})
            task = self.wait_done()
        self.assertEqual(task["failed"], 0)
        self.assertEqual([s["id"] for s in task["steps"]], ["jdk11", "maven399", "prefetch"])
        self.assertTrue(ra.assistente_dispensado(self.home))

    def test_preparar_com_falha_nao_dispensa(self):
        self.dismiss(False)
        with mock.patch(f"{R}._plans", side_effect=DownloadError("sem rede")), \
                mock.patch(f"{R}.fetch"), \
                mock.patch(f"{R}.prefetch_jaguar2", side_effect=lambda h, s: ra.resolve("jaguar2", h, s)):
            self.c.post("/bem-vindo/preparar", data={"escopo": "essencial"})
            task = self.wait_done()
        self.assertEqual([s["state"] for s in task["steps"]], ["failed", "failed", "failed"])
        self.assertIn("sem rede", task["steps"][0]["msg"])
        self.assertIn("JDK 11 não encontrado", task["steps"][2]["msg"])
        self.assertFalse(ra.assistente_dispensado(self.home))

    def test_segunda_preparacao_simultanea_nao_inicia_outra(self):
        import threading
        gate = threading.Event()
        with mock.patch(f"{R}._plans", side_effect=self.fake_plans), \
                mock.patch(f"{R}.fetch", side_effect=lambda d, progress=None: gate.wait(5)), \
                mock.patch(f"{R}.prefetch_jaguar2", return_value=True) as pf:
            r1 = self.c.post("/bem-vindo/preparar", data={"escopo": "essencial"})
            r2 = self.c.post("/bem-vindo/preparar", data={"escopo": "tudo"})
            self.assertEqual(len(self.app.state.tasks), 1)
            tid = next(iter(self.app.state.tasks))
            self.assertIn(f"/bem-vindo/progresso/{tid}", r2.text)
            self.assertIn("em andamento", r1.text)
            self.assertIn(f"/bem-vindo/progresso/{tid}", self.c.get("/bem-vindo").text)
            gate.set()
            task = self.wait_done()
        self.assertEqual(len(task["steps"]), 3)       # o escopo da primeira prevaleceu
        self.assertEqual(pf.call_count, 1)

    def test_progresso_inexistente(self):
        self.assertEqual(self.c.get("/bem-vindo/progresso/nada").status_code, 404)

    def test_ambiente_tem_link_do_assistente(self):
        for p in self.no_tools():
            p.start()
            self.addCleanup(p.stop)
        self.assertIn('href="/bem-vindo"', self.c.get("/ambiente").text)


if __name__ == "__main__":
    unittest.main()
