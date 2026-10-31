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
            copia = home.work / "w" / f"copia-{bug.bug_id}-{kind}"
            copia.mkdir(parents=True, exist_ok=True)
            started.set()
            try:
                proc.run([sys.executable, "-c", "import time; time.sleep(60)"])
                w.finish({"status": "ok"})
            except proc.Aborted:
                w.finish({"status": st.INTERROMPIDO})
            finally:
                shutil.rmtree(copia, ignore_errors=True)    # o `execute` de verdade remove a própria cópia
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


class ProgressTests(Base):
    def test_current_exposes_progress_fed_by_lines(self):
        seen, release = threading.Event(), threading.Event()

        def fake(home, settings, bug, kind, opts):
            for ln in ("# jflp tentativa 1", "[INFO] --- maven-surefire-plugin:2.14.1:test (t) @ a ---",
                       "[INFO] Tests run: 5, Failures: 1, Errors: 0, Skipped: 0, Time elapsed: 0.1 s - in a.BTest"):
                opts.log_cb(ln)
            seen.set()
            release.wait(10)
            return "rel/x"

        bid = jq.create_batch(self.home, "t", self.bugs(1), ["maven"], {})
        with mock.patch.object(jq, "execute", fake):
            w = self.worker()
            w.start()
            self.assertTrue(seen.wait(10))
            cur = w.current()
            p = cur["progress"]
            self.assertEqual((p["tentativa"], p["fase"], p["testes"]["rodados"], p["testes"]["falhas"]),
                             (1, "rodando testes", 5, 1))
            self.assertEqual(w.progress_of(cur["job_id"])["testes"]["rodados"], 5)
            job_id = cur["job_id"]
            release.set()
            self.assertTrue(self.batch_done(bid))
        self.assertTrue(wait_for(lambda: w.current() is None))
        final = w.progress_of(job_id)           # o estado final fica disponível depois do job
        self.assertEqual(final["etapa"], "fim")
        self.assertIsNone(w.progress_of(job_id + 1))

    def test_progress_resets_for_each_job(self):
        def fake(home, settings, bug, kind, opts):
            opts.log_cb("[INFO] Tests run: 3, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 1 s - in a.T")
            return "rel/" + bug.bug_id

        bid = jq.create_batch(self.home, "t", self.bugs(2), ["maven"], {})
        with mock.patch.object(jq, "execute", fake):
            w = self.worker()
            w.start()
            self.assertTrue(self.batch_done(bid))
        ids = [j["id"] for j in jq.batch_jobs(self.home, bid)]
        self.assertEqual(w.progress_of(ids[-1])["testes"]["rodados"], 3)   # não acumulou os 6


# -- WorkerPool: vários workers em paralelo --

class PoolBase(Base):
    def pool(self, n):
        p = jq.WorkerPool(self.home, self.settings, n)
        self.addCleanup(self._stop_pool, p)
        return p

    @staticmethod
    def _stop_pool(p):
        p.stop()
        p.join(5)

    def gauge(self, hold: float = 0.05):
        """Execute falso que mede quantos jobs rodam ao mesmo tempo."""
        lock, state = threading.Lock(), {"now": 0, "max": 0}

        def fake(home, settings, bug, kind, opts):
            with lock:
                state["now"] += 1
                state["max"] = max(state["max"], state["now"])
            time.sleep(hold)
            with lock:
                state["now"] -= 1
            return f"rel/{bug.bug_id}/{kind}"
        return fake, state


