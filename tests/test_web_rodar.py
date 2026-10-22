from __future__ import annotations

import contextlib
import json
import re
import shutil
import unittest

from sbfl import queue as jq
from sbfl import store
from sbfl.adapters.base import Bug
from tests.bugsjar_fixture import make_bugsjar_git, ramo_de
from tests.web_helpers import HAVE_WEB, WebFixture

HX = {"HX-Request": "true"}


@unittest.skipUnless(HAVE_WEB, "fastapi/httpx não instalados")
class Rodar(WebFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        if shutil.which("git") is None:
            self.skipTest("git não instalado")
        self.raiz = self.tmp / "bugs-dot-jar"
        self.raiz.mkdir()
        self.repo = make_bugsjar_git(self.raiz, ("MNG-1", "MNG-2", "MNG-3"), atual="MNG-2")
        outra = make_bugsjar_git(self.tmp / "outra", ("LANG-7", "LANG-8"))
        self.repo2 = self.raiz / "commons-lang"
        shutil.move(str(outra), str(self.repo2))
        self.c = self.authed()

    @staticmethod
    def marcados(html: str) -> list[str]:
        tags = re.findall(r'<input type="checkbox" class="rd-sel".*?>', html, re.S)
        return [t for t in tags if "checked" in t]

    @staticmethod
    def valor(repo, id_):
        return f"{repo}|{ramo_de(id_)}"

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

    def test_colecao_vira_secoes_recolhiveis_com_bugs_sob_demanda(self):
        r = self.c.get("/rodar/pasta", params={"projeto": str(self.raiz)})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Projetos e bugs", r.text)
        self.assertEqual(r.text.count('<details class="rd-proj"'), 2)
        self.assertIn('hx-trigger="toggle once"', r.text)
        self.assertIn("/rodar/bugs?repo=", r.text)
        self.assertIn("/rodar/contagem", r.text)
        self.assertIn("salva como raiz", r.text)
        self.assertNotIn("MNG-1", r.text)             # a lista de bugs só vem quando a seção é aberta
        self.assertEqual(self.settings.get("bugsjar", "root"), str(self.raiz.resolve()))
        self.assertEqual(self.c.get("/rodar/contagem", params={"repo": str(self.repo)}).text, "3 bugs")

    def test_bugs_do_projeto_com_id_status_e_selecao_do_ramo_atual(self):
        store.RunWriter(self.home, Bug("bugsjar", "maven", ramo_de("MNG-1")), "maven").finish(
            {"status": "ok_testes_falhando"})
        store.RunWriter(self.home, Bug("bugsjar", "maven", ramo_de("MNG-3")), "maven").finish(
            {"status": "compilacao_falhou"})
        r = self.c.get("/rodar/bugs", params={"repo": str(self.repo), "pi": 4})
        self.assertEqual(r.status_code, 200)
        for i in ("MNG-1", "MNG-2", "MNG-3"):
            self.assertIn(i, r.text)
        marcado = self.marcados(r.text)
        self.assertEqual(len(marcado), 1)
        self.assertIn(f'value="{self.repo}|{ramo_de("MNG-2")}"', marcado[0])
        self.assertIn('data-proj="4"', marcado[0])
        self.assertEqual(re.findall(r'data-st="(\w+)"', r.text), ["ok", "nunca", "falha"])
        self.assertIn("/rodar/resumos?repo=", r.text)
        self.assertEqual(self.c.get("/rodar/bugs", params={"repo": str(self.tmp)}).status_code, 404)

    def test_resumos_chegam_fora_de_banda_e_ficam_em_cache(self):
        r = self.c.get("/rodar/resumos", params={"repo": str(self.repo), "pi": 2})
        spans = re.findall(r'<span id="rs-2-(\d)" hx-swap-oob="innerHTML">(.*?)</span>', r.text)
        self.assertEqual(spans, [("0", "resumo do MNG-1"), ("1", "resumo do MNG-2"), ("2", "resumo do MNG-3")])
        self.assertEqual(len(self.app.state.resumos), 3)
        self.assertEqual(self.c.get("/rodar/resumos", params={"repo": "nada"}).text, "")

    def test_subpasta_do_repositorio_vai_direto_aos_bugs_com_padroes(self):
        r = self.c.get("/rodar/pasta", params={"projeto": str(self.repo / "maven-core")})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Bugs de maven", r.text)
        marcado = self.marcados(r.text)
        self.assertEqual(len(marcado), 1)
        self.assertIn(ramo_de("MNG-2"), marcado[0])
        self.assertNotIn("Nenhum repositório", r.text)
        tags = {v: t for t, v in re.findall(r'(<input type="checkbox" name="kinds" value="(\w+)".*?>)', r.text)}
        self.assertIn("checked", tags["maven"])
        self.assertIn("checked", tags["jaguar2"])
        self.assertNotIn("checked", tags["jaguar1"])
        self.assertRegex(r.text, r'value="relevant" checked')
        self.assertIn('<details class="rd-avancado">', r.text)
        self.assertRegex(r.text, r'name="auto_search" value="1" checked')
        for t in ("rd-filtro", "rd-status", "Nunca rodados", "Com falha", "Marcar visíveis", "bugs em"):
            self.assertIn(t, r.text)

    def test_postar_bugs_de_dois_projetos_cria_um_lote_e_fica_na_pagina(self):
        r = self.c.post("/rodar", headers=HX, data={
            "modo": "bugsjar", "pasta": str(self.raiz),
            "sel": [self.valor(self.repo, "MNG-1"), self.valor(self.repo2, "LANG-7"),
                    self.valor(self.repo2, "LANG-8")],
            "kinds": ["jaguar2", "maven"], "auto_search": "1", "escopo": "relevant"})
        self.assertEqual(r.status_code, 200)
        bid = int(r.headers["HX-Push-Url"].rsplit("=", 1)[1])
        self.assertEqual(r.headers["HX-Push-Url"], f"/rodar?lote={bid}")
        self.assertIn('id="lote-vista"', r.text)
        self.assertIn('hx-swap-oob="true"', r.text)
        self.assertIn("3 bugs em 2 projetos", r.text)
        self.assertIn("Maven, Jaguar 2", r.text)
        self.assertIn("alterar", r.text)
        self.assertNotIn("<html", r.text)
        jobs = jq.batch_jobs(self.home, bid)
        self.assertEqual(len(jobs), 6)
        self.assertEqual({j["kind"] for j in jobs}, {"jaguar2", "maven"})
        self.assertEqual({j["bug_key"] for j in jobs},
                         {f"bugsjar/maven/{ramo_de('MNG-1')}", f"bugsjar/commons-lang/{ramo_de('LANG-7')}",
                          f"bugsjar/commons-lang/{ramo_de('LANG-8')}"})
        self.assertEqual(set(self.app.state.local_bugs), {j["bug_key"] for j in jobs})
        self.assertEqual(jq.get_batch(self.home, bid)["total"], 6)

    def test_post_comum_redireciona_para_a_pagina_do_lote(self):
        r = self.c.post("/rodar", data={"modo": "bugsjar", "pasta": str(self.raiz), "repo": str(self.repo),
                                        "sel": [ramo_de("MNG-1")], "kinds": ["maven"]}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertRegex(r.headers["location"], r"^/rodar\?lote=\d+$")
        bid = int(r.headers["location"].rsplit("=", 1)[1])      # só o ramo, com `repo` à parte (formato simples)
        self.assertEqual([j["bug_key"] for j in jq.batch_jobs(self.home, bid)],
                         [f"bugsjar/maven/{ramo_de('MNG-1')}"])
        self.assertEqual(jq.get_batch(self.home, bid)["name"], "bugs.jar maven: 1 bug(s)")

    def test_escopo_e_busca_automatica_vao_para_as_opcoes_do_lote(self):
        r = self.c.post("/rodar", data={"modo": "bugsjar", "sel": [self.valor(self.repo, "MNG-1")],
                                        "kinds": ["maven"], "escopo": "all"}, follow_redirects=False)
        bid = int(r.headers["location"].rsplit("=", 1)[1])
        with contextlib.closing(jq._connect(self.home)) as con:
            opts = con.execute("SELECT options FROM batches WHERE id=?", (bid,)).fetchone()[0]
        self.assertEqual(json.loads(opts), {"test_scope": "all", "auto_search": False, "archive_checkout": False})

    def test_pagina_do_lote_restaura_resumo_e_vista(self):
        r = self.c.post("/rodar", data={
            "modo": "bugsjar", "pasta": str(self.raiz),
            "sel": [self.valor(self.repo, "MNG-1"), self.valor(self.repo, "MNG-3")], "kinds": ["maven", "jaguar1"]},
            follow_redirects=False)
        bid = int(r.headers["location"].rsplit("=", 1)[1])
        self.settings.set("bugsjar", "root", str(self.raiz))
        r = self.c.get("/rodar", params={"lote": bid})
        self.assertEqual(r.status_code, 200)
        self.assertIn("2 bugs", r.text)
        self.assertIn("Maven, Jaguar 1", r.text)
        self.assertIn('id="lote-vista"', r.text)
        self.assertIn('class="rd-alterar"', r.text)
        self.assertRegex(r.text, r'<div id="rd-form" hidden>')
        self.assertIn('hx-trigger="carregar"', r.text)
        r = self.c.get("/rodar", params={"lote": 999})
        self.assertEqual(r.status_code, 404)
        self.assertIn("Não existe o lote 999", r.text)

    def test_sem_bug_ou_sem_ferramenta_e_erro(self):
        base = {"modo": "bugsjar", "pasta": str(self.raiz)}
        r = self.c.post("/rodar", data={**base, "kinds": ["maven"]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("pelo menos um bug", r.text)
        r = self.c.post("/rodar", data={**base, "sel": [self.valor(self.repo, "MNG-1")]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("pelo menos uma ferramenta", r.text)
        r = self.c.post("/rodar", headers=HX, data={**base, "kinds": ["maven"]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.headers["HX-Retarget"], "#rd-erro")
        self.assertIn("pelo menos um bug", r.text)
        self.assertNotIn("<html", r.text)
        r = self.c.post("/rodar", headers=HX, data={**base, "kinds": ["maven"], "sel": [f"{self.tmp}|x"]})
        self.assertIn("não é um repositório", r.text)

    def test_projeto_maven_simples(self):
        proj = self.tmp / "meu-maven"
        (proj / "mod-a").mkdir(parents=True)
        (proj / "pom.xml").write_text("<project/>")
        (proj / "mod-a" / "pom.xml").write_text("<project/>")
        r = self.c.get("/rodar/pasta", params={"projeto": str(proj)})
        self.assertEqual(r.status_code, 200)
        self.assertIn('data-modo="maven"', r.text)
        self.assertIn('<option value="mod-a">', r.text)
        r = self.c.post("/rodar", data={"modo": "maven", "pasta": str(proj), "module": "", "kinds": ["maven"]},
                        follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertRegex(r.headers["location"], r"^/rodar\?lote=\d+$")
        bid = int(r.headers["location"].rsplit("=", 1)[1])
        jobs = jq.batch_jobs(self.home, bid)
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0]["bug_key"].startswith("local/meu-maven/"))
        r = self.c.post("/rodar", headers=HX, data={"modo": "maven", "pasta": str(proj), "kinds": ["maven"]})
        self.assertIn("Projeto Maven meu-maven", r.text)
        self.assertIn('id="lote-vista"', r.text)

    def test_pasta_invalida(self):
        r = self.c.get("/rodar/pasta", params={"projeto": str(self.tmp / "nao-existe")})
        self.assertIn("não existe", r.text)
        vazia = self.tmp / "vazia"
        vazia.mkdir()
        r = self.c.get("/rodar/pasta", params={"projeto": str(vazia)})
        self.assertIn("Não encontrei", r.text)


if __name__ == "__main__":
    unittest.main()
