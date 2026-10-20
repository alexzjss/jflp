from __future__ import annotations

import gzip
import unittest
from unittest import mock

from sbfl import queue as jq
from sbfl import store
from sbfl.adapters.base import Bug
from sbfl.home import load_settings
from tests.test_web_base import WebBase

BUG = Bug("bugsjar", "commons-lang", "7")
DIAG = {"status": "classes_duplicadas", "cause": "Classes duplicadas", "evidence": ["duplicate class: a.B"],
        "suggestions": [{"fix": "exclude-duplicates", "params": {"files": ["a/B.java"]}, "jdk": None,
                         "maven": None, "label": "Excluir os arquivos duplicados"},
                        {"fix": None, "params": {}, "jdk": 8, "maven": None, "label": "Tentar com JDK 8"}],
        "warnings": []}


class RunsBase(WebBase):
    def make_run(self, kind="jaguar2", status="ok", bug=BUG, fixes=("junit4",), attempts=2, diag=None,
                 csv="a/B,10,3,0,1,5,0.9\na/B,11,1,2,1,5,0.2\na/C,3,0,3,0,6,0.0\n"):
        w = store.RunWriter(self.home, bug, kind)
        for n in range(1, attempts + 1):
            d = w.attempt_dir(n)
            with gzip.open(d / "log.txt.gz", "wt", encoding="utf-8") as f:
                f.write(f"linha um {n}\nErro de compilação aqui\nlinha tres\n")
        w.write_text("outputs/jaguar2.csv", csv)
        w.write_text("outputs/surefire-reports/TEST-a.BTest.xml",
                     '<testsuite><testcase classname="a.BTest" name="t1"/>'
                     '<testcase classname="a.BTest" name="t2"><failure message="x"/></testcase></testsuite>')
        cfg = {"fixes_on": list(fixes), "fixes_off": [], "params": {}, "jdk": None, "maven": None,
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
        self.assertIn("Salvar como receita", r.text)
        frag = c.get(f"/run/{rel}/linhas?ord=susp&dir=desc")
        self.assertIn('class="defeito"', frag.text)
        self.assertLess(frag.text.index("0.9"), frag.text.index("0.2"))
        only_c = c.get(f"/run/{rel}/linhas?filtro=a.C").text
        self.assertIn("a.C", only_c)
        self.assertNotIn(">11<", only_c)
        self.assertEqual(c.get("/run/bugsjar/x/1/nada").status_code, 404)
        self.assertEqual(c.get("/run/bugsjar/../x/1/nada").status_code, 404)

    def test_file_download_confined_to_run_dir(self):
        rel = self.make_run()
        c = self.authed()
        ok = c.get(f"/run/{rel}/arquivo", params={"p": "outputs/jaguar2.csv"})
        self.assertEqual(ok.status_code, 200)
        self.assertIn("a/B,10", ok.text)
        for bad in ("../../../../jflp.db", "/etc/passwd", "C:/Windows/win.ini", "outputs/../../x"):
            with self.subTest(bad=bad):
                self.assertEqual(c.get(f"/run/{rel}/arquivo", params={"p": bad}).status_code, 400)

    def test_log_search(self):
        rel = self.make_run()
        r = self.authed().get(f"/run/{rel}/log", params={"q": "compilação"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Erro de compilação aqui", r.text)
        self.assertIn("2 ocorrências", r.text)

    def test_retry_enqueues_with_parent_and_fix(self):
        rel = self.make_run(kind="jaguar2", status="classes_duplicadas", diag=DIAG, fixes=("junit4",))
        c = self.authed()
        p = mock.patch("sbfl.web.routes_bugs.bug_from_key", return_value=BUG)
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

    def _opts(self, bid):
        import contextlib
        with contextlib.closing(jq._connect(self.home)) as con:
            return con.execute("SELECT options FROM batches WHERE id=?", (bid,)).fetchone()[0]

    def test_save_recipe_writes_toml(self):
        rel = self.make_run()
        c = self.authed()
        r = c.post(f"/run/{rel}/receita", data={"scope": "bug", "note": "ok"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        txt = (self.home.recipes / "bugsjar" / "commons-lang.toml").read_text(encoding="utf-8")
        self.assertIn('match = "7"', txt)
        self.assertIn('fixes = ["junit4"]', txt)
        bad = c.post(f"/run/{rel}/receita", data={"scope": "glob", "glob": ""})
        self.assertEqual(bad.status_code, 400)
        plain = self.make_run(fixes=(), attempts=1)
        self.assertNotIn("Salvar como receita", c.get(f"/run/{plain}").text)
        self.assertEqual(c.post(f"/run/{plain}/receita", data={"scope": "bug"}).status_code, 400)

    def test_open_folder_and_zip(self):
        rel = self.make_run()
        c = self.authed()
        with mock.patch("sbfl.web.routes_runs._open_path") as op:
            self.assertEqual(c.post(f"/run/{rel}/abrir").status_code, 200)
        op.assert_called_once_with(store.run_dir(self.home, rel))
        z = c.get(f"/run/{rel}/zip")
        self.assertEqual(z.status_code, 200)
        import io
        import zipfile
        self.assertIn("run.json", zipfile.ZipFile(io.BytesIO(z.content)).namelist())


class Compare(RunsBase):
    def test_compare_page(self):
        a = self.make_run(kind="jaguar2")
        b = self.make_run(kind="jaguar2", csv="a/B,10,3,0,1,5,0.5\na/D,1,1,0,0,2,0.1\n")
        c = self.authed()
        r = c.get("/comparar", params={"a": a, "b": b})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Posição das linhas do defeito", r.text)
        self.assertIn("a.D", r.text)
        self.assertEqual(c.get("/comparar").status_code, 200)
        self.assertEqual(c.get("/comparar", params={"a": "x/y/z/w", "b": b}).status_code, 404)

    def test_bug_page_links(self):
        j2 = self.make_run(kind="jaguar2")
        mv = self.make_run(kind="maven")
        with mock.patch("sbfl.web.routes_bugs.bug_from_key", return_value=BUG):
            r = self.authed().get(f"/bug/{BUG.key}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(f"/comparar?a={mv}&amp;b={j2}", r.text)
        self.assertNotIn("Jaguar 1 × Jaguar 2", r.text)


class Batches(RunsBase):
    def test_batch_create_progress_csv(self):
        c = self.authed()
        pre = c.get("/lotes", params={"bugs": "bugsjar/commons-lang/7,bugsjar/commons-lang/8"})
        self.assertIn("bugsjar/commons-lang/8", pre.text)
        with mock.patch("sbfl.web.routes_bugs.bug_from_key", side_effect=lambda s, k: Bug(*k.split("/", 2))):
            r = c.post("/lotes", data={"bugs": "bugsjar/commons-lang/7\nbugsjar/commons-lang/8",
                                       "kinds": ["maven", "jaguar2"], "scope": "all", "auto_search": "1"},
                       follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        bid = int(r.headers["location"].rsplit("/", 1)[1])
        self.assertEqual(len(jq.batch_jobs(self.home, bid)), 4)
        rel = self.make_run()
        con_job = jq.batch_jobs(self.home, bid)[0]
        import contextlib
        with contextlib.closing(jq._connect(self.home)) as con, con:
            con.execute("UPDATE jobs SET state='done', run_rel=?, seconds=5 WHERE id=?", (rel, con_job["id"]))
        page = c.get(f"/lote/{bid}")
        self.assertIn('hx-trigger="every 3s"', page.text)
        frag = c.get(f"/lote/{bid}/progresso")
        self.assertNotIn("<html", frag.text)
        self.assertIn("Pausar", frag.text)
        csvr = c.get(f"/lote/{bid}/csv")
        self.assertEqual(csvr.status_code, 200)
        self.assertTrue(csvr.text.startswith("benchmark,project,kind"))
        self.assertIn("bugsjar,commons-lang,jaguar2,1", csvr.text)
        self.assertEqual(c.post(f"/lote/{bid}/pausar", follow_redirects=False).status_code, 303)
        self.worker.pause.assert_called_once_with(bid)
        self.assertIn("lote", c.get("/lotes").text.lower())
        self.assertEqual(c.get("/lote/999").status_code, 404)
        bad = c.post("/lotes", data={"bugs": "", "kinds": ["maven"]})
        self.assertEqual(bad.status_code, 400)


class Recipes(RunsBase):
    def test_invalid_recipe_toml_rejected_pt(self):
        f = self.home.recipes / "bugsjar" / "commons-lang.toml"
        f.parent.mkdir(parents=True)
        f.write_text('[[recipe]]\nmatch = "7"\n', encoding="utf-8")
        c = self.authed()
        self.assertIn("bugsjar/commons-lang.toml", c.get("/receitas").text)
        r = c.post("/receitas", data={"f": "bugsjar/commons-lang.toml", "texto": "[[recipe\nmatch ="})
        self.assertEqual(r.status_code, 400)
        self.assertIn("TOML inválido", r.text)
        self.assertEqual(f.read_text(encoding="utf-8"), '[[recipe]]\nmatch = "7"\n')
        ok = c.post("/receitas", data={"f": "bugsjar/commons-lang.toml",
                                       "texto": '[[recipe]]\r\nmatch = "9"\r\nkind = "*"\r\n'})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(f.read_text(encoding="utf-8"), '[[recipe]]\nmatch = "9"\nkind = "*"\n')
        esc = c.post("/receitas", data={"f": "../config.toml", "texto": "a = 1"})
        self.assertEqual(esc.status_code, 400)


class Export(RunsBase):
    def test_export_dataset(self):
        self.make_run()
        c = self.authed()
        self.assertEqual(c.get("/exportar").status_code, 200)
        r = c.post("/exportar", data={"nome": "meu_ds"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue((self.home.datasets / "meu_ds" / "manifest.csv").exists())
        dup = c.post("/exportar", data={"nome": "meu_ds"})
        self.assertEqual(dup.status_code, 400)
        self.assertIn("Já existe", dup.text)


if __name__ == "__main__":
    unittest.main()
