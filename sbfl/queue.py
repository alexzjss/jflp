"""Fila persistente de execuções (SQLite) e um único worker em segundo plano que sobrevive a reinícios."""
from __future__ import annotations

import contextlib
import json
import queue as _q
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime

from . import compare, proc, store
from .adapters.base import Bug
from .execute import RunOptions, execute
from .home import Home, Settings
from .instance import WorkerLock
from .prepare import _rmtree
from .power import keep_awake
from .progress import Progress
from .prepare import bug_from_key

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS batches(
        id INTEGER PRIMARY KEY, name TEXT, created TEXT, kinds TEXT, options TEXT, state TEXT)""",
    """CREATE TABLE IF NOT EXISTS jobs(
        id INTEGER PRIMARY KEY, batch_id INTEGER, idx INTEGER, bug_key TEXT, bug_extra TEXT,
        kind TEXT, state TEXT, run_rel TEXT, error TEXT, seconds REAL)""",
)
LOG_LINES = 500
SUB_MAX = 10000


def _connect(home: Home) -> sqlite3.Connection:
    home.db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(home.db_path), timeout=30)
    con.row_factory = sqlite3.Row
    for sql in _SCHEMA:
        con.execute(sql)
    return con


def create_batch(home: Home, name: str, bugs: list[Bug], kinds: list[str], opts: dict) -> int:
    """Cria um lote (um job por bug e tipo). Uma execução avulsa é um lote de 1 job."""
    with contextlib.closing(_connect(home)) as con, con:
        cur = con.execute("INSERT INTO batches(name, created, kinds, options, state) VALUES(?,?,?,?,?)",
                          (name, datetime.now().isoformat(timespec="seconds"),
                           json.dumps(list(kinds)), json.dumps(opts or {}), "active"))
        bid = cur.lastrowid
        idx = 0
        for bug in bugs:
            for kind in kinds:
                con.execute("INSERT INTO jobs(batch_id, idx, bug_key, bug_extra, kind, state) "
                            "VALUES(?,?,?,?,?,'pending')",
                            (bid, idx, bug.key, json.dumps(bug.extra or {}), kind))
                idx += 1
    return bid


def _bug_of(settings: Settings, job: dict) -> Bug:
    try:
        return bug_from_key(settings, job["bug_key"])
    except ValueError:
        benchmark, project, bug_id = job["bug_key"].split("/", 2)   # bug local: reconstrói do extra
        return Bug(benchmark, project, bug_id, json.loads(job["bug_extra"] or "{}"))


def _close_if_finished(con: sqlite3.Connection, batch_id: int) -> None:
    left = con.execute("SELECT COUNT(*) FROM jobs WHERE batch_id=? AND state IN ('pending','running')",
                       (batch_id,)).fetchone()[0]
    if left == 0:
        con.execute("UPDATE batches SET state='done' WHERE id=? AND state IN ('active','paused')",
                    (batch_id,))


def active_batch_ids(home: Home) -> list[int]:
    """Lotes ativos que ainda têm jobs pendentes ou em andamento."""
    with contextlib.closing(_connect(home)) as con:
        return [r[0] for r in con.execute(
            "SELECT b.id FROM batches b WHERE b.state='active' AND EXISTS "
            "(SELECT 1 FROM jobs j WHERE j.batch_id=b.id AND j.state IN ('pending','running')) ORDER BY b.id")]


def list_jobs(home: Home, state: str | None = None, limit: int = 50) -> list[dict]:
    """Jobs de todos os lotes (mais antigos primeiro), opcionalmente de um só estado."""
    sql, args = "SELECT * FROM jobs", []
    if state is not None:
        sql += " WHERE state=?"
        args.append(state)
    sql += " ORDER BY batch_id, idx LIMIT ?"
    with contextlib.closing(_connect(home)) as con:
        return [dict(r) for r in con.execute(sql, (*args, limit))]


def get_job(home: Home, job_id: int) -> dict | None:
    """Um job pelo id (consulta única, sem carregar a fila inteira)."""
    with contextlib.closing(_connect(home)) as con:
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_batches(home: Home, limit: int = 100) -> list[dict]:
    """Lotes do mais recente ao mais antigo, com a contagem de jobs."""
    with contextlib.closing(_connect(home)) as con:
        return [dict(r) for r in con.execute(
            "SELECT b.id, b.name, b.created, b.kinds, b.state, "
            "(SELECT COUNT(*) FROM jobs j WHERE j.batch_id=b.id) AS total, "
            "(SELECT COUNT(*) FROM jobs j WHERE j.batch_id=b.id AND j.state='done') AS done "
            "FROM batches b ORDER BY b.id DESC LIMIT ?", (limit,))]


def pending_jobs(home: Home) -> list[dict]:
    """Jobs pendentes de lotes ativos ou pausados, na ordem de execução (com nome e estado do lote)."""
    with contextlib.closing(_connect(home)) as con:
        return [dict(r) for r in con.execute(
            "SELECT j.*, b.name AS batch_name, b.state AS batch_state FROM jobs j "
            "JOIN batches b ON b.id = j.batch_id WHERE j.state='pending' AND b.state IN ('active','paused') "
            "ORDER BY j.batch_id, j.idx")]


def delete_batch(home: Home, batch_id: int) -> None:
    """Apaga um lote encerrado e seus jobs do histórico (as execuções gravadas em `runs/` ficam).

    Recusa lotes com job pendente ou em andamento: é preciso cancelar antes. A checagem e a remoção
    são um único comando SQL por tabela, na mesma transação, então o worker nunca pega um job no meio."""
    with contextlib.closing(_connect(home)) as con, con:
        if con.execute("SELECT 1 FROM batches WHERE id=?", (batch_id,)).fetchone() is None:
            raise ValueError(f"Lote não encontrado: {batch_id}")
        con.execute("DELETE FROM jobs WHERE batch_id=? AND NOT EXISTS "
                    "(SELECT 1 FROM jobs WHERE batch_id=? AND state IN ('pending','running'))",
                    (batch_id, batch_id))
        cur = con.execute("DELETE FROM batches WHERE id=? AND NOT EXISTS (SELECT 1 FROM jobs WHERE batch_id=?)",
                          (batch_id, batch_id))
        if cur.rowcount == 0:
            raise ValueError("O lote ainda tem jobs na fila ou em andamento: cancele antes de excluir.")


def batch_jobs(home: Home, batch_id: int) -> list[dict]:
    """Jobs de um lote, na ordem (bug_extra já decodificado em `extra`)."""
    with contextlib.closing(_connect(home)) as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM jobs WHERE batch_id=? ORDER BY idx", (batch_id,))]
    for r in rows:
        r["extra"] = json.loads(r.get("bug_extra") or "{}")
    return rows


def batch_status(home: Home, batch_id: int) -> dict:
    """Contagens por estado, ETA pela duração média dos jobs concluídos e resumo por projeto."""
    with contextlib.closing(_connect(home)) as con:
        b = con.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if b is None:
            raise ValueError(f"Lote não encontrado: {batch_id}")
        jobs = [dict(r) for r in con.execute("SELECT * FROM jobs WHERE batch_id=? ORDER BY idx", (batch_id,))]
    counts = {s: 0 for s in ("pending", "running", "done", "cancelled")}
    for j in jobs:
        counts[j["state"]] = counts.get(j["state"], 0) + 1
    secs = [j["seconds"] for j in jobs if j["state"] == "done" and j["seconds"] is not None]
    eta = round(sum(secs) / len(secs) * (counts["pending"] + counts["running"])) if secs else None
    paths = [j["run_rel"] for j in jobs if j["state"] == "done" and j["run_rel"]]
    runs = []
    if paths:
        marks = ",".join("?" * len(paths))
        with contextlib.closing(store._connect(home)) as con:
            runs = [dict(r) for r in con.execute(f"SELECT * FROM runs WHERE path IN ({marks})", paths)]
    finished = b["state"] == "done" or (b["state"] == "cancelled" and counts["running"] == 0)
    return {"id": batch_id, "name": b["name"], "state": b["state"], "finished": finished, "total": len(jobs),
            "counts": counts, "eta_seconds": eta, "summary": compare.batch_summary(home, runs),
            "jobs": jobs}


class Worker(threading.Thread):
    """Executa os jobs pendentes, um por vez, na ordem de criação."""

    def __init__(self, home: Home, settings: Settings, lock: WorkerLock | None = None):
        super().__init__(name="jflp-worker", daemon=True)
        self.home, self.settings = home, settings
        self._wlock = lock or WorkerLock(home)     # entregue ao worker: ele a libera ao terminar
        self._other = False                        # outra instância segura a trava
        self._stop_ev = threading.Event()
        self._abort = threading.Event()            # fechamento do app: mata o Maven e devolve o job à fila
        self._cancel = threading.Event()           # cancelamento pelo usuário: mata o Maven e descarta o job
        self._cancel_req: set[int] = set()         # jobs com cancelamento pedido (cobre o intervalo até o início)
        self._settled = True                       # o job corrente já passou do ponto de cancelar
        self._lock = threading.Lock()
        self._subs: set[_q.Queue] = set()
        self._log: deque[str] = deque(maxlen=LOG_LINES)
        self._current: dict | None = None
        self._progress: Progress | None = None     # progresso do job corrente (alimentado por _on_line)
        self._last_progress: tuple[int, dict] | None = None   # (job, estado final) do último job
        self._last_error: str | None = None

    # -- controle --
    def _set_batch(self, batch_id: int, frm: tuple, to: str) -> None:
        marks = ",".join("?" * len(frm))
        with contextlib.closing(_connect(self.home)) as con, con:
            con.execute(f"UPDATE batches SET state=? WHERE id=? AND state IN ({marks})", (to, batch_id, *frm))

    def pause(self, batch_id: int) -> None:
        self._set_batch(batch_id, ("active",), "paused")

    def resume(self, batch_id: int) -> None:
        self._set_batch(batch_id, ("paused",), "active")

    def cancel(self, batch_id: int) -> None:
        """Cancela os jobs pendentes; o job em andamento termina normalmente."""
        with contextlib.closing(_connect(self.home)) as con, con:
            con.execute("UPDATE jobs SET state='cancelled' WHERE batch_id=? AND state='pending'", (batch_id,))
            con.execute("UPDATE batches SET state='cancelled' WHERE id=? AND state IN ('active','paused')",
                        (batch_id,))

    def remove_pending(self, job_id: int) -> bool:
        """Tira um job da fila (vira `cancelled`). Falso se ele não está mais pendente.

        O UPDATE condicional é o mesmo da reivindicação em `_next_job`: só um dos dois vence."""
        with contextlib.closing(_connect(self.home)) as con, con:
            cur = con.execute("UPDATE jobs SET state='cancelled' WHERE id=? AND state='pending'", (job_id,))
            if cur.rowcount == 0:
                return False
            bid = con.execute("SELECT batch_id FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
            _close_if_finished(con, bid)
        return True

    def cancel_job(self, job_id: int) -> str:
        """Cancela um job. Devolve `pendente` (tirado da fila), `andamento` (Maven morto, execução
        descartada) ou vazio se não havia o que cancelar."""
        if self.remove_pending(job_id):
            return "pendente"
        job = get_job(self.home, job_id)
        if job is None or job["state"] != "running":
            return ""
        with self._lock:
            cur = self._current
            if cur is not None and (cur["job_id"] != job_id or self._settled):
                return ""
            if cur is None and (self._other or not self.is_alive()):
                return ""
            self._cancel_req.add(job_id)      # se o job ainda não começou, `_run_job` vê o pedido
            if cur is not None:
                self._cancel.set()
        if cur is not None and self.ident is not None:
            proc.kill_thread(self.ident)      # só os processos do worker, a árvore inteira
        return "andamento"

    def cancel_pending(self) -> int:
        """Cancela todos os jobs pendentes de todos os lotes; o job em andamento segue."""
        with contextlib.closing(_connect(self.home)) as con, con:
            n = con.execute("UPDATE jobs SET state='cancelled' WHERE state='pending'").rowcount
            con.execute("UPDATE batches SET state='cancelled' WHERE state IN ('active','paused')")
        return n

    def cancel_everything(self) -> int:
        """Esvazia a fila e cancela também o job em andamento."""
        n = self.cancel_pending()
        cur = self.current()
        if cur is not None and self.cancel_job(cur["job_id"]):
            n += 1
        return n

    def stop(self, kill: bool = True) -> None:
        """Para o laço. Com `kill`, também mata os processos em andamento (o job volta para `pending`)."""
        self._stop_ev.set()
        if kill:
            self._abort.set()
            proc.kill_all()

    # -- observação --
    def current(self) -> dict | None:
        with self._lock:
            if self._current is None:
                return None
            prog = self._progress.to_dict() if self._progress is not None else None
            return {**self._current, "log": list(self._log), "progress": prog}

    def progress_of(self, job_id: int) -> dict | None:
        """Progresso do job corrente ou, logo depois de terminar, o estado final dele."""
        with self._lock:
            if self._current is not None and self._current.get("job_id") == job_id and self._progress is not None:
                return self._progress.to_dict()
            if self._last_progress is not None and self._last_progress[0] == job_id:
                return self._last_progress[1]
        return None

    def _end_current(self) -> None:
        """Fim do job corrente (com a trava): guarda o estado final do progresso e limpa o corrente."""
        if self._progress is not None and self._current is not None:
            self._progress.finish()
            self._last_progress = (self._current["job_id"], self._progress.to_dict())
        self._progress = None
        self._current = None

    def subscribe(self) -> _q.Queue:
        q: _q.Queue = _q.Queue(maxsize=SUB_MAX)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: _q.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def _publish(self, line) -> None:
        with self._lock:
            if line is not None:
                self._log.append(line)
            for q in list(self._subs):
                try:
                    q.put_nowait(line)
                except _q.Full:
                    self._subs.discard(q)   # assinante lento: descartado, nunca bloqueia o worker

    def _on_line(self, line: str) -> None:
        text = str(line).rstrip("\r\n")
        with self._lock:
            if self._progress is not None:
                try:
                    self._progress.feed(text)
                except Exception:      # o progresso é enfeite: nunca atrapalha o job
                    pass
        self._publish(text)

    # -- laço --
    def _after_select(self) -> None:
        """Gancho (só para testes): roda entre o SELECT e a reivindicação do job."""

    def _next_job(self):
        with contextlib.closing(_connect(self.home)) as con, con:
            row = con.execute(
                "SELECT j.* FROM jobs j JOIN batches b ON b.id = j.batch_id "
                "WHERE j.state='pending' AND b.state='active' ORDER BY j.batch_id, j.idx LIMIT 1").fetchone()
            if row is None:
                return None
            self._after_select()
            # reivindicação atômica: só vale se o job ainda está pendente e o lote ativo
            cur = con.execute("UPDATE jobs SET state='running' WHERE id=? AND state='pending' "
                              "AND batch_id IN (SELECT id FROM batches WHERE state='active')", (row["id"],))
            if cur.rowcount == 0:
                return None
            opts = json.loads(con.execute("SELECT options FROM batches WHERE id=?",
                                          (row["batch_id"],)).fetchone()[0] or "{}")
            return dict(row), opts

    def _record_error(self, e: BaseException) -> None:
        self._last_error = repr(e)[-500:]

    def status(self) -> dict:
        return {"alive": self.is_alive(), "last_error": self._last_error, "current": self.current(),
                "other_instance": self._other}

    def _recover(self) -> None:
        """Só roda com a trava: recupera .partial órfãs, devolve jobs `running` e apaga cópias de trabalho."""
        try:
            store.recover_partials(self.home)
        except Exception as e:   # a recuperação parcial não pode travar o worker num laço
            self._record_error(e)
        try:
            work = self.home.work / "w"
            if work.is_dir():
                for d in work.iterdir():
                    _rmtree(d, strict=False)
        except Exception as e:
            self._record_error(e)
        with contextlib.closing(_connect(self.home)) as con, con:
            con.execute("UPDATE jobs SET state = CASE WHEN batch_id IN "
                        "(SELECT id FROM batches WHERE state='cancelled') THEN 'cancelled' ELSE 'pending' END "
                        "WHERE state='running'")
            for (bid,) in con.execute("SELECT id FROM batches WHERE state IN ('active','paused')").fetchall():
                _close_if_finished(con, bid)

    def run(self) -> None:
        try:
            self._loop()
        finally:
            self._wlock.release()

    def _loop(self) -> None:
        proc.set_abort(self._abort, self._cancel)
        recovered = False
        while not self._stop_ev.is_set():
            try:
                if not self._wlock.acquire():     # outra instância processa a fila: só espera
                    self._other = True
                    self._stop_ev.wait(1.0)
                    continue
                self._other = False
                if not recovered:
                    recovered = True              # antes de tentar: uma falha não repete para sempre
                    self._recover()
                picked = self._next_job()
                if picked is None:
                    self._stop_ev.wait(1.0)
                    continue
                self._process(picked)
            except Exception as e:   # nada derruba a thread: registra e tenta de novo
                self._record_error(e)
                self._stop_ev.wait(1.0)

    def _process(self, picked) -> None:
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(keep_awake())
            except Exception as e:   # sem keep_awake os jobs seguem
                self._record_error(e)
            while picked is not None:
                self._run_job(*picked)
                picked = None if self._stop_ev.is_set() else self._next_job()

    def _run_job(self, job: dict, opts: dict) -> None:
        with self._lock:
            self._log.clear()
            self._cancel.clear()
            self._settled = False
            self._current = {"job_id": job["id"], "batch_id": job["batch_id"],
                             "bug": job["bug_key"], "kind": job["kind"], "started": time.time()}
            self._progress = Progress(job["kind"])
            if job["id"] in self._cancel_req:     # cancelado entre a reivindicação e o início
                self._cancel.set()
        t0 = time.monotonic()
        since = datetime.now().isoformat(timespec="seconds")
        rel, error = None, None
        if not self._cancel.is_set():
            try:
                bug = _bug_of(self.settings, job)
                rel = execute(self.home, self.settings, bug, job["kind"],
                              RunOptions(**opts, log_cb=self._on_line))
            except Exception as e:   # o worker nunca morre por erro de job
                error = repr(e)[-1500:]
                rel = self._find_run(job, since)
        with self._lock:
            cancelled = self._cancel.is_set() or job["id"] in self._cancel_req
            self._settled = True      # a partir daqui um cancelamento não tem mais efeito
        if cancelled:                 # o usuário cancelou: nada do que foi gravado fica
            self._discard_job(job, rel or self._find_run(job, since), time.monotonic() - t0)
            return
        if self._abort.is_set():   # app fechando: o job é refeito na próxima abertura
            self._requeue_job(job)
            return
        self._finish_job(job, rel, error, time.monotonic() - t0)

    def _discard_job(self, job: dict, rel: str | None, seconds: float) -> None:
        """Fim de um job cancelado: apaga a execução parcial e a cópia de trabalho e marca `cancelled`."""
        try:
            if rel:
                store.delete_run(self.home, rel)
            work = self.home.work / "w"
            if work.is_dir():           # um job por vez: toda cópia de trabalho é deste job
                for d in work.iterdir():
                    _rmtree(d, strict=False)
        except Exception as e:
            self._record_error(e)
        try:
            with contextlib.closing(_connect(self.home)) as con, con:
                con.execute("UPDATE jobs SET state='cancelled', run_rel=NULL, error=NULL, seconds=? WHERE id=?",
                            (round(seconds, 3), job["id"]))
                _close_if_finished(con, job["batch_id"])
        finally:
            self._publish(None)
            with self._lock:
                self._end_current()
                self._cancel.clear()
                self._cancel_req.discard(job["id"])

    def _requeue_job(self, job: dict) -> None:
        try:
            with contextlib.closing(_connect(self.home)) as con, con:
                con.execute("UPDATE jobs SET state='pending' WHERE id=? AND state='running'", (job["id"],))
        finally:
            self._publish(None)
            with self._lock:
                self._end_current()

    def _find_run(self, job: dict, since: str) -> str | None:
        """Pasta criada por um job que falhou (a mais recente do bug e tipo desde o início do job)."""
        try:
            b, p, i = job["bug_key"].split("/", 2)
            runs = [r for r in store.list_runs(self.home, b, p, i, job["kind"])
                    if (r.get("started") or "") >= since]
            return runs[0]["path"] if runs else None
        except Exception:
            return None

    def _finish_job(self, job: dict, rel, error, seconds: float) -> None:
        try:
            with contextlib.closing(_connect(self.home)) as con, con:
                con.execute("UPDATE jobs SET state='done', run_rel=?, error=?, seconds=? WHERE id=?",
                            (rel, error, round(seconds, 3), job["id"]))
                _close_if_finished(con, job["batch_id"])
        finally:
            self._publish(None)
            with self._lock:
                self._end_current()
