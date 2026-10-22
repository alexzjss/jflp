from __future__ import annotations

import gzip
import json
import unittest
from unittest import mock

from sbfl import queue as jq
from sbfl import store
from sbfl.adapters.base import Bug
from tests.web_helpers import HAVE_WEB, WebFixture

BUG = Bug("bugsjar", "commons-lang", "7")
DIAG = {"status": "classes_duplicadas", "cause": "Classes duplicadas", "evidence": ["duplicate class: a.B"],
        "suggestions": [{"fix": "exclude-duplicates", "params": {"files": ["a/B.java"]}, "jdk": None,
                         "maven": None, "label": "Excluir os arquivos duplicados"},
                        {"fix": None, "params": {}, "jdk": 8, "maven": None, "label": "Tentar com JDK 8"}],
        "warnings": []}


@unittest.skipUnless(HAVE_WEB, "fastapi/httpx não instalados")
class RunsBase(WebFixture, unittest.TestCase):
    def make_run(self, kind="jaguar2", status="ok", bug=BUG, fixes=("junit4",), attempts=2, diag=None,
                 csv="a/B,10,3,0,1,5,0.9\na/B,11,1,2,1,5,0.2\na/C,3,0,3,0,6,0.0\n", detected=None,
                 off=()):
        w = store.RunWriter(self.home, bug, kind)
        for n in range(1, attempts + 1):
            d = w.attempt_dir(n)
            with gzip.open(d / "log.txt.gz", "wt", encoding="utf-8") as f:
                f.write(f"linha um {n}\nErro de compilação aqui\nlinha tres\n")
            att = {"config": {"fixes_on": list(fixes), "jdk": 11},
                   "status": "ok" if n == attempts else "compilacao_falhou"}
            if detected is not None:
                att["extra"] = {"pom_plan": {"detected": list(detected)}}
            w.write_json(f"attempts/{n}/attempt.json", att)
        w.write_text("outputs/jaguar2.csv", csv)
        w.write_text("outputs/surefire-reports/TEST-a.BTest.xml",
                     '<testsuite><testcase classname="a.BTest" name="t1"/>'
                     '<testcase classname="a.BTest" name="t2"><failure message="x"/></testcase></testsuite>')
        cfg = {"fixes_on": list(fixes), "fixes_off": list(off), "params": {}, "jdk": None, "maven": None,
               "test_scope": "relevant"}
        w.finish({"status": status, "diagnosis": diag or {"status": status, "cause": "", "evidence": [],
                                                          "suggestions": [], "warnings": []},
                  "tests": {"total": 2, "passed": 1, "failed": 1, "error": 0, "skipped": 0},
                  "attempts": attempts, "attempt_configs": [cfg] * attempts, "test_scope": "relevant",
                  "truth": [{"cls": "a.B", "line": 10, "kind": "removed"}], "warnings": []})
        return w.rel


