from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

from sbfl import queue as jq
from sbfl.adapters.base import Bug
from tests.test_execute import Base as ExecBase
from tests.test_web_runs import RunsBase
from tests.web_helpers import HAVE_WEB

if HAVE_WEB:
    from sbfl.web import routes_quick as rq


@unittest.skipUnless(HAVE_WEB, "fastapi/httpx não instalados")
class Recentes(RunsBase):
    def test_grava_dedup_limita_e_ordena(self):
        for i in range(12):
            rq.add_recente(self.home, f"/p/{i}", f"p{i}", ["jaguar2"], [], True)
        rq.add_recente(self.home, "/p/5", "p5", ["maven"], ["a.B"], False)
        lst = rq.load_recentes(self.home)
        self.assertEqual(len(lst), 10)
        self.assertEqual(lst[0]["spec"], "/p/5")           # mais recente primeiro
        self.assertEqual(lst[0]["kinds"], ["maven"])
        self.assertEqual([r["spec"] for r in lst].count("/p/5"), 1)
        self.assertNotIn("/p/0", [r["spec"] for r in lst])
        self.assertTrue(lst[0]["data"])

    def test_arquivo_invalido_vira_lista_vazia(self):
        path = self.home.root / "recentes.json"
        for texto in ("{nao e json", '{"a": 1}', '[1, "x", {"nome": "sem spec"}]'):
            path.write_text(texto, encoding="utf-8")
            self.assertEqual(rq.load_recentes(self.home), [])
        rq.add_recente(self.home, "/p/1", "p", ["maven"], [], True)    # sobrescreve o lixo
        self.assertEqual(len(rq.load_recentes(self.home)), 1)
        json.loads(path.read_text(encoding="utf-8"))

    def test_parse_testes(self):
        self.assertEqual(rq.parse_tests("a.B, a.C#m\n a.D\r\n\n,"), ["a.B", "a.C#m", "a.D"])
        self.assertEqual(rq.parse_tests(""), [])


