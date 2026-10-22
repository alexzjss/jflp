from __future__ import annotations

import contextlib
import queue as stdlib_queue  # a stdlib continua acessível
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from sbfl import queue as jq
from sbfl.adapters.base import Bug
from sbfl.home import Home, load_settings
from sbfl.store import RunWriter, list_runs


def wait_for(cond, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = Home(self.tmp / "Pasta Ção").ensure()
        self.settings = load_settings(self.home)

    def worker(self):
        w = jq.Worker(self.home, self.settings)
        self.addCleanup(self._stop, w)
        return w

    @staticmethod
    def _stop(w):
        w.stop()
        if w.is_alive():
            w.join(5)

    @staticmethod
    def bugs(n):
        return [Bug("local", "p", str(i), {"dir": f"/x/{i}"}) for i in range(n)]

    def states(self, bid):
        return [j["state"] for j in jq.batch_status(self.home, bid)["jobs"]]

    def batch_done(self, bid):
        return wait_for(lambda: jq.batch_status(self.home, bid)["state"] == "done")


class QueueTests(Base):
    def test_stdlib_queue_still_importable(self):
        self.assertTrue(hasattr(stdlib_queue, "Queue"))

    def test_worker_processes_in_order(self):
        calls = []

        def fake(home, settings, bug, kind, opts):
            calls.append((bug.bug_id, kind, bug.extra, opts.test_scope))
            opts.log_cb(f"linha {bug.bug_id}")
            return f"rel/{bug.bug_id}"

        bid = jq.create_batch(self.home, "t", self.bugs(3), ["jaguar2"], {"test_scope": "all"})
        with mock.patch.object(jq, "execute", fake):
            w = self.worker()
            sub = w.subscribe()
            w.start()
            self.assertTrue(self.batch_done(bid))
        self.assertEqual([c[0] for c in calls], ["0", "1", "2"])
        self.assertEqual(calls[1][2:], ({"dir": "/x/1"}, "all"))   # bug local reconstruído do extra
        st = jq.batch_status(self.home, bid)
        self.assertEqual(st["counts"]["done"], 3)
        self.assertEqual([j["run_rel"] for j in st["jobs"]], ["rel/0", "rel/1", "rel/2"])
        lines = []
        while not sub.empty():
            lines.append(sub.get_nowait())
        self.assertEqual(lines.count(None), 3)
        self.assertIn("linha 0", lines)

    def test_execute_exception_marks_done_with_error(self):
        def boom(home, settings, bug, kind, opts):
            if bug.bug_id == "0":
                raise RuntimeError("falhou")
            return "rel/ok"

        bid = jq.create_batch(self.home, "t", self.bugs(2), ["jaguar2"], {})
        with mock.patch.object(jq, "execute", boom):
            self.worker().start()
            self.assertTrue(self.batch_done(bid))
        jobs = jq.batch_status(self.home, bid)["jobs"]
        self.assertIn("falhou", jobs[0]["error"])
        self.assertEqual(jobs[1]["run_rel"], "rel/ok")

    def test_resume_after_crash(self):
        bug = Bug("local", "p", "7", {})
        bid = jq.create_batch(self.home, "t", [bug], ["jaguar2"], {})
        # simula morte no meio: job `running` e .partial órfã
        with contextlib.closing(jq._connect(self.home)) as con, con:
            con.execute("UPDATE jobs SET state='running'")
        orphan_rel = RunWriter(self.home, bug, "jaguar2").rel
        runs = []

        def fake(home, settings, b, kind, opts):
            runs.append(b.bug_id)
            return "rel/novo"

        with mock.patch.object(jq, "execute", fake):
            self.worker().start()
            self.assertTrue(self.batch_done(bid))
        self.assertEqual(runs, ["7"])
        found = {r["path"]: r for r in list_runs(self.home)}
        self.assertEqual(found[orphan_rel]["status"], "interrompido")

    def test_pause_and_cancel(self):
        gate, started = threading.Event(), threading.Event()
        calls = []

        def fake(home, settings, bug, kind, opts):
            calls.append(bug.bug_id)
            started.set()
            gate.wait(10)
            return "rel/" + bug.bug_id

        bid = jq.create_batch(self.home, "t", self.bugs(3), ["jaguar2"], {})
        with mock.patch.object(jq, "execute", fake):
            w = self.worker()
            w.start()
            self.assertTrue(started.wait(10))
            w.pause(bid)
            gate.set()      # o job corrente termina, mas nada novo é pego
            self.assertTrue(wait_for(lambda: self.states(bid)[0] == "done"))
            time.sleep(1.5)
            self.assertEqual(calls, ["0"])
            self.assertEqual(self.states(bid), ["done", "pending", "pending"])
            self.assertEqual(jq.batch_status(self.home, bid)["state"], "paused")
            gate.clear()
            w.resume(bid)
            self.assertTrue(wait_for(lambda: len(calls) == 2))   # job 1 em andamento
            w.cancel(bid)
            gate.set()
            self.assertTrue(wait_for(lambda: self.states(bid)[1] == "done"))
            time.sleep(1.5)
        self.assertEqual(calls, ["0", "1"])
        self.assertEqual(self.states(bid), ["done", "done", "cancelled"])
        self.assertEqual(jq.batch_status(self.home, bid)["state"], "cancelled")
        self.assertNotIn("pending", self.states(bid))
        self.assertIn("cancelled", self.states(bid))

    def test_cancel_between_select_and_claim_never_runs_job(self):
        calls = []
        bid = jq.create_batch(self.home, "t", self.bugs(1), ["jaguar2"], {})
        w = self.worker()
        w._after_select = lambda: w.cancel(bid)   # cancela entre o SELECT e o UPDATE
        with mock.patch.object(jq, "execute", lambda *a, **k: calls.append(1) or "rel/x"):
            self.assertIsNone(w._next_job())
        self.assertEqual(calls, [])
        self.assertEqual(self.states(bid), ["cancelled"])

    def test_crash_recovery_cancelled_batch_running_job(self):
        bid = jq.create_batch(self.home, "t", self.bugs(2), ["jaguar2"], {})
        with contextlib.closing(jq._connect(self.home)) as con, con:
            con.execute("UPDATE jobs SET state='running' WHERE idx=0")
            con.execute("UPDATE jobs SET state='cancelled' WHERE idx=1")
            con.execute("UPDATE batches SET state='cancelled'")
        calls = []
        with mock.patch.object(jq, "execute", lambda *a, **k: calls.append(1) or "rel/x"):
            w = self.worker()
            w.start()
            self.assertTrue(wait_for(lambda: self.states(bid) == ["cancelled", "cancelled"]))
            time.sleep(1.2)
        self.assertEqual(calls, [])
        self.assertTrue(jq.batch_status(self.home, bid)["finished"])

    def test_keep_awake_failure_does_not_stop_processing(self):
        @contextlib.contextmanager
        def bad():
            raise OSError("sem energia")
            yield

        bid = jq.create_batch(self.home, "t", self.bugs(2), ["jaguar2"], {})
        with mock.patch.object(jq, "keep_awake", bad),                 mock.patch.object(jq, "execute", lambda *a, **k: "rel/ok"):
            w = self.worker()
            w.start()
            self.assertTrue(self.batch_done(bid))
        self.assertIn("sem energia", w.status()["last_error"])

    def test_loop_survives_db_error(self):
        bid = jq.create_batch(self.home, "t", self.bugs(1), ["jaguar2"], {})
        real = jq.Worker._next_job
        n = {"i": 0}

        def flaky(self_):
            n["i"] += 1
            if n["i"] == 1:
                raise RuntimeError("banco travado")
            return real(self_)

        with mock.patch.object(jq.Worker, "_next_job", flaky),                 mock.patch.object(jq, "execute", lambda *a, **k: "rel/ok"):
            w = self.worker()
            w.start()
            self.assertTrue(self.batch_done(bid))
        self.assertIn("banco travado", w.status()["last_error"])

    def test_slow_subscriber_is_dropped(self):
        w = self.worker()
        q = w.subscribe()
        for i in range(jq.SUB_MAX + 5):
            w._on_line(f"l{i}")
        self.assertNotIn(q, w._subs)
        self.assertEqual(len(w._log), jq.LOG_LINES)


def blocking_execute(started: threading.Event):
    """Runner falso: grava uma execução e bloqueia num processo real até o worker matá-lo."""
    from sbfl import proc, status as st
    from sbfl.store import RunWriter as RW

    def fake(home, settings, bug, kind, opts):
        with RW(home, bug, kind) as w:
            (home.work / "w" / "copia").mkdir(parents=True, exist_ok=True)
            started.set()
            try:
                proc.run([sys.executable, "-c", "import time; time.sleep(60)"])
                w.finish({"status": "ok"})
            except proc.Aborted:
                w.finish({"status": st.INTERROMPIDO})
        return w.rel
    return fake


class CancelTests(Base):
    def runs_on_disk(self):
        return [p for p in self.home.runs.rglob("*") if p.is_dir() and len(p.relative_to(self.home.runs).parts) == 4]

    def test_cancel_running_discards_run_and_continues(self):
        started, calls = threading.Event(), []
        slow = blocking_execute(started)

        def fake(home, settings, bug, kind, opts):
            calls.append(bug.bug_id)
            if bug.bug_id == "0":
                return slow(home, settings, bug, kind, opts)
            return "rel/" + bug.bug_id

        bid = jq.create_batch(self.home, "t", self.bugs(2), ["jaguar2"], {})
        with mock.patch.object(jq, "execute", fake):
            w = self.worker()
            w.start()
            self.assertTrue(started.wait(10))
            job_id = w.current()["job_id"]
            t0 = time.monotonic()
            self.assertEqual(w.cancel_job(job_id), "andamento")
            self.assertTrue(self.batch_done(bid))
            self.assertLess(time.monotonic() - t0, 10)      # o processo foi morto, não esperou 60 s
        self.assertEqual(self.states(bid), ["cancelled", "done"])
        j0 = jq.get_job(self.home, job_id)
        self.assertIsNone(j0["run_rel"])
        self.assertEqual(self.runs_on_disk(), [])
        self.assertEqual(list_runs(self.home), [])
        self.assertEqual(list((self.home.runs).rglob("*.partial")), [])
        self.assertEqual(list((self.home.work / "w").iterdir()), [])
        self.assertEqual(calls, ["0", "1"])

    def test_shutdown_still_requeues(self):
        started = threading.Event()
        bid = jq.create_batch(self.home, "t", self.bugs(1), ["jaguar2"], {})
        with mock.patch.object(jq, "execute", blocking_execute(started)):
            w = self.worker()
            w.start()
            self.assertTrue(started.wait(10))
            w.stop()
            w.join(10)
        self.assertEqual(self.states(bid), ["pending"])

    def test_cancel_job_not_running_returns_empty(self):
        bid = jq.create_batch(self.home, "t", self.bugs(1), ["jaguar2"], {})
        w = self.worker()
        jid = jq.batch_jobs(self.home, bid)[0]["id"]
        self.assertEqual(w.cancel_job(jid), "pendente")
        self.assertEqual(w.cancel_job(jid), "")
        self.assertEqual(w.cancel_job(9999), "")
        self.assertEqual(jq.batch_status(self.home, bid)["state"], "done")   # nada sobrou: lote fechado

    def test_remove_pending_races_with_claim(self):
        calls = []
        bid = jq.create_batch(self.home, "t", self.bugs(2), ["jaguar2"], {})
        jid = jq.batch_jobs(self.home, bid)[0]["id"]
        w = self.worker()
        w._after_select = lambda: w.remove_pending(jid)    # remove entre o SELECT e a reivindicação
        with mock.patch.object(jq, "execute", lambda *a, **k: calls.append(1) or "rel/x"):
            self.assertIsNone(w._next_job())
        self.assertEqual(calls, [])
        self.assertEqual(self.states(bid), ["cancelled", "pending"])
        self.assertFalse(w.remove_pending(jid))
        w._after_select = lambda: None
        picked = w._next_job()                              # o outro job segue na fila
        self.assertEqual(picked[0]["idx"], 1)
        self.assertFalse(w.remove_pending(picked[0]["id"]))  # já reivindicado: não dá mais

    def test_cancel_pending_and_everything(self):
        started = threading.Event()
        b1 = jq.create_batch(self.home, "a", self.bugs(2), ["jaguar2"], {})
        b2 = jq.create_batch(self.home, "b", self.bugs(1), ["jaguar2"], {})
        with mock.patch.object(jq, "execute", blocking_execute(started)):
            w = self.worker()
            w.start()
            self.assertTrue(started.wait(10))
            self.assertEqual(w.cancel_everything(), 3)      # 2 pendentes + o corrente
            self.assertTrue(wait_for(lambda: w.current() is None and self.states(b1)[0] == "cancelled"))
        self.assertEqual(self.states(b1), ["cancelled", "cancelled"])
        self.assertEqual(self.states(b2), ["cancelled"])
        self.assertEqual(self.runs_on_disk(), [])

    def test_delete_batch(self):
        done = jq.create_batch(self.home, "ok", self.bugs(1), ["jaguar2"], {})
        busy = jq.create_batch(self.home, "ativo", self.bugs(1), ["jaguar2"], {})
        w = self.worker()
        with self.assertRaises(ValueError):
            jq.delete_batch(self.home, busy)                # pendente: recusa
        with contextlib.closing(sqlite3.connect(str(self.home.db_path))) as con, con:
            con.execute("UPDATE jobs SET state='running' WHERE batch_id=?", (busy,))
        w.cancel(busy)
        with self.assertRaises(ValueError):
            jq.delete_batch(self.home, busy)                # ainda em andamento: recusa
        with contextlib.closing(sqlite3.connect(str(self.home.db_path))) as con, con:
            con.execute("UPDATE jobs SET state='done', run_rel='a/b/c/d' WHERE batch_id=?", (busy,))
        jq.delete_batch(self.home, busy)
        self.assertEqual([b["id"] for b in jq.list_batches(self.home)], [done])
        self.assertEqual(jq.list_jobs(self.home, limit=10)[0]["batch_id"], done)
        with self.assertRaises(ValueError):
            jq.delete_batch(self.home, busy)                # já não existe

    def test_pending_jobs_listing(self):
        a = jq.create_batch(self.home, "a", self.bugs(2), ["jaguar2"], {})
        b = jq.create_batch(self.home, "b", self.bugs(1), ["jaguar2"], {})
        w = self.worker()
        w.pause(b)
        w.cancel(a)
        pj = jq.pending_jobs(self.home)
        self.assertEqual([(j["batch_name"], j["batch_state"]) for j in pj], [("b", "paused")])


if __name__ == "__main__":
    unittest.main()
