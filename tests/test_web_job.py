from __future__ import annotations

import queue as _q
import shutil
import tempfile
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
from sbfl.adapters.base import Bug
from sbfl.home import Home, load_settings

TOKEN = "tok-de-teste"
BASE = "http://127.0.0.1:8123"
POMS = Path(__file__).parent / "fixtures" / "poms"


@unittest.skipUnless(HAVE_WEB, "fastapi/httpx não instalados")
class WebJob(unittest.TestCase):
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

    def test_job_lookup_queries_a_single_job(self):
        from sbfl.web import routes_job as routes_bugs
        bid = jq.create_batch(self.home, "t", [Bug("bugsjar", "commons", "1")], ["maven"], {})
        job = jq.batch_jobs(self.home, bid)[0]
        with mock.patch.object(jq, "list_jobs", side_effect=AssertionError("não deve listar tudo")):
            self.assertEqual(routes_bugs._job(self.home, job["id"])["id"], job["id"])
            self.assertIsNone(routes_bugs._job(self.home, 999999))

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
        self.assertEqual(anon.get("/resultados").status_code, 403)

    # ---- correções da revisão ----
    def running_job(self):
        bid = jq.create_batch(self.home, "t", [Bug("bugsjar", "commons", "9")], ["maven"], {})
        job = jq.batch_jobs(self.home, bid)[0]
        self.set_state(job["id"], "running")
        return job

    def set_state(self, job_id, state, rel=None):
        import contextlib
        with contextlib.closing(jq._connect(self.home)) as con, con:
            con.execute("UPDATE jobs SET state=?, run_rel=? WHERE id=?", (state, rel, job_id))

    def test_sse_waits_while_worker_not_yet_current(self):
        from sbfl.web import routes_job as routes_bugs
        job = self.running_job()
        q = _q.Queue()
        q.put("dentro")
        q.put(None)
        calls = []

        def cur():
            calls.append(1)
            if len(calls) < 3:
                return None            # janela de reivindicação: job running, worker ainda não o tem
            self.set_state(job["id"], "done", "r/p/9/x")
            return {"job_id": job["id"], "log": []}

        self.worker.current.side_effect = cur
        self.worker.subscribe.return_value = q
        with mock.patch.object(routes_bugs, "START_POLL", 0.01):
            body = self.c.get(f"/job/{job['id']}/stream").text
        self.assertGreaterEqual(len(calls), 3)
        self.assertIn("data: dentro", body)
        self.assertIn("event: fim\ndata: r/p/9/x", body)
        self.assertEqual(body.count("event: fim"), 1)

    def test_sse_ends_when_subscriber_dropped(self):
        job = self.running_job()
        outer = self

        class Fila(_q.Queue):          # assinante descartado: nunca recebe o sentinela
            def get(self, block=True, timeout=None):
                outer.set_state(job["id"], "done", "r/p/9/y")
                raise _q.Empty

        self.worker.current.return_value = {"job_id": job["id"], "log": []}
        self.worker.subscribe.return_value = Fila()
        body = self.c.get(f"/job/{job['id']}/stream").text
        self.assertIn("event: fim\ndata: r/p/9/y", body)

    def test_sse_log_line_cannot_inject_events(self):
        from sbfl.web.routes_job import _sse
        out = _sse(None, "x\revent: fim\r\nid: 1\nok")
        self.assertEqual(out, "data: x\ndata: event: fim\ndata: id: 1\ndata: ok\n\n")
        job = self.running_job()
        outer = self

        class Fila(_q.Queue):
            def get(self, block=True, timeout=None):
                outer.set_state(job["id"], "done", "r/p/9/z")
                return None

        self.worker.current.return_value = {"job_id": job["id"], "log": ["x\revent: fim"]}
        self.worker.subscribe.return_value = Fila()
        body = self.c.get(f"/job/{job['id']}/stream").text
        self.assertEqual(body.count("event: fim"), 2)    # a linha vira "data: event: fim" + o fim real
        self.assertTrue(body.startswith("data: x\ndata: event: fim\n\n"))
        self.assertTrue(body.endswith("event: fim\ndata: r/p/9/z\n\n"))

if __name__ == "__main__":
    unittest.main()

