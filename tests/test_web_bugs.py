from __future__ import annotations

import queue as _q
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

try:
    from fastapi.testclient import TestClient
    from sbfl.web.app import create_app
    HAVE_WEB = True
except ImportError:
    HAVE_WEB = False

from sbfl import queue as jq
from sbfl import store
from sbfl.adapters.base import Bug
from sbfl.home import Home, load_settings

TOKEN = "tok-de-teste"
BASE = "http://127.0.0.1:8123"
POMS = Path(__file__).parent / "fixtures" / "poms"


@unittest.skipUnless(HAVE_WEB, "fastapi/httpx não instalados")
class WebBugs(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = Home(self.tmp / "home").ensure()
        self.settings = load_settings(self.home)
        self.worker = mock.Mock()
        self.worker.status.return_value = {"alive": True, "last_error": None, "current": None}
        self.worker.current.return_value = None
        self.app = create_app(self.home, self.settings, self.worker, TOKEN, 8123)
        self.c = TestClient(self.app, base_url=BASE)
        self.c.cookies.set("jflp_token", TOKEN)

    def local_project(self, pom="parent_sniffer.xml") -> Path:
        d = self.tmp / "meu projeto"
        d.mkdir()
        shutil.copy(POMS / pom, d / "pom.xml")
        return d

    def register(self, d: Path) -> str:
        r = self.c.post("/bugs/local", data={"spec": str(d)}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        return r.headers["location"]

    def fake_run(self, bug: Bug, kind: str, status: str, started: str) -> str:
        rel = f"{bug.benchmark}/{bug.project}/{bug.bug_id}/{kind}-{started}"
        store._index(self.home, rel, {"benchmark": bug.benchmark, "project": bug.project,
                                      "bug_id": bug.bug_id, "kind": kind, "status": status,
                                      "started": started, "seconds": 12.0,
                                      "tests": {"total": 5, "failed": 1}})
        return rel

    def test_bug_page_lists_runs(self):
        loc = self.register(self.local_project())
        bug = next(iter(self.app.state.local_bugs.values()))
        rel = self.fake_run(bug, "maven", "ok", "2026-01-01T10:00:00")
        self.fake_run(bug, "jaguar2", "compilacao_falhou", "2026-01-02T10:00:00")
        r = self.c.get(loc)
        self.assertEqual(r.status_code, 200)
        self.assertIn(bug.key, r.text)
        self.assertIn(f"/run/{rel}", r.text)
        self.assertIn("compilacao_falhou", r.text)
        self.assertIn("Rodar Maven", r.text)
        self.assertIn("Revisar e rodar Jaguar 2", r.text)
        self.assertIn("Rodar Jaguar 1", r.text)

    def test_unknown_bug_is_404_in_portuguese(self):
        r = self.c.get("/bug/local/nada/dir-00000000")
        self.assertEqual(r.status_code, 404)
        self.assertIn("Bug não encontrado", r.text)

    def test_local_bug_rebuilt_from_stored_job(self):
        d = self.local_project()
        from sbfl.prepare import local_bug
        bug = local_bug(str(d))
        jq.create_batch(self.home, "t", [bug], ["maven"], {})
        r = self.c.get(f"/bug/{bug.key}")   # registro em memória vazio (app reiniciado)
        self.assertEqual(r.status_code, 200)

    def test_local_form_rejects_invalid(self):
        r = self.c.post("/bugs/local", data={"spec": "isto não existe"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Entrada local inválida", r.text)

    def test_pom_review_shows_detected_fixes(self):
        loc = self.register(self.local_project())
        r = self.c.get(f"{loc}/jaguar2")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Ignorar o animal-sniffer", r.text)
        self.assertIn("(detectada)", r.text)
        self.assertIn('value="skip-animal-sniffer" checked', r.text)
        self.assertIn('class="mais"', r.text)          # diff colorido
        self.assertIn("quebra a compilação", r.text)   # motivo

    def test_post_enqueues_job_with_fixes(self):
        loc = self.register(self.local_project())
        r = self.c.post(f"{loc}/jaguar2", data={
            "fix": ["source-dirs"], "detected": ["skip-animal-sniffer"],
            "scope": "all", "auto_search": "1"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        job = jq.list_jobs(self.home)[0]
        self.assertEqual(r.headers["location"], f"/job/{job['id']}/ao-vivo")
        self.assertEqual(job["kind"], "jaguar2")
        import json
        import sqlite3
        con = sqlite3.connect(str(self.home.db_path))
        opts = json.loads(con.execute("SELECT options FROM batches").fetchone()[0])
        con.close()
        self.assertEqual(opts["fixes_on"], ["source-dirs"])
        self.assertEqual(opts["fixes_off"], ["skip-animal-sniffer"])
        self.assertEqual(opts["test_scope"], "all")
        self.assertTrue(opts["auto_search"])
        self.assertIn("jaguar2", jq.batch_jobs(self.home, job["batch_id"])[0]["kind"])
        self.assertEqual(self.c.get(r.headers["location"]).status_code, 200)

    def test_rodar_maven_enqueues_and_redirects(self):
        loc = self.register(self.local_project())
        r = self.c.post(f"{loc}/rodar", data={"kind": "maven", "scope": "relevant"},
                        follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        job = jq.list_jobs(self.home)[0]
        self.assertEqual(job["kind"], "maven")
        self.assertIn(f"/job/{job['id']}/ao-vivo", r.headers["location"])

    def test_bugs_list_filter_and_unconfigured(self):
        bugs = [Bug("bugsjar", "commons", "a"), Bug("bugsjar", "commons", "b")]
        self.fake_run(bugs[0], "maven", "ok", "2026-01-01T10:00:00")
        self.fake_run(bugs[1], "maven", "jaguar_crash", "2026-01-01T11:00:00")
        with mock.patch("sbfl.web.routes_bugs.list_bugs", return_value=bugs):
            r = self.c.get("/bugs?bench=bugsjar&proj=commons")
            self.assertIn("/bug/bugsjar/commons/a", r.text)
            self.assertIn("/bug/bugsjar/commons/b", r.text)
            r = self.c.get("/bugs?bench=bugsjar&proj=commons&pendentes=1")
            self.assertNotIn("/bug/bugsjar/commons/a", r.text)
            self.assertIn("/bug/bugsjar/commons/b", r.text)
        r = self.c.get("/bugs?bench=bugsjar&proj=commons")   # sem Bugs.jar configurado
        self.assertEqual(r.status_code, 200)
        self.assertIn("Nenhum projeto", r.text)

    def test_sse_stream_yields_lines(self):
        bug = Bug("bugsjar", "commons", "1")
        bid = jq.create_batch(self.home, "t", [bug], ["maven"], {})
        job = jq.batch_jobs(self.home, bid)[0]
        import contextlib
        with contextlib.closing(jq._connect(self.home)) as con, con:
            con.execute("UPDATE jobs SET state='running' WHERE id=?", (job["id"],))
        home = self.home

        class Fila(_q.Queue):   # como o worker: grava o resultado e só então publica o sentinela
            def get(self, block=True, timeout=None):
                item = super().get(block, timeout)
                if item is None:
                    with contextlib.closing(jq._connect(home)) as con, con:
                        con.execute("UPDATE jobs SET state='done', run_rel='bugsjar/commons/1/x' "
                                    "WHERE id=?", (job["id"],))
                return item

        q = Fila()
        self.worker.current.return_value = {"job_id": job["id"], "log": ["linha antiga"]}
        self.worker.subscribe.return_value = q
        q.put("linha nova")
        q.put(None)
        r = self.c.get(f"/job/{job['id']}/stream")
        self.assertEqual(r.headers["content-type"].split(";")[0], "text/event-stream")
        body = r.text
        self.assertLess(body.index("data: linha antiga"), body.index("data: linha nova"))
        self.assertIn("event: fim\ndata: bugsjar/commons/1/x", body)
        self.worker.unsubscribe.assert_called_once_with(q)

    def test_sse_done_job_ends_immediately(self):
        bug = Bug("bugsjar", "commons", "2")
        bid = jq.create_batch(self.home, "t", [bug], ["maven"], {})
        job = jq.batch_jobs(self.home, bid)[0]
        import contextlib
        with contextlib.closing(jq._connect(self.home)) as con, con:
            con.execute("UPDATE jobs SET state='done', run_rel='r/p/1/z' WHERE id=?", (job["id"],))
        r = self.c.get(f"/job/{job['id']}/stream")
        self.assertEqual(r.text, "event: fim\ndata: r/p/1/z\n\n")
        self.worker.subscribe.assert_not_called()

    def test_sse_requires_token(self):
        anon = TestClient(self.app, base_url=BASE)
        self.assertEqual(anon.get("/job/1/stream").status_code, 403)
        self.assertEqual(anon.get("/bugs").status_code, 403)


if __name__ == "__main__":
    unittest.main()
