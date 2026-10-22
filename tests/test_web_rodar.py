from __future__ import annotations

import re
import shutil
import unittest

from sbfl import queue as jq
from tests.bugsjar_fixture import make_bugsjar_git, ramo_de
from tests.web_helpers import HAVE_WEB, WebFixture


@unittest.skipUnless(HAVE_WEB, "fastapi/httpx não instalados")
class Rodar(WebFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        if shutil.which("git") is None:
            self.skipTest("git não instalado")
        self.raiz = self.tmp / "bugs-dot-jar"
        self.raiz.mkdir()
        self.repo = make_bugsjar_git(self.raiz, ("MNG-1", "MNG-2", "MNG-3"), atual="MNG-2")
        self.c = self.authed()

    @staticmethod
    def marcados(html: str) -> list[str]:
        tags = re.findall(r'<input type="checkbox" class="rd-sel".*?>', html, re.S)
        return [t for t in tags if "checked" in t]

    def test_inicio_e_pagina(self):
        r = self.c.get("/")
        self.assertEqual(r.status_code, 200)
        for t in ("Rodar bugs", "Pasta", "Escolher pasta"):
            self.assertIn(t, r.text)
        self.assertEqual(self.c.get("/rodar").status_code, 200)

    def test_pasta_da_config_vem_preenchida_e_carrega(self):
        self.settings.set("bugsjar", "root", str(self.raiz))
        r = self.c.get("/rodar")
        self.assertIn('hx-trigger="load"', r.text)
        self.assertIn(str(self.raiz).replace("\\", "\\"), r.text.replace("%5C", "\\").replace("%3A", ":"))

    def test_colecao_lista_projetos_e_salva_a_raiz(self):
        r = self.c.get("/rodar/pasta", params={"projeto": str(self.raiz)})
        self.assertEqual(r.status_code, 200)
        self.assertIn("maven", r.text)
        self.assertIn("/rodar/contagem", r.text)
        self.assertIn("salva como raiz", r.text)
        self.assertEqual(self.settings.get("bugsjar", "root"), str(self.raiz.resolve()))
        self.assertEqual(self.c.get("/rodar/contagem", params={"repo": str(self.repo)}).text, "3 bugs")

    def test_projeto_lista_bugs_e_pre_seleciona_o_atual(self):
        r = self.c.get("/rodar/projeto", params={"repo": str(self.repo), "raiz": str(self.raiz)})
        self.assertEqual(r.status_code, 200)
        for i in ("MNG-1", "MNG-2", "MNG-3"):
            self.assertIn(i, r.text)
        self.assertIn('hx-swap-oob="true"', r.text)
        self.assertIn("Todos (3)", r.text)
        marcado = self.marcados(r.text)
        self.assertEqual(len(marcado), 1)
        self.assertIn(ramo_de("MNG-2"), marcado[0])

    def test_subpasta_do_repositorio_vai_direto_aos_bugs(self):
        r = self.c.get("/rodar/pasta", params={"projeto": str(self.repo / "maven-core")})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Bugs de maven", r.text)
        marcado = self.marcados(r.text)
        self.assertEqual(len(marcado), 1)
        self.assertIn(ramo_de("MNG-2"), marcado[0])
        self.assertNotIn("Nenhum repositório", r.text)

    def test_resumo_lazy_do_bug(self):
        r = self.c.get("/executar/bugsjar-resumo", params={"repo": str(self.repo), "ramo": ramo_de("MNG-1")})
        self.assertIn("resumo do MNG-1", r.text)

    def test_postar_dois_bugs_e_dois_tipos_cria_um_lote(self):
        r = self.c.post("/rodar", data={"modo": "bugsjar", "pasta": str(self.raiz), "repo": str(self.repo),
                                        "sel": [ramo_de("MNG-1"), ramo_de("MNG-3")],
                                        "kinds": ["jaguar1", "maven"], "auto_search": "1"},
                        follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].startswith("/lote/"))
        bid = int(r.headers["location"].rsplit("/", 1)[1])
        jobs = jq.batch_jobs(self.home, bid)
        self.assertEqual(len(jobs), 4)
        self.assertEqual({j["kind"] for j in jobs}, {"jaguar1", "maven"})
        self.assertEqual({j["bug_key"] for j in jobs},
                         {f"bugsjar/maven/{ramo_de('MNG-1')}", f"bugsjar/maven/{ramo_de('MNG-3')}"})

    def test_um_bug_vai_para_a_tela_de_execucao(self):
        r = self.c.post("/rodar", data={"modo": "bugsjar", "repo": str(self.repo), "sel": [ramo_de("MNG-1")],
                                        "kinds": ["maven"]}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].startswith("/executar/"))

    def test_sem_bug_ou_sem_ferramenta_e_erro(self):
        base = {"modo": "bugsjar", "pasta": str(self.raiz), "repo": str(self.repo)}
        r = self.c.post("/rodar", data={**base, "kinds": ["maven"]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("pelo menos um bug", r.text)
        r = self.c.post("/rodar", data={**base, "sel": [ramo_de("MNG-1")]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("pelo menos uma ferramenta", r.text)

    def test_projeto_maven_simples(self):
        proj = self.tmp / "meu-maven"
        (proj / "mod-a").mkdir(parents=True)
        (proj / "pom.xml").write_text("<project/>")
        (proj / "mod-a" / "pom.xml").write_text("<project/>")
        r = self.c.get("/rodar/pasta", params={"projeto": str(proj)})
        self.assertEqual(r.status_code, 200)
        self.assertIn("projeto Maven", r.text)
        self.assertIn('<option value="mod-a">', r.text)
        r = self.c.post("/rodar", data={"modo": "maven", "pasta": str(proj), "module": "", "kinds": ["maven"]},
                        follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].startswith("/executar/"))
        bid = int(r.headers["location"].rsplit("/", 1)[1])
        jobs = jq.batch_jobs(self.home, bid)
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0]["bug_key"].startswith("local/meu-maven/"))

    def test_pasta_invalida(self):
        r = self.c.get("/rodar/pasta", params={"projeto": str(self.tmp / "nao-existe")})
        self.assertIn("não existe", r.text)
        vazia = self.tmp / "vazia"
        vazia.mkdir()
        r = self.c.get("/rodar/pasta", params={"projeto": str(vazia)})
        self.assertIn("Não encontrei", r.text)


if __name__ == "__main__":
    unittest.main()