@unittest.skipUnless(HAVE_WEB, "fastapi/httpx não instalados")
class Formulario(RunsBase):
    def setUp(self):
        super().setUp()
        self.proj = self.tmp / "meu-projeto"
        self.proj.mkdir()
        (self.proj / "pom.xml").write_text("<project/>")

    def test_renderiza_formulario_e_item_na_barra(self):
        r = self.authed().get("/executar")
        self.assertEqual(r.status_code, 200)
        for t in ("Projeto", "Maven", "Jaguar 2", "Jaguar 1", "Executar", "Recentes"):
            self.assertIn(t, r.text)
        self.assertIn('href="/executar"', r.text)
        self.assertRegex(r.text, r'value="jaguar2"\s+checked')

    def test_post_cria_lote_com_tipos_e_opcoes(self):
        r = self.authed().post("/executar", data={"projeto": str(self.proj), "kinds": ["maven", "jaguar2"],
                                                  "tests_mode": "all", "auto_search": "1"},
                               follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertRegex(r.headers["location"], r"^/executar/\d+$")
        bid = int(r.headers["location"].rsplit("/", 1)[1])
        jobs = jq.batch_jobs(self.home, bid)
        self.assertEqual(sorted(j["kind"] for j in jobs), ["jaguar2", "maven"])
        self.assertTrue(jobs[0]["bug_key"].startswith("local/meu-projeto/dir-"))
        opts = json.loads(self.client_batch_opts(bid))
        self.assertEqual(opts["test_scope"], "all")
        self.assertTrue(opts["auto_search"])
        self.assertNotIn("tests", opts)
        self.assertEqual(rq.load_recentes(self.home)[0]["spec"], str(self.proj))
        self.assertIn(jobs[0]["bug_key"], self.app.state.local_bugs)

    def client_batch_opts(self, bid):
        import contextlib
        with contextlib.closing(jq._connect(self.home)) as con:
            return con.execute("SELECT options FROM batches WHERE id=?", (bid,)).fetchone()[0]

    def test_sem_busca_automatica_e_so_estes_testes(self):
        r = self.authed().post("/executar", data={
            "projeto": str(self.proj), "kinds": ["jaguar1"], "tests_mode": "only",
            "tests": "a.BTest, a.CTest#m1\nb.DTest"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        opts = json.loads(self.client_batch_opts(int(r.headers["location"].rsplit("/", 1)[1])))
        self.assertEqual(opts["tests"], ["a.BTest", "a.CTest#m1", "b.DTest"])
        self.assertEqual(opts["test_scope"], "relevant")
        self.assertFalse(opts["auto_search"])

    def test_validacao_em_portugues(self):
        c = self.authed()
        casos = [({"projeto": "  ", "kinds": ["maven"]}, "Informe o projeto"),
                 ({"projeto": str(self.proj)}, "pelo menos um tipo"),
                 ({"projeto": str(self.tmp / "nao-existe"), "kinds": ["maven"]}, "Entrada local inválida"),
                 ({"projeto": str(self.proj), "kinds": ["maven"], "tests_mode": "only", "tests": " "},
                  "Informe pelo menos um teste")]
        for data, msg in casos:
            with self.subTest(msg=msg):
                r = c.post("/executar", data=data)
                self.assertEqual(r.status_code, 400)
                self.assertIn(msg, r.text)
        self.assertEqual(jq.list_batches(self.home), [])
        self.assertEqual(rq.load_recentes(self.home), [])

    def test_recentes_aparecem_usar_e_executar_de_novo(self):
        c = self.authed()
        c.post("/executar", data={"projeto": str(self.proj), "kinds": ["jaguar1"], "tests_mode": "only",
                                  "tests": "a.BTest", "auto_search": "1"}, follow_redirects=False)
        page = c.get("/executar")
        self.assertIn("meu-projeto", page.text)
        self.assertIn("/executar/recente/0", page.text)
        usar = c.get("/executar?usar=0")
        self.assertIn(f'value="{self.proj}"', usar.text)
        self.assertIn("a.BTest", usar.text)
        again = c.post("/executar/recente/0", follow_redirects=False)
        self.assertEqual(again.status_code, 303)
        self.assertEqual(len(jq.list_batches(self.home)), 2)
        self.assertEqual(len(rq.load_recentes(self.home)), 1)       # sem duplicar
        self.assertEqual(c.post("/executar/recente/7").status_code, 404)

    def test_modulo_no_post_na_chave_e_nos_recentes(self):
        (self.proj / "sub").mkdir(exist_ok=True)
        (self.proj / "sub" / "pom.xml").write_text("<project/>")
        c = self.authed()
        sem = c.post("/executar", data={"projeto": str(self.proj), "kinds": ["maven"]}, follow_redirects=False)
        com = c.post("/executar", data={"projeto": str(self.proj), "kinds": ["maven"], "module": "sub"},
                     follow_redirects=False)
        self.assertEqual(com.status_code, 303)
        k_sem = jq.batch_jobs(self.home, int(sem.headers["location"].rsplit("/", 1)[1]))[0]
        k_com = jq.batch_jobs(self.home, int(com.headers["location"].rsplit("/", 1)[1]))[0]
        self.assertNotEqual(k_sem["bug_key"], k_com["bug_key"])
        self.assertEqual(k_com["extra"]["module"], "sub")
        rec = rq.load_recentes(self.home)
        self.assertEqual([(r["spec"], r["module"]) for r in rec], [(str(self.proj), "sub"), (str(self.proj), "")])
        page = c.get("/executar")
        self.assertIn("Módulo (subpasta com pom.xml, opcional)", page.text)
        self.assertIn('value="sub"', c.get("/executar?usar=0").text)
        again = c.post("/executar/recente/0", follow_redirects=False)
        bid = int(again.headers["location"].rsplit("/", 1)[1])
        self.assertEqual(jq.batch_jobs(self.home, bid)[0]["bug_key"], k_com["bug_key"])
        res = c.get(f"/executar/{bid}")
        self.assertIn("meu-projeto", res.text.split("</h1>")[0])
        self.assertIn("sub", res.text.split("</h1>")[0])

    def test_modulo_invalido_da_erro_em_portugues(self):
        c = self.authed()
        for mod, msg in (("nao/existe", "Módulo sem pom.xml"), ("../fora", "Módulo inválido")):
            with self.subTest(mod=mod):
                r = c.post("/executar", data={"projeto": str(self.proj), "kinds": ["maven"], "module": mod})
                self.assertEqual(r.status_code, 400)
                self.assertIn(msg, r.text)
                self.assertIn(f'value="{mod}"', r.text)
        self.assertEqual(jq.list_batches(self.home), [])

    def test_recente_com_pasta_sumida_avisa(self):
        rq.add_recente(self.home, str(self.tmp / "sumiu"), "sumiu", ["maven"], [], True)
        c = self.authed()
        page = c.get("/executar")
        self.assertIn("Pasta não encontrada", page.text)
        r = c.post("/executar/recente/0")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(jq.list_batches(self.home), [])


@unittest.skipUnless(HAVE_WEB, "fastapi/httpx não instalados")
class Guia(RunsBase):
    """Pasta sem pom.xml e coleções do Bugs.jar: a tela orienta em vez de iniciar uma execução fadada a falhar."""

    def setUp(self):
        super().setUp()
        self.col = self.tmp / "colecao"
        for n in ("maven", "camel"):
            (self.col / n / ".bugs-dot-jar").mkdir(parents=True)
            (self.col / n / ".git").mkdir()
            (self.col / n / "pom.xml").write_text("<project/>")
        (self.col / "solto.txt").write_text("x")

    def post(self, **kw):
        data = {"projeto": str(self.col), "kinds": ["maven"]}
        data.update(kw)
        return self.authed().post("/executar", data=data, follow_redirects=False)

    def test_sem_pom_lista_subpastas_com_pom(self):
        plain = self.tmp / "plain"
        for n in ("a", "b/c"):
            (plain / n).mkdir(parents=True)
            (plain / n / "pom.xml").write_text("<project/>")
        r = self.post(projeto=str(plain))
        self.assertEqual(r.status_code, 400)
        self.assertIn("não tem pom.xml", r.text)
        self.assertIn("b/c", r.text)
        self.assertIn("data-modulo=\"a\"", r.text)
        self.assertNotIn('guia-bugsjar', r.text)
        self.assertEqual(jq.list_batches(self.home), [])

    def test_sem_nenhum_pom_da_erro_claro(self):
        vazio = self.tmp / "vazio"
        (vazio / "x").mkdir(parents=True)
        r = self.post(projeto=str(vazio))
        self.assertEqual(r.status_code, 400)
        self.assertIn("Nenhum pom.xml encontrado", r.text)
        self.assertIn("precisa de um projeto Maven", r.text)
        self.assertEqual(jq.list_batches(self.home), [])

    def test_colecao_bugsjar_recomenda_a_tela_bugs(self):
        r = self.post()
        self.assertEqual(r.status_code, 400)
        self.assertIn('guia-bugsjar', r.text)
        self.assertIn("Bugs.jar", r.text)
        self.assertIn("bugsjar.root", r.text)
        self.assertIn("data-modulo=\"maven\"", r.text)
        self.assertIn("/executar/bugsjar-root", r.text)         # um clique para configurar a raiz
        self.assertEqual(jq.list_batches(self.home), [])

    def test_repo_bugsjar_com_pom_pede_confirmacao_e_roda_com_forcar(self):
        r = self.post(module="maven")
        self.assertEqual(r.status_code, 400)
        self.assertIn('guia-bugsjar', r.text)
        self.assertIn("Rodar mesmo assim", r.text)
        self.assertEqual(jq.list_batches(self.home), [])
        ok = self.post(module="maven", forcar="1")
        self.assertEqual(ok.status_code, 303)
        job = jq.batch_jobs(self.home, int(ok.headers["location"].rsplit("/", 1)[1]))[0]
        self.assertTrue(job["bug_key"].startswith("local/maven/dir-"))     # o módulo virou o projeto
        self.assertNotIn("module", job["extra"])
        self.assertEqual(Path(job["extra"]["path"]), (self.col / "maven").resolve())

    def test_raiz_sem_pom_com_modulo_valido_sem_bugsjar_roda(self):
        plain = self.tmp / "plain2"
        (plain / "m").mkdir(parents=True)
        (plain / "m" / "pom.xml").write_text("<project/>")
        r = self.post(projeto=str(plain), module="m")
        self.assertEqual(r.status_code, 303)

    def test_recente_de_pasta_sem_pom_tambem_orienta(self):
        rq.add_recente(self.home, str(self.col), "colecao", ["maven"], [], True)
        r = self.authed().post("/executar/recente/0")
        self.assertEqual(r.status_code, 400)
        self.assertIn('guia-bugsjar', r.text)
        self.assertEqual(jq.list_batches(self.home), [])

    def test_definir_raiz_bugsjar_com_um_clique(self):
        r = self.authed().post("/executar/bugsjar-root", data={"projeto": str(self.col)}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/bugs")
        self.assertEqual(self.settings.get("bugsjar", "root"), str(self.col))
        from sbfl.home import load_settings
        self.assertEqual(load_settings(self.home).get("bugsjar", "root"), str(self.col))
        ruim = self.authed().post("/executar/bugsjar-root", data={"projeto": str(self.tmp / "plain")})
        self.assertEqual(ruim.status_code, 400)


@unittest.skipUnless(HAVE_WEB, "fastapi/httpx não instalados")
class Seletor(RunsBase):
    def test_botao_so_com_tkinter(self):
        c = self.authed()
        with mock.patch.object(rq, "_tk_available", return_value=True):
            self.assertIn("/executar/escolher", c.get("/executar").text)
        with mock.patch.object(rq, "_tk_available", return_value=False):
            self.assertNotIn("/executar/escolher", c.get("/executar").text)
            self.assertEqual(c.post("/executar/escolher").status_code, 404)

    def test_escolher_devolve_campo_preenchido(self):
        with mock.patch.object(rq, "_tk_available", return_value=True), \
                mock.patch.object(rq, "_pick_folder", return_value='C:\\Meus "Projetos"'):
            r = self.authed().post("/executar/escolher", data={"projeto": "antigo"})
        self.assertEqual(r.status_code, 200)
        self.assertIn('id="projeto"', r.text)
        self.assertIn("C:\\Meus &quot;Projetos&quot;", r.text)
        with mock.patch.object(rq, "_tk_available", return_value=True), \
                mock.patch.object(rq, "_pick_folder", return_value=""):    # cancelou: mantém o digitado
            r = self.authed().post("/executar/escolher", data={"projeto": "antigo"})
        self.assertIn('value="antigo"', r.text)

    def test_pick_folder_usa_tkinter_topmost(self):
        tk, fd = mock.MagicMock(), mock.MagicMock()
        fd.askdirectory.return_value = "E:/proj"
        tk.filedialog = fd
        with mock.patch.dict(sys.modules, {"tkinter": tk, "tkinter.filedialog": fd}):
            self.assertEqual(rq._pick_folder(), "E:/proj")
        tk.Tk.return_value.attributes.assert_any_call("-topmost", True)
        tk.Tk.return_value.destroy.assert_called()


@unittest.skipUnless(HAVE_WEB, "fastapi/httpx não instalados")
class Cartoes(RunsBase):
    def setUp(self):
        super().setUp()
        self.bug = Bug("local", "meu-projeto", "dir-abc", {"path": str(self.tmp)})

    def batch(self, kinds):
        return jq.create_batch(self.home, "x", [self.bug], kinds, {"test_scope": "all"})

    def finish(self, bid, kind, rel):
        import contextlib
        with contextlib.closing(jq._connect(self.home)) as con, con:
            con.execute("UPDATE jobs SET state='done', run_rel=? WHERE batch_id=? AND kind=?",
                        (rel, bid, kind))

    def test_job_pendente_reagenda_a_cada_2s(self):
        bid = self.batch(["jaguar2", "maven"])
        c = self.authed()
        page = c.get(f"/executar/{bid}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Na fila", page.text)
        self.assertIn("/job/", page.text)
        self.assertIn("ao-vivo", page.text)
        frag = c.get(f"/executar/{bid}/cartoes")
        self.assertIn(f'hx-get="/executar/{bid}/cartoes"', frag.text)
        self.assertIn("delay:2s", frag.text)
        self.assertNotIn("<html", frag.text)

    def test_tudo_terminado_nao_reagenda_e_mostra_top_linhas(self):
        csv = "".join(f"a/Cls{i},{i + 1},{i},0,1,5,{i / 20:.2f}\n" for i in range(15))
        rel = self.make_run(bug=self.bug, csv=csv, status="ok_testes_falhando")
        bid = self.batch(["jaguar2"])
        self.finish(bid, "jaguar2", rel)
        c = self.authed()
        frag = c.get(f"/executar/{bid}/cartoes")
        self.assertEqual(frag.status_code, 200)
        self.assertNotIn("hx-get", frag.text)
        self.assertNotIn("hx-trigger", frag.text)
        self.assertIn("ok_testes_falhando", frag.text)
        self.assertIn("2 testes", frag.text)
        self.assertIn("1 com falha", frag.text)
        self.assertIn(f"/run/{rel}/codigo?classe=a.Cls14#L15", frag.text)     # maior susp primeiro
        self.assertNotIn("classe=a.Cls4#", frag.text)                        # só as 10 mais suspeitas
        self.assertEqual(frag.text.count("#L"), 10)
        self.assertIn(f'href="/run/{rel}/codigo"', frag.text)
        self.assertIn(f'href="/run/{rel}"', frag.text)
        self.assertIn("0.70", frag.text)

    def test_diagnostico_e_sugestoes_em_execucao_com_falha(self):
        diag = {"status": "compilacao_falhou", "cause": "Falha ao compilar o projeto", "evidence": [],
                "suggestions": [{"fix": None, "params": {}, "jdk": 8, "maven": None, "label": "Tentar com JDK 8"}],
                "warnings": []}
        rel = self.make_run(kind="maven", status="compilacao_falhou", bug=self.bug, diag=diag)
        bid = self.batch(["maven"])
        self.finish(bid, "maven", rel)
        text = self.authed().get(f"/executar/{bid}/cartoes").text
        self.assertIn("compilacao_falhou", text)
        self.assertIn("Falha ao compilar o projeto", text)
        self.assertIn("Tentar com JDK 8", text)
        self.assertNotIn("/codigo", text)               # Maven não tem linhas

    def test_link_de_comparacao_com_maven_e_jaguar2_prontos(self):
        rm = self.make_run(kind="maven", bug=self.bug)
        rj = self.make_run(kind="jaguar2", bug=self.bug)
        bid = self.batch(["maven", "jaguar2"])
        self.finish(bid, "maven", rm)
        text_parcial = self.authed().get(f"/executar/{bid}/cartoes").text
        self.assertNotIn("/comparar", text_parcial)
        self.finish(bid, "jaguar2", rj)
        text = self.authed().get(f"/executar/{bid}/cartoes").text
        self.assertIn(f"/comparar?a={rm}&amp;b={rj}", text)

    def test_job_com_erro_sem_execucao(self):
        import contextlib
        bid = self.batch(["maven"])
        with contextlib.closing(jq._connect(self.home)) as con, con:
            con.execute("UPDATE jobs SET state='done', error='RuntimeError(boom)' WHERE batch_id=?", (bid,))
        text = self.authed().get(f"/executar/{bid}/cartoes").text
        self.assertIn("boom", text)
        self.assertNotIn("hx-get", text)

    def test_lote_inexistente(self):
        c = self.authed()
        self.assertEqual(c.get("/executar/999").status_code, 404)
        self.assertEqual(c.get("/executar/999/cartoes").status_code, 404)


class SoEstesTestes(ExecBase):
    def test_tests_substitui_relevantes_e_forca_escopo(self):
        from sbfl import execute as ex
        seen = []

        def behave(n, cfg):
            seen.append((cfg.test_scope, self.prep.relevant_tests))
            return "ok"
        opts = ex.RunOptions(tests=["a.XTest#m"], test_scope="all", auto_search=False)
        orig = self.fake_runner(behave)

        def runner(home, settings, prep, *a, **k):
            seen.append(prep.relevant_tests)
            return orig(home, settings, prep, *a, **k)
        with mock.patch.object(ex, "prepare", return_value=self.prep), \
                mock.patch.object(ex, "run_jaguar2", runner):
            rel = ex.execute(self.home, self.settings, self.prep.bug, "jaguar2", opts)
        self.assertIn(["a.XTest#m"], seen)
        self.assertEqual(self.cfgs[0].test_scope, "relevant")
        from sbfl.store import load_run
        run = load_run(self.home, rel)
        self.assertEqual(run["relevant_tests"], ["a.XTest#m"])
        self.assertEqual(run["test_scope"], "relevant")
        self.assertEqual(self.prep.relevant_tests, ["a.BTest"])         # o Prepared original não muda


if __name__ == "__main__":
    unittest.main()
