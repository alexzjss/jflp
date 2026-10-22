"""Instância única do processador da fila (worker.lock), PID do dono da .partial e registro de processos."""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from sbfl import proc, store
from sbfl import queue as jq
from sbfl.adapters.base import Bug
from sbfl.home import Home, load_settings
from sbfl.instance import WorkerLock, pid_alive, process_start

from test_queue import Base, wait_for


class LockTests(Base):
    def test_second_lock_fails_until_release(self):
        a, b = WorkerLock(self.home), WorkerLock(self.home)
        self.assertTrue(a.acquire())
        self.assertFalse(b.acquire())
        a.release()
        self.assertTrue(b.acquire())
        b.release()

    def test_pid_alive(self):
        self.assertTrue(pid_alive(os.getpid(), process_start(os.getpid())))
        self.assertFalse(pid_alive(99999999))
        self.assertFalse(pid_alive(None))
        # PID reaproveitado: hora de início diferente da gravada
        self.assertFalse(pid_alive(os.getpid(), 12345.0))


class WorkerLockTests(Base):
    def test_worker_without_lock_does_not_recover_or_claim(self):
        bug = Bug("local", "p", "1", {})
        bid = jq.create_batch(self.home, "t", [bug], ["jaguar2"], {})
        with contextlib.closing(jq._connect(self.home)) as con, con:
            con.execute("UPDATE jobs SET state='running'")
        partial = store.RunWriter(self.home, bug, "jaguar2")
        partial.dir.joinpath(store._MANIFEST).write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        owner = WorkerLock(self.home)
        self.assertTrue(owner.acquire())
        calls = []
        with mock.patch.object(jq, "execute", lambda *a, **k: calls.append(1) or "rel/x"):
            w = self.worker()
            w.start()
            self.assertTrue(wait_for(lambda: w.status()["other_instance"]))
            time.sleep(0.5)
            self.assertEqual(calls, [])
            self.assertEqual(self.states(bid), ["running"])        # não foi resetado
            self.assertTrue(partial.dir.exists())                  # a .partial viva não foi recuperada
            owner.release()                                        # a outra instância saiu: assume
            self.assertTrue(self.batch_done(bid))
        self.assertEqual(calls, [1])
        self.assertFalse(w.status()["other_instance"])

    def test_worker_can_enqueue_while_other_instance_holds_lock(self):
        owner = WorkerLock(self.home)
        owner.acquire()
        self.addCleanup(owner.release)
        w = self.worker()
        w.start()
        self.assertTrue(wait_for(lambda: w.status()["other_instance"]))
        bid = jq.create_batch(self.home, "t", self.bugs(1), ["maven"], {})
        self.assertEqual(self.states(bid), ["pending"])

    def test_recovery_failure_does_not_loop_forever(self):
        calls = []

        def boom(home):
            calls.append(1)
            raise OSError("rename falhou")

        with mock.patch.object(store, "recover_partials", boom):
            w = self.worker()
            w.start()
            self.assertTrue(wait_for(lambda: w.status()["last_error"]))
            time.sleep(1.5)
        self.assertEqual(len(calls), 1)
        self.assertIn("rename falhou", w.status()["last_error"])

    def test_recover_removes_stale_work_copies(self):
        stale = self.home.work / "w" / "abcd1234"
        stale.mkdir(parents=True)
        (stale / "x.txt").write_text("x")
        w = self.worker()
        w.start()
        self.assertTrue(wait_for(lambda: not stale.exists()))


class PartialOwnerTests(Base):
    def _partial(self, pid, started=None):
        w = store.RunWriter(self.home, Bug("b", "p", "1"), "maven")
        man = json.loads((w.dir / store._MANIFEST).read_text(encoding="utf-8"))
        man.update({"pid": pid, "pid_started": started})
        (w.dir / store._MANIFEST).write_text(json.dumps(man), encoding="utf-8")
        return w

    def test_manifest_records_owner_pid_and_start(self):
        w = store.RunWriter(self.home, Bug("b", "p", "1"), "maven")
        man = json.loads((w.dir / store._MANIFEST).read_text(encoding="utf-8"))
        self.assertEqual(man["pid"], os.getpid())
        self.assertIn("pid_started", man)

    def test_partial_of_live_foreign_process_is_left_alone(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        w = self._partial(child.pid, process_start(child.pid))
        self.assertEqual(store.recover_partials(self.home), [])
        self.assertTrue(w.dir.exists())

    def test_partial_of_dead_process_is_recovered(self):
        self._partial(99999999)
        self.assertEqual(len(store.recover_partials(self.home)), 1)


class ProcRegistryTests(Base):
    SLEEP = [sys.executable, "-c", "import time; print('oi', flush=True); time.sleep(120)"]

    def test_kill_all_stops_live_process(self):
        out = {}
        t = threading.Thread(target=lambda: out.update(proc.run(self.SLEEP)))
        t.start()
        self.assertTrue(wait_for(lambda: proc.live_count() == 1))
        proc.kill_all()
        t.join(20)
        self.assertFalse(t.is_alive())
        self.assertEqual(proc.live_count(), 0)

    def test_worker_stop_kills_running_maven(self):
        started = threading.Event()

        def fake(home, settings, bug, kind, opts):
            started.set()
            proc.run(self.SLEEP)
            return "rel/x"

        bid = jq.create_batch(self.home, "t", self.bugs(1), ["maven"], {})
        with mock.patch.object(jq, "execute", fake):
            w = self.worker()
            w.start()
            self.assertTrue(started.wait(10))
            self.assertTrue(wait_for(lambda: proc.live_count() == 1))
            w.stop()
            self.assertTrue(wait_for(lambda: proc.live_count() == 0, 20))
            w.join(20)
        self.assertFalse(w.is_alive())
        # o job interrompido volta para a fila em vez de virar "done"
        self.assertEqual(self.states(bid), ["pending"])

    def test_abort_scope_refuses_new_processes(self):
        ev = threading.Event()
        proc.set_abort(ev)
        self.addCleanup(proc.set_abort, None)
        ev.set()
        with self.assertRaises(proc.Aborted):
            proc.run(self.SLEEP)


if __name__ == "__main__":
    unittest.main()