class PoolTests(PoolBase):
    def test_pool_roda_em_paralelo(self):
        started = {"0": threading.Event(), "1": threading.Event()}
        release = threading.Event()

        def fake(home, settings, bug, kind, opts):
            started[bug.bug_id].set()
            release.wait(15)
            return "rel/" + bug.bug_id

        bid = jq.create_batch(self.home, "t", self.bugs(2), ["maven"], {})
        with mock.patch.object(jq, "execute", fake):
            pool = self.pool(2)
            pool.start()
            try:
                self.assertTrue(started["0"].wait(10) and started["1"].wait(10))   # os dois ao mesmo tempo
                self.assertTrue(wait_for(lambda: len(pool.currents()) == 2))
                self.assertEqual(sorted(c["index"] for c in pool.currents()), [0, 1])
                self.assertEqual(pool.status()["workers"], 2)
            finally:
                release.set()
            self.assertTrue(self.batch_done(bid))
        self.assertEqual(self.states(bid), ["done", "done"])

    def test_pool_nunca_dois_jobs_do_mesmo_bug(self):
        fake, state = self.gauge()
        bid = jq.create_batch(self.home, "t", self.bugs(1), ["maven", "jaguar2", "jaguar1"], {})
        with mock.patch.object(jq, "execute", fake):
            self.pool(3).start()
            self.assertTrue(self.batch_done(bid))
        self.assertEqual(state["max"], 1)
        self.assertEqual(self.states(bid), ["done"] * 3)

    def test_jaguar1_serializado_sem_port_aware(self):
        fake, state = self.gauge()
        bid = jq.create_batch(self.home, "t", self.bugs(3), ["jaguar1"], {})
        with mock.patch.object(jq, "execute", fake):
            self.pool(3).start()
            self.assertTrue(self.batch_done(bid))
        self.assertEqual(state["max"], 1)

    def test_jaguar1_paralelo_com_port_aware(self):
        from types import SimpleNamespace
        from sbfl import jaguar
        self.settings.set("jaguar1", "port_aware", True)
        ports, ready = {}, threading.Barrier(2, timeout=10)

        def fake(home, settings, bug, kind, opts):
            cmd = jaguar.build_cmd(
                SimpleNamespace(lib="/lib", java="java", jvm_args=[], output_type="xml", log_level="INFO"),
                SimpleNamespace(classes_dir="c", tests_dir="t", extra_cp=[]), Path("tf"), "out", "ochiai")
            ports[bug.bug_id] = next(c for c in cmd if c.startswith("-javaagent"))
            ready.wait()                  # só passa se os dois jobs estiverem rodando juntos
            return "rel/" + bug.bug_id

        bid = jq.create_batch(self.home, "t", self.bugs(2), ["jaguar1"], {})
        with mock.patch.object(jq, "execute", fake):
            self.pool(2).start()
            self.assertTrue(self.batch_done(bid))
        self.assertEqual(len(ports), 2)
        self.assertEqual(len(set(ports.values())), 2)       # uma porta por worker
        self.assertTrue(all("port=63" in v for v in ports.values()))

    def test_build_cmd_porta_padrao(self):
        from types import SimpleNamespace
        from sbfl import jaguar
        jaguar.set_port(None)
        cmd = jaguar.build_cmd(
            SimpleNamespace(lib="/lib", java="java", jvm_args=[], output_type="xml", log_level="INFO"),
            SimpleNamespace(classes_dir="c", tests_dir="t", extra_cp=[]), Path("tf"), "out", "ochiai")
        self.assertTrue(any(c.endswith("port=6300") for c in cmd))
        self.assertFalse(any(c.startswith("-Djaguar.port") for c in cmd))

    def test_interromper_devolve_a_fila(self):
        started = threading.Event()
        bid = jq.create_batch(self.home, "t", self.bugs(1), ["jaguar2"], {})
        with mock.patch.object(jq, "execute", blocking_execute(started)):
            pool = self.pool(2)
            pool.start()
            self.assertTrue(started.wait(10))
            pool.pause(bid)                  # senão o worker pegaria o job de volta na hora
            t0 = time.monotonic()
            self.assertEqual(pool.interromper(), 1)
            self.assertTrue(wait_for(lambda: self.states(bid) == ["pending"]))
            self.assertLess(time.monotonic() - t0, 10)
        self.assertEqual(list_runs(self.home), [])
        self.assertEqual(list(self.home.runs.rglob("*.partial")), [])
        self.assertIsNone(jq.batch_jobs(self.home, bid)[0]["run_rel"])

    def test_reabrir_recupera_running(self):
        bid = jq.create_batch(self.home, "t", self.bugs(1), ["jaguar2"], {})
        with contextlib.closing(jq._connect(self.home)) as con, con:
            con.execute("UPDATE jobs SET state='running'")
        calls = []

        def fake(home, settings, bug, kind, opts):
            calls.append(bug.bug_id)
            return "rel/novo"

        with mock.patch.object(jq, "execute", fake):
            self.pool(3).start()
            self.assertTrue(self.batch_done(bid))
        self.assertEqual(calls, ["0"])

    def test_start_cria_a_pasta_de_trabalho(self):
        import dataclasses
        alvo = self.tmp / "nova" / "pasta"
        pool = jq.WorkerPool(dataclasses.replace(self.home, work_root=alvo), self.settings, 1)
        self.addCleanup(self._stop_pool, pool)
        pool.start()
        self.assertTrue(alvo.is_dir())

    def test_cancelar_job_em_andamento_no_pool(self):
        started = threading.Event()
        bid = jq.create_batch(self.home, "t", self.bugs(1), ["jaguar2"], {})
        with mock.patch.object(jq, "execute", blocking_execute(started)):
            pool = self.pool(2)
            pool.start()
            self.assertTrue(started.wait(10))
            jid = pool.current()["job_id"]
            self.assertEqual(pool.cancel_job(jid), "andamento")
            self.assertTrue(self.batch_done(bid))
        self.assertEqual(self.states(bid), ["cancelled"])

    def test_add_worktree_serializado(self):
        from sbfl import gitutil
        lock, state = threading.Lock(), {"now": 0, "max": 0}
        barrier = threading.Barrier(2, timeout=10)

        def fake_git(repo, *args, check=True):
            with lock:
                state["now"] += 1
                state["max"] = max(state["max"], state["now"])
            time.sleep(0.1)
            with lock:
                state["now"] -= 1
            return ""

        def work(i):
            barrier.wait()
            gitutil.add_worktree(self.tmp / "repo", self.tmp / f"wt{i}", "HEAD")

        with mock.patch.object(gitutil, "git", fake_git), \
                mock.patch.object(gitutil.subprocess, "run", lambda *a, **k: mock.Mock(returncode=0)):
            ts = [threading.Thread(target=work, args=(i,)) for i in range(2)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(15)
        self.assertEqual(state["max"], 1)

    def test_remove_pristine_usa_a_trava_do_repo(self):
        from sbfl import prepare
        lock, state = threading.Lock(), {"now": 0, "max": 0}
        barrier = threading.Barrier(2, timeout=10)
        repo = self.tmp / "repo"
        repo.mkdir()

        def fake_git(r, *args, check=True):
            with lock:
                state["now"] += 1
                state["max"] = max(state["max"], state["now"])
            time.sleep(0.1)
            with lock:
                state["now"] -= 1
            return ""

        def work(i):
            dest = self.tmp / f"p{i}"
            dest.mkdir()
            barrier.wait()
            prepare._remove_pristine(dest, str(repo))

        with mock.patch.object(prepare, "git", fake_git):
            ts = [threading.Thread(target=work, args=(i,)) for i in range(2)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(15)
        self.assertEqual(state["max"], 1)


class PoolResizeTests(PoolBase):
    def test_aumentar_inicia_workers_novos_que_pegam_jobs(self):
        started = {str(i): threading.Event() for i in range(3)}
        release = threading.Event()

        def fake(home, settings, bug, kind, opts):
            started[bug.bug_id].set()
            release.wait(15)
            return "rel/" + bug.bug_id

        bid = jq.create_batch(self.home, "t", self.bugs(3), ["maven"], {})
        with mock.patch.object(jq, "execute", fake):
            pool = self.pool(1)
            pool.start()
            try:
                self.assertTrue(started["0"].wait(10))
                time.sleep(0.4)
                self.assertFalse(started["1"].is_set())       # um worker: os outros esperam
                self.assertEqual(pool.set_workers(3), 3)
                self.assertTrue(started["1"].wait(10) and started["2"].wait(10))
                st = pool.status()
                self.assertEqual((st["workers"], st["workers_ativos"]), (3, 3))
                self.assertEqual(sorted(c["index"] for c in pool.currents()), [0, 1, 2])
            finally:
                release.set()
            self.assertTrue(self.batch_done(bid))

    def test_limites_1_a_12(self):
        pool = self.pool(2)
        self.assertEqual(pool.set_workers(99), 12)
        self.assertEqual(pool.set_workers(0), 1)
        self.assertEqual(pool.set_workers("x"), 1)

    def test_diminuir_deixa_job_em_andamento_terminar_e_o_worker_sai(self):
        started = {str(i): threading.Event() for i in range(3)}
        release = {str(i): threading.Event() for i in range(3)}

        def fake(home, settings, bug, kind, opts):
            started[bug.bug_id].set()
            release[bug.bug_id].wait(15)
            return "rel/" + bug.bug_id

        bid = jq.create_batch(self.home, "t", self.bugs(3), ["maven"], {})
        with mock.patch.object(jq, "execute", fake):
            pool = self.pool(3)
            pool.start()
            try:
                self.assertTrue(all(e.wait(10) for e in started.values()))
                self.assertEqual(pool.set_workers(1), 1)
                time.sleep(0.5)
                st = pool.status()
                self.assertEqual((st["workers"], st["workers_ativos"]), (1, 3))   # nada foi morto
                self.assertEqual(len(pool.currents()), 3)
                self.assertEqual(self.states(bid), ["running"] * 3)
                for e in release.values():
                    e.set()
                self.assertTrue(self.batch_done(bid))
                self.assertTrue(wait_for(lambda: pool.status()["workers_ativos"] == 1))
            finally:
                for e in release.values():
                    e.set()
        self.assertEqual(self.states(bid), ["done"] * 3)

    def test_redimensionar_nunca_roda_dois_jobs_do_mesmo_bug(self):
        fake, state = self.gauge(0.05)
        bid = jq.create_batch(self.home, "t", self.bugs(1), ["maven", "jaguar2", "jaguar1"], {})
        with mock.patch.object(jq, "execute", fake):
            pool = self.pool(4)
            pool.set_workers(2)             # antes de iniciar
            pool.start()
            pool.set_workers(3)
            pool.set_workers(1)
            self.assertTrue(self.batch_done(bid))
            self.assertTrue(wait_for(lambda: pool.status()["workers_ativos"] == 1))
        self.assertEqual(state["max"], 1)
        self.assertEqual(self.states(bid), ["done"] * 3)

    def test_voltar_a_aumentar_depois_de_diminuir(self):
        fake, _ = self.gauge(0.01)
        with mock.patch.object(jq, "execute", fake):
            pool = self.pool(3)
            pool.start()
            pool.set_workers(1)
            self.assertTrue(wait_for(lambda: pool.status()["workers_ativos"] == 1))
            pool.set_workers(3)
            self.assertTrue(wait_for(lambda: pool.status()["workers_ativos"] == 3))
            bid = jq.create_batch(self.home, "t", self.bugs(4), ["maven"], {})
            self.assertTrue(self.batch_done(bid))
        self.assertEqual(sorted(w.index for w in pool._workers), [0, 1, 2])


class PoolCancelTests(PoolBase):
    def test_cancelar_vai_ao_worker_dono_do_job(self):
        started = threading.Event()
        bid = jq.create_batch(self.home, "t", self.bugs(1), ["jaguar2"], {})
        with mock.patch.object(jq, "execute", blocking_execute(started)):
            pool = self.pool(2)
            pool._workers[0]._next_job = lambda: None     # worker 0 vivo e ocioso: o dono é o 1
            pool.start()
            self.assertTrue(started.wait(10))
            jid = pool.current()["job_id"]
            self.assertEqual(pool.currents()[0]["index"], 1)
            t0 = time.monotonic()
            self.assertEqual(pool.cancel_job(jid), "andamento")
            self.assertTrue(self.batch_done(bid))
            self.assertLess(time.monotonic() - t0, 10)     # o processo foi morto, não esperou 60 s
        self.assertEqual(self.states(bid), ["cancelled"])
        self.assertEqual(list_runs(self.home), [])

    def test_cancelar_job_reivindicado_antes_de_comecar(self):
        calls = []
        bid = jq.create_batch(self.home, "t", self.bugs(1), ["jaguar2"], {})
        with mock.patch.object(jq, "execute", lambda *a, **k: calls.append(1) or "rel/x"):
            pool = self.pool(2)
            picked = pool._workers[1]._next_job()          # reivindicado, ainda sem `_current`
            self.assertEqual(pool.cancel_job(picked[0]["id"]), "andamento")
            pool._workers[1]._run_job(*picked)
        self.assertEqual(calls, [])
        self.assertEqual(self.states(bid), ["cancelled"])

    def test_interromper_pausa_e_devolve_tambem_o_reivindicado(self):
        started, calls = threading.Event(), []
        slow = blocking_execute(started)

        def fake(home, settings, bug, kind, opts):
            calls.append(bug.bug_id)
            return slow(home, settings, bug, kind, opts)

        bid = jq.create_batch(self.home, "t", self.bugs(2), ["jaguar2"], {})
        with mock.patch.object(jq, "execute", fake):
            pool = self.pool(1)
            pool.start()
            self.assertTrue(started.wait(10))              # job 0 rodando
            extra = pool._workers[0].__class__(self.home, self.settings, index=9, owns_lock=False)
            extra._cancel_req, extra._interrupt_req = pool._cancel_req, pool._interrupt_req
            picked = extra._next_job()                     # job 1 reivindicado, ainda sem `_current`
            self.assertIsNotNone(picked)
            self.assertEqual(pool.interromper(), 2)
            extra._run_job(*picked)
            self.assertTrue(wait_for(lambda: self.states(bid) == ["pending", "pending"]))
            time.sleep(0.6)                                # nada é reivindicado de novo
            self.assertEqual(self.states(bid), ["pending", "pending"])
            self.assertEqual(jq.get_batch(self.home, bid)["state"], "paused")
            self.assertEqual(calls, ["0"])
            pool.resume(bid)
        self.assertEqual(list_runs(self.home), [])


# -- cobertura: checkouts, espaço em disco, nova tentativa --

class CoberturaDiscoTests(PoolBase):
    def setUp(self):
        super().setUp()
        folga = type("Uso", (), {"free": 500 * 1024 ** 3})      # por padrão sobra espaço, qualquer que seja o disco
        p = mock.patch.object(jq, "_disk_usage", lambda path: folga())
        p.start()
        self.addCleanup(p.stop)

    def cob_batch(self, n=1, kinds=("maven", "jaguar2"), cobertura=True):
        bid = jq.create_batch(self.home, "cob", self.bugs(n), list(kinds), {})
        if cobertura:
            with contextlib.closing(jq._connect(self.home)) as con, con:
                con.execute("UPDATE batches SET meta=? WHERE id=?", ('{"cobertura": true}', bid))
        return bid

    @staticmethod
    def fake_exec(home, settings, bug, kind, opts):
        return f"rel/{bug.bug_id}/{kind}"

    def test_apaga_o_checkout_so_quando_o_bug_acaba(self):
        bid = self.cob_batch(n=2)
        chamadas = []
        with mock.patch.object(jq, "execute", self.fake_exec), \
                mock.patch.object(jq, "remove_pristine", lambda h, b: chamadas.append(b.key)):
            self.pool(1).start()
            self.assertTrue(self.batch_done(bid))
            self.assertTrue(wait_for(lambda: len(chamadas) == 2))
        self.assertEqual(sorted(chamadas), ["local/p/0", "local/p/1"])     # uma vez por bug, não por etapa

    def test_checkout_fica_enquanto_ha_etapa_pendente(self):
        self.cob_batch(n=1, kinds=("maven", "jaguar2"))
        w = self.worker()
        chamadas = []
        with mock.patch.object(jq, "remove_pristine", lambda h, b: chamadas.append(b.key)):
            job, _ = w._next_job()
            w._finish_job(job, "rel/a", None, 0.1)
            self.assertEqual(chamadas, [])            # a segunda etapa ainda está pendente
            job, _ = w._next_job()
            w._finish_job(job, "rel/b", None, 0.1)
            self.assertEqual(chamadas, ["local/p/0"])

    def test_nao_apaga_em_lote_comum(self):
        bid = self.cob_batch(cobertura=False)
        with mock.patch.object(jq, "execute", self.fake_exec), \
                mock.patch.object(jq, "remove_pristine") as rp:
            self.pool(1).start()
            self.assertTrue(self.batch_done(bid))
            time.sleep(0.2)
        rp.assert_not_called()

    def test_descartar_job_apaga_o_checkout(self):
        self.cob_batch(n=1, kinds=("maven",))
        w = self.worker()
        job, _ = w._next_job()
        with mock.patch.object(jq, "remove_pristine") as rp:
            w._discard_job(job, None, 0.1)
        rp.assert_called_once()

    def test_requeue_nao_apaga_o_checkout(self):
        self.cob_batch(n=1, kinds=("maven",))
        w = self.worker()
        job, _ = w._next_job()
        with mock.patch.object(jq, "remove_pristine") as rp:
            w._requeue_job(job)
        rp.assert_not_called()

    def test_erro_ao_apagar_so_e_registrado(self):
        bid = self.cob_batch(n=1, kinds=("maven",))
        w = self.worker()
        job, _ = w._next_job()
        with mock.patch.object(jq, "remove_pristine", side_effect=OSError("preso")):
            w._finish_job(job, "rel/a", None, 0.1)
        self.assertEqual(self.states(bid), ["done"])
        self.assertIn("preso", w._last_error)

    def test_pouco_espaco_pausa_so_a_cobertura(self):
        cob = self.cob_batch(n=1, kinds=("maven",))
        comum = self.cob_batch(n=1, kinds=("maven",), cobertura=False)
        uso = type("Uso", (), {"free": 3.2 * 1024 ** 3})
        with mock.patch.object(jq, "_disk_usage", lambda p: uso()), \
                mock.patch.object(jq, "execute", self.fake_exec):
            pool = self.pool(1)
            pool.start()
            self.assertTrue(wait_for(lambda: jq.batch_status(self.home, comum)["state"] == "done"))
            self.assertEqual(jq.batch_status(self.home, cob)["state"], "paused")
            self.assertEqual(self.states(cob), ["pending"])
            aviso = pool.status()["aviso_disco"]
        self.assertIn("3,2 GB livres", aviso)
        self.assertIn("mínimo 5 GB", aviso)
        self.assertIn("Cobertura pausada", aviso)

    def test_com_espaco_nao_ha_aviso(self):
        bid = self.cob_batch(n=1, kinds=("maven",))
        uso = type("Uso", (), {"free": 50 * 1024 ** 3})
        with mock.patch.object(jq, "_disk_usage", lambda p: uso()), \
                mock.patch.object(jq, "execute", self.fake_exec), \
                mock.patch.object(jq, "remove_pristine"):
            pool = self.pool(1)
            pool.start()
            self.assertTrue(self.batch_done(bid))
            self.assertIsNone(pool.status()["aviso_disco"])

    def test_minimo_vem_da_configuracao_e_checagem_e_cacheada(self):
        self.settings.set("cobertura", "min_livre_gb", 1)
        chamadas = []

        def uso(p):
            chamadas.append(p)
            return type("Uso", (), {"free": 2 * 1024 ** 3})()
        guard = jq.DiskGuard(self.home, self.settings, ttl=60)
        with mock.patch.object(jq, "_disk_usage", uso):
            self.assertIsNone(guard.verificar())
            self.assertIsNone(guard.verificar())
        self.assertEqual(len(chamadas), 2)       # uma checagem (root e work); a segunda veio do cache

    def test_concluir_tenta_de_novo_com_banco_ocupado(self):
        bid = self.cob_batch(n=1, kinds=("maven",))
        w = self.worker()
        job, _ = w._next_job()
        real, falhas = jq._connect, {"n": 2}

        def conectar(home):
            if falhas["n"] > 0:
                falhas["n"] -= 1
                raise sqlite3.OperationalError("database is locked")
            return real(home)
        with mock.patch.object(jq, "_connect", conectar), mock.patch.object(jq.time, "sleep"), \
                mock.patch.object(jq, "remove_pristine"):
            w._finish_job(job, "rel/a", None, 0.1)
        self.assertEqual(self.states(bid), ["done"])

    def test_concluir_desiste_depois_das_tentativas(self):
        self.cob_batch(n=1, kinds=("maven",))
        w = self.worker()
        job, _ = w._next_job()
        with mock.patch.object(jq, "_connect", side_effect=sqlite3.OperationalError("locked")), \
                mock.patch.object(jq.time, "sleep"):
            with self.assertRaises(sqlite3.OperationalError):
                w._finish_job(job, "rel/a", None, 0.1)

    def test_banco_usa_wal(self):
        with contextlib.closing(jq._connect(self.home)) as con:
            self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")

    def test_fechar_o_app_apaga_a_execucao_parcial(self):
        self.cob_batch(n=1, kinds=("maven",))
        w = self.worker()
        picked = w._next_job()
        w._abort.set()
        with mock.patch.object(jq, "execute", self.fake_exec), \
                mock.patch.object(jq.store, "delete_run") as dr, mock.patch.object(jq, "remove_pristine") as rp:
            w._run_job(*picked)
        dr.assert_called_once_with(self.home, "rel/0/maven")
        rp.assert_not_called()
        self.assertEqual(jq.get_job(self.home, picked[0]["id"])["state"], "pending")


if __name__ == "__main__":
    unittest.main()
