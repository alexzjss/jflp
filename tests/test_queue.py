from __future__ import annotations

import contextlib
import queue as stdlib_queue  # a stdlib continua acessível
import shutil
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

    def test_slow_subscriber_is_dropped(self):
        w = self.worker()
        q = w.subscribe()
        for i in range(jq.SUB_MAX + 5):
            w._on_line(f"l{i}")
        self.assertNotIn(q, w._subs)
        self.assertEqual(len(w._log), jq.LOG_LINES)


if __name__ == "__main__":
    unittest.main()