class RunPage(RunsBase):
    def test_run_page_renders_synthetic_run(self):
        rel = self.make_run()
        c = self.authed()
        r = c.get(f"/run/{rel}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("ok", r.text)
        self.assertIn("a.BTest#t2", r.text)
        self.assertIn("outputs/jaguar2.csv", r.text)
        frag = c.get(f"/run/{rel}/linhas?ord=susp&dir=desc")
        self.assertIn('class="defeito"', frag.text)
        self.assertLess(frag.text.index("0.9"), frag.text.index("0.2"))
        only_c = c.get(f"/run/{rel}/linhas?filtro=a.C").text
        self.assertIn("a.C", only_c)
        self.assertNotIn(">11<", only_c)
        self.assertEqual(c.get("/run/bugsjar/x/1/nada").status_code, 404)
        self.assertEqual(c.get("/run/bugsjar/%2e%2e/x/1/nada").status_code, 404)

    def test_valid_rel(self):
        from sbfl.web.routes_runs import _valid_rel
        self.assertTrue(_valid_rel("bugsjar/proj/7/20261020-1-maven"))
        for bad in ("bugsjar/../x/1", "bugsjar/../x/1/y", "a/./b/c/d", "a/b/c/.d", r"a/b\c/d/e",
                    "C:/b/c/d", "a/b/c", "a/b/c/d/e", "a//c/d"):
            with self.subTest(bad=bad):
                self.assertFalse(_valid_rel(bad))

    def test_file_download_confined_to_run_dir(self):
        rel = self.make_run()
        c = self.authed()
        ok = c.get(f"/run/{rel}/arquivo", params={"p": "outputs/jaguar2.csv"})
        self.assertEqual(ok.status_code, 200)
        self.assertIn("a/B,10", ok.text)
        for bad in ("../../../../jflp.db", "/etc/passwd", "C:/Windows/win.ini", "outputs/../../x"):
            with self.subTest(bad=bad):
                self.assertEqual(c.get(f"/run/{rel}/arquivo", params={"p": bad}).status_code, 400)

    def test_open_folder_htmx_fragment_on_error(self):
        rel = self.make_run()
        with mock.patch("sbfl.web.routes_runs._open_path", side_effect=OSError("falhou")):
            r = self.authed().post(f"/run/{rel}/abrir", headers={"HX-Request": "true"})
        self.assertEqual(r.status_code, 500)
        self.assertNotIn("<html", r.text)
        self.assertIn("Não foi possível abrir a pasta: falhou", r.text)

    def test_attempts_skips_malformed_json(self):
        rel = self.make_run()
        d = store.run_dir(self.home, rel) / "attempts" / "1" / "attempt.json"
        d.chmod(0o666)
        d.write_text("[1, 2]", encoding="utf-8")
        r = self.authed().get(f"/run/{rel}")
        self.assertEqual(r.status_code, 200)

    def test_attempts_show_effective_jdk_maven_from_toolenv(self):
        rel = self.make_run(attempts=2)
        d = store.run_dir(self.home, rel) / "attempts"
        # tentativa 1: configuração "padrão do perfil" (jdk None) e o JDK/Maven realmente usados em toolenv
        # tentativa 2: arquivo antigo, sem toolenv, usa os valores da configuração
        f = d / "1" / "attempt.json"
        f.chmod(0o666)
        f.write_text(json.dumps(
            {"config": {"fixes_on": [], "jdk": None, "maven": None}, "status": "compilacao_falhou",
             "toolenv": {"jdk": {"major": 8}, "maven": {"version": "3.9.6"}}}), encoding="utf-8")
        r = self.authed().get(f"/run/{rel}")
        self.assertEqual(r.status_code, 200)
        self.assertRegex(r.text, r"<td>1</td>[\s\S]*?<td>8</td>\s*<td>3\.9\.6</td>")
        self.assertRegex(r.text, r"<td>2</td>[\s\S]*?<td>11</td>\s*<td>-</td>")

    def test_log_search(self):
        rel = self.make_run()
        r = self.authed().get(f"/run/{rel}/log", params={"q": "compilação"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Erro de compilação aqui", r.text)
        self.assertIn("2 ocorrências", r.text)

    def test_retry_enqueues_with_parent_and_fix(self):
        rel = self.make_run(kind="jaguar2", status="classes_duplicadas", diag=DIAG, fixes=("junit4",))
        c = self.authed()
        p = mock.patch("sbfl.web.comum.bug_from_key", return_value=BUG)
        p.start()
        self.addCleanup(p.stop)
        r = c.post(f"/run/{rel}/tentar", data={"idx": "1"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertRegex(r.headers["location"], r"^/job/\d+/ao-vivo$")
        bid = jq.active_batch_ids(self.home)[0]
        opts = __import__("json").loads(self._opts(bid))
        self.assertEqual(opts["jdk"], 8)
        self.assertEqual(opts["parent"], rel)
        self.assertEqual(opts["fixes_on"], ["junit4"])
        self.assertEqual(jq.batch_jobs(self.home, bid)[0]["bug_key"], BUG.key)
        # exclude-duplicates exige confirmação
        r = c.post(f"/run/{rel}/tentar", data={"idx": "0"}, follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("a/B.java", r.text)
        self.assertEqual(len(jq.active_batch_ids(self.home)), 1)
        r = c.post(f"/run/{rel}/tentar", data={"idx": "0", "confirmar": "1"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        bid2 = jq.active_batch_ids(self.home)[-1]
        opts = __import__("json").loads(self._opts(bid2))
        self.assertIn("exclude-duplicates", opts["fixes_on"])
        self.assertEqual(opts["params"]["exclude-duplicates"], {"files": ["a/B.java"]})

    def _retry(self, rel, idx=0):
        p = mock.patch("sbfl.web.comum.bug_from_key", return_value=BUG)
        p.start()
        self.addCleanup(p.stop)
        r = self.authed().post(f"/run/{rel}/tentar", data={"idx": str(idx), "confirmar": "1"},
                               follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        bid = jq.active_batch_ids(self.home)[-1]
        return __import__("json").loads(self._opts(bid))

    def test_retry_with_detected_fixes_keeps_them_and_adds_suggestion(self):
        diag = {"status": "plugin_incompativel", "cause": "x", "evidence": [], "warnings": [],
                "suggestions": [{"fix": "skip-rat", "params": {}, "jdk": None, "maven": None, "label": "RAT"}]}
        rel = self.make_run(status="plugin_incompativel", diag=diag, fixes=(), detected=["junit4", "source-dirs"],
                            off=["source-dirs"])
        opts = self._retry(rel)
        self.assertEqual(sorted(opts["fixes_on"]), ["junit4", "skip-rat"])     # detectadas - desligadas + sugestão
        self.assertEqual(opts["fixes_off"], ["source-dirs"])

    def test_retry_without_known_detection_asks_execute_to_add_to_detected(self):
        diag = {"status": "plugin_incompativel", "cause": "x", "evidence": [], "warnings": [],
                "suggestions": [{"fix": "skip-rat", "params": {}, "jdk": None, "maven": None, "label": "RAT"}]}
        rel = self.make_run(status="plugin_incompativel", diag=diag, fixes=())
        opts = self._retry(rel)
        self.assertEqual(opts["fixes_on"], [])
        self.assertEqual(opts["fixes_add"], ["skip-rat"])

    def test_run_page_hides_suggestions_that_do_not_apply_to_the_kind(self):
        diag = {"status": "plugin_incompativel", "cause": "x", "evidence": [], "warnings": [],
                "suggestions": [{"fix": "skip-rat", "params": {}, "jdk": None, "maven": None, "label": "Pular RAT"},
                                {"fix": "unblock-http", "params": {}, "jdk": None, "maven": None,
                                 "label": "Liberar http"}]}
        rel = self.make_run(kind="maven", status="plugin_incompativel", diag=diag, fixes=())
        page = self.authed().get(f"/run/{rel}").text
        self.assertNotIn("Pular RAT", page)
        self.assertIn("Liberar http", page)
        # o índice enviado é o da lista filtrada e o servidor aplica o mesmo filtro
        opts = self._retry(rel, 0)
        self.assertEqual(opts["fixes_on"], ["unblock-http"])

    def _opts(self, bid):
        import contextlib
        with contextlib.closing(jq._connect(self.home)) as con:
            return con.execute("SELECT options FROM batches WHERE id=?", (bid,)).fetchone()[0]

    def test_open_folder_and_zip(self):
        rel = self.make_run()
        c = self.authed()
        with mock.patch("sbfl.web.routes_runs._open_path") as op:
            self.assertEqual(c.post(f"/run/{rel}/abrir").status_code, 200)
        op.assert_called_once_with(store.run_dir(self.home, rel))
        page = c.get(f"/run/{rel}").text
        self.assertIn("Tentativas", page)
        self.assertIn("compilacao_falhou", page)
        z = c.get(f"/run/{rel}/zip")
        self.assertEqual(z.status_code, 200)
        import io
        import zipfile
        self.assertIn("run.json", zipfile.ZipFile(io.BytesIO(z.content)).namelist())


class ResultadoNaPagina(RunsBase):
    """Seção "Resultado": status honesto, provável erro, 10 linhas mais suspeitas e downloads."""
    STACK = ("java.lang.AssertionError: boom\n\tat org.junit.Assert.fail(Assert.java:1)\n"
             "\tat a.B.soma(B.java:10)\n\tat a.BTest.t2(BTest.java:4)\n")

    def run_with(self, kind="jaguar2", status="ok_testes_falhando", **fin):
        w = store.RunWriter(self.home, BUG, kind)
        w.write_text("outputs/jaguar2.csv", "a/B,10,3,0,1,5,0.9\na/B,11,1,2,1,5,0.2\n")
        base = {"status": status, "diagnosis": {"status": status, "cause": "", "evidence": [], "suggestions": [],
                                                "warnings": []},
                "tests": {"total": 2, "passed": 1, "failed": 1, "error": 0, "skipped": 0}, "attempts": 1,
                "attempt_configs": [], "truth": [{"cls": "a.B", "line": 10, "kind": "removed"}], "warnings": []}
        base.update(fin)
        w.finish(base)
        return w.rel

    def test_mostra_provavel_erro_top_linhas_e_downloads(self):
        rel = self.run_with(failures=[{"test": "a.BTest#t2", "message": "boom", "type": "AssertionError",
                                       "frame": {"cls": "a.B", "method": "soma", "file": "B.java", "line": 10}}],
                            warnings=["aviso do prepare"])
        t = self.authed().get(f"/run/{rel}").text
        self.assertIn("a.BTest#t2", t)
        self.assertIn("boom", t)
        self.assertIn("a.B.soma (B.java:10)", t)
        self.assertIn("10 linhas mais suspeitas", t)
        self.assertIn("a.B:10", t)
        self.assertIn("linha do defeito", t)
        self.assertIn("aviso do prepare", t)
        self.assertIn(f"/run/{rel}/ranking.csv", t)
        self.assertIn(f"/run/{rel}/espectro", t)

    def test_download_do_ranking_e_da_saida_bruta(self):
        rel = self.run_with()
        c = self.authed()
        r = c.get(f"/run/{rel}/ranking.csv")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.text.splitlines(), ["class,line,score,rank", "a.B,10,0.9,1", "a.B,11,0.2,2"])
        self.assertIn("attachment", r.headers["content-disposition"])
        raw = c.get(f"/run/{rel}/espectro")
        self.assertEqual(raw.status_code, 200)
        self.assertIn("a/B,10", raw.text)
        self.assertEqual(c.get("/run/bugsjar/x/1/nada/ranking.csv").status_code, 404)

    def test_maven_nao_tem_ranking(self):
        rel = self.run_with(kind="maven")
        c = self.authed()
        self.assertEqual(c.get(f"/run/{rel}/ranking.csv").status_code, 404)
        self.assertNotIn("ranking.csv", c.get(f"/run/{rel}").text)

    def test_execucao_antiga_sem_nenhum_teste_falhando_e_mostrada_com_a_causa(self):
        rel = self.run_with(status="ok", tests={"total": 1, "passed": 1, "failed": 0, "error": 0, "skipped": 0})
        t = self.authed().get(f"/run/{rel}").text
        self.assertIn("sem_teste_falhando", t)
        self.assertIn("Nenhum teste falhou: o SBFL precisa de ao menos um teste falhando", t)

    def test_jaguar1_vazio_avisa_que_nada_foi_instrumentado(self):
        w = store.RunWriter(self.home, BUG, "jaguar1")
        w.write_text("outputs/jaguar1.xml", '<FlatFaultClassification heuristic="OCHIAI" project="x"/>')
        w.finish({"status": "ok", "diagnosis": {"status": "ok", "cause": "", "evidence": [], "suggestions": [],
                                                "warnings": []},
                  "tests": {"total": 1, "passed": 1, "failed": 0, "error": 0, "skipped": 0}, "attempts": 1,
                  "attempt_configs": [], "truth": [], "warnings": []})
        t = self.authed().get(f"/run/{w.rel}").text
        self.assertIn("nenhuma classe do código foi instrumentada", t)
        self.assertIn("Nenhuma linha foi pontuada", t)
        self.assertNotIn("ranking.csv", t)
        self.assertIn("/espectro", t)

    def test_execucao_antiga_sem_failures_le_os_relatorios_guardados(self):
        rel = self.run_with(kind="maven")
        w_dir = store.run_dir(self.home, rel)
        rep = w_dir / "outputs" / "surefire-reports"
        rep.mkdir(parents=True)
        (rep / "TEST-a.BTest.xml").write_text(
            '<testsuite><testcase classname="a.BTest" name="t2"><failure message="boom">' + self.STACK
            + "</failure></testcase></testsuite>", encoding="utf-8")
        t = self.authed().get(f"/run/{rel}").text
        self.assertIn("a.B.soma (B.java:10)", t)

    J1_XML = ('<F><requirements location="811" cef="1" cep="0" cnf="0" cnp="258" name="a.B" suspicious-value="1.0"/>'
              '<requirements location="12" cef="0" cep="9" cnf="1" cnp="249" name="a.C" suspicious-value="0.1"/>'
              '</F>')

    def j1_cortado(self):
        """Execução antiga do Jaguar 1 cujo log parou após 19 testes, gravada como sem_teste_falhando."""
        w = store.RunWriter(self.home, BUG, "jaguar1")
        w.write_text("outputs/jaguar1.xml", self.J1_XML)
        with gzip.open(w.dir / "outputs" / "jaguar1.log.gz", "wt", encoding="utf-8") as f:
            f.write("# sbfl attempt 1\n" + "".join(f"Test t{i}(a.T) : Passed\n" for i in range(19)))
        w.finish({"status": "sem_teste_falhando",
                  "diagnosis": {"status": "sem_teste_falhando", "cause": "Nenhum teste falhou: x", "evidence": [],
                                "suggestions": [], "warnings": []},
                  "tests": {"total": 19, "passed": 19, "failed": 0, "error": 0, "skipped": 0}, "attempts": 1,
                  "attempt_configs": [], "failures": [], "warnings": [],
                  "truth": [{"cls": "a.B", "line": 811, "kind": "removed"},
                            {"cls": "a.D", "line": 3, "kind": "removed"}]})
        return w.rel

    def test_jaguar1_com_log_cortado_e_reavaliado_pelo_xml(self):
        t = self.authed().get(f"/run/{self.j1_cortado()}").text
        self.assertIn("ok_testes_falhando", t)
        self.assertNotIn("Nenhum teste falhou", t)
        self.assertIn("o log do Jaguar foi interrompido", t)
        self.assertIn("259 testes", t)
        self.assertIn("(falha sem nome 1)", t)
        self.assertIn("10 linhas mais suspeitas", t)

    def test_linhas_do_defeito_com_posicao_no_ranking(self):
        t = self.authed().get(f"/run/{self.j1_cortado()}").text
        self.assertIn("Linhas do defeito (pelo patch)", t)
        self.assertIn("B:811", t)
        self.assertIn("posição 1 de 2", t)
        self.assertIn("melhor linha do defeito: posição 1 de 2", t)
        self.assertNotIn("posição esperada", t)  # sem empate não há posição esperada na linha
        self.assertIn("não coberta", t)          # a.D:3 não está no espectro

    def test_empates_mostram_faixa_de_posicoes_e_posicao_esperada(self):
        """12 linhas empatadas em 1.0 (Jaguar 1 real tinha 137): faixa 1–12, esperada 6.5, nota no top 10."""
        tied = "".join(f'<requirements location="{i}" cef="1" cep="0" cnf="0" cnp="258" name="a.B" '
                       f'suspicious-value="1.0"/>' for i in range(1, 13))
        xml = "<F>" + tied + '<requirements location="12" cef="0" cep="9" cnf="1" cnp="249" name="a.C" ' \
                             'suspicious-value="0.1"/></F>'
        w = store.RunWriter(self.home, BUG, "jaguar1")
        w.write_text("outputs/jaguar1.xml", xml)
        w.finish({"status": "ok", "diagnosis": {"status": "ok", "cause": "", "evidence": [], "suggestions": [],
                                                "warnings": []},
                  "tests": {"total": 1, "passed": 1, "failed": 0, "error": 0, "skipped": 0}, "attempts": 1,
                  "attempt_configs": [], "failures": [], "warnings": [],
                  "truth": [{"cls": "a.B", "line": 5, "kind": "removed"},
                            {"cls": "a.C", "line": 12, "kind": "removed"}]})
        c = self.authed()
        t = c.get(f"/run/{w.rel}").text
        self.assertIn("posições 1–12 de 13 (12 empatadas), posição esperada 6.5", t)
        self.assertIn("melhor linha do defeito: posições 1–12 (empate com 12 linhas); posição esperada 6.5", t)
        self.assertIn("posição 13 de 13", t)
        self.assertIn("12 linhas empatadas com pontuação 1.0000; a ordem entre elas é arbitrária", t)
        csv_rows = c.get(f"/run/{w.rel}/ranking.csv").text.splitlines()
        self.assertEqual(len(csv_rows), 14)
        self.assertTrue(all(r.endswith(",1") for r in csv_rows[1:13]))   # CSV: empatadas dividem a melhor posição
        self.assertTrue(csv_rows[13].endswith(",13"))

    def test_maven_com_verdade_lista_as_linhas_do_defeito(self):
        rel = self.run_with(kind="maven")
        t = self.authed().get(f"/run/{rel}").text
        self.assertIn("Linhas do defeito (pelo patch)", t)
        self.assertIn("B:10", t)
        self.assertNotIn("melhor linha do defeito", t)

    def test_sem_verdade_nao_mostra_secao_de_linhas_do_defeito(self):
        rel = self.run_with(truth=[])
        self.assertNotIn("Linhas do defeito (pelo patch)", self.authed().get(f"/run/{rel}").text)


if __name__ == "__main__":
    unittest.main()
