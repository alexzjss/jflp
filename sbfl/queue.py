"""Fila persistente de execuções (SQLite) e um único worker em segundo plano que sobrevive a reinícios."""
from __future__ import annotations

import contextlib
import json
import queue as _q
import shutil
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from . import compare, jaguar, proc, store
from .adapters.base import Bug
from .execute import RunOptions, execute
from .home import Home, Settings
from .instance import WorkerLock
from .prepare import _rmtree, remove_pristine
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
WORKERS_MAX = 12      # teto de workers em paralelo
SUB_MAX = 10000
DISK_TTL = 60.0         # segundos entre checagens de espaço livre
FINISH_TRIES = 5        # tentativas do UPDATE que conclui um job


def _disk_usage(path):
    """Ponto único de `shutil.disk_usage` (os testes trocam por um falso)."""
    return shutil.disk_usage(str(path))


COBERTURA_KINDS = ("maven", "jaguar1", "jaguar2")


class DiskGuard:
    """Checa o espaço livre de `home.root` e `home.work` (no máximo a cada `ttl` s). Abaixo do mínimo
    (`cobertura.min_livre_gb`, 5 GB), os lotes de cobertura são pausados e `aviso` diz por quê."""

    def __init__(self, home: Home, settings: Settings, ttl: float = DISK_TTL):
        self.home, self.settings, self.ttl = home, settings, ttl
        self.aviso: str | None = None
        self._at: float | None = None
        self._lock = threading.Lock()

    def _check(self) -> str | None:
        try:
            minimo = float(self.settings.get("cobertura", "min_livre_gb", 5))
        except (TypeError, ValueError):
            minimo = 5.0
        for path in (self.home.root, self.home.work):
            try:
                livre = _disk_usage(path).free / 1024 ** 3
            except OSError:      # a pasta ainda não existe: sem o que medir
                continue
            if livre < minimo:
                unidade = Path(path).anchor or str(path)
                gb = f"{livre:.1f}".replace(".", ",")
                return f"Pouco espaço em {unidade} ({gb} GB livres; mínimo {minimo:g} GB). Cobertura pausada."
        return None

    def verificar(self) -> str | None:
        """Devolve o aviso atual (None se há espaço), pausando a cobertura quando falta."""
        with self._lock:
            agora = time.monotonic()
            if self._at is not None and agora - self._at < self.ttl:
                return self.aviso
            self._at = agora
            self.aviso = self._check()
            aviso = self.aviso
        if aviso:
            try:
                _pausar_cobertura(self.home)
            except Exception:     # noqa: BLE001  # o aviso vale mesmo que a pausa falhe
                pass
        return aviso


def _pausar_cobertura(home: Home) -> None:
    """Pausa os lotes ativos de cobertura (meta.cobertura); os demais lotes seguem."""
    with contextlib.closing(_connect(home)) as con, con:
        for r in con.execute("SELECT id, meta FROM batches WHERE state='active'").fetchall():
            if (json.loads(r["meta"] or "{}")).get("cobertura"):
                con.execute("UPDATE batches SET state='paused' WHERE id=? AND state='active'", (r["id"],))


def _connect(home: Home) -> sqlite3.Connection:
    home.db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(home.db_path), timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")      # leitores não bloqueiam o escritor (persiste no arquivo)
    except sqlite3.OperationalError:                # banco ocupado: fica no modo atual
        pass
    for sql in _SCHEMA:
        con.execute(sql)
    for tabela, coluna in (("jobs", "origem"), ("jobs", "forcado"), ("batches", "meta")):
        if coluna not in {r[1] for r in con.execute(f"PRAGMA table_info({tabela})")}:
            try:
                con.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} TEXT")
            except sqlite3.OperationalError:    # outra conexão migrou primeiro
                pass
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


def recover(home: Home) -> list[Exception]:
    """Recuperação ao abrir (só com a trava): .partial órfãs viram `interrompido`, jobs `running` voltam
    à fila (ou ficam `cancelled` se o lote foi cancelado) e as cópias de trabalho sobrando são apagadas."""
    errors: list[Exception] = []
    try:
        store.recover_partials(home)
    except Exception as e:   # a recuperação parcial não pode travar o worker num laço
        errors.append(e)
    try:
        work = home.work / "w"
        if work.is_dir():
            for d in work.iterdir():
                _rmtree(d, strict=False)
    except Exception as e:
        errors.append(e)
    with contextlib.closing(_connect(home)) as con, con:
        con.execute("UPDATE jobs SET state = CASE WHEN batch_id IN "
                    "(SELECT id FROM batches WHERE state='cancelled') THEN 'cancelled' ELSE 'pending' END "
                    "WHERE state='running'")
        for (bid,) in con.execute("SELECT id FROM batches WHERE state IN ('active','paused')").fetchall():
            _close_if_finished(con, bid)
    return errors


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


def get_batch(home: Home, batch_id: int) -> dict | None:
    """Um lote pelo id, com a contagem de jobs (como em `list_batches`); None se não existe."""
    with contextlib.closing(_connect(home)) as con:
        row = con.execute(
            "SELECT b.id, b.name, b.created, b.kinds, b.state, "
            "(SELECT COUNT(*) FROM jobs j WHERE j.batch_id=b.id) AS total, "
            "(SELECT COUNT(*) FROM jobs j WHERE j.batch_id=b.id AND j.state='done') AS done "
            "FROM batches b WHERE b.id=?", (batch_id,)).fetchone()
    return dict(row) if row else None


def job_of_run(home: Home, run_rel: str) -> dict | None:
    """O job que gravou a execução `run_rel` (com `extra` decodificado); None se nenhum."""
    with contextlib.closing(_connect(home)) as con:
        row = con.execute("SELECT * FROM jobs WHERE run_rel=? ORDER BY id DESC LIMIT 1", (run_rel,)).fetchone()
    if row is None:
        return None
    job = dict(row)
    job["extra"] = json.loads(job.get("bug_extra") or "{}")
    return job


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

    def __init__(self, home: Home, settings: Settings, lock: WorkerLock | None = None, *,
                 index: int = 0, owns_lock: bool = True):
        """`owns_lock=False`: worker de um `WorkerPool`; o pool segura a trava e recupera a fila uma vez."""
        super().__init__(name="jflp-worker" if owns_lock else f"jflp-worker-{index}", daemon=True)
        self.home, self.settings = home, settings
        self.index, self._owns_lock = index, owns_lock
        self._idle = 1.0 if owns_lock else 0.25    # pool: um job que acaba libera outro bug/Jaguar 1 logo
        self._gate = None                        # pool: devolve True quando a trava está com o pool
        self._on_exit = None                       # pool: chamado quando a thread termina
        self.on_bug_change = None                  # pool: chamado com a chave do bug quando um job de cobertura muda
        self._wlock = (lock or WorkerLock(home)) if owns_lock else None   # entregue ao worker
        self._interrupt_req: set[int] = set()      # jobs com "parar agora" pedido (cobre o intervalo até o início)
        self._interrupt = threading.Event()      # "Parar agora": mata o job e o devolve à fila
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
        self._disk = DiskGuard(home, settings)     # pool: compartilhado entre os workers

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

    def interromper_job(self) -> int | None:
        """"Parar agora": mata o job corrente e o devolve à fila. Devolve o id do job, ou None."""
        with self._lock:
            cur = self._current
            if cur is None or self._settled:
                return None
            self._interrupt.set()
            self._cancel.set()            # nenhum processo novo começa
        if self.ident is not None:
            proc.kill_thread(self.ident)
        return cur["job_id"]

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

    def current_of(self, job_id: int) -> dict | None:
        """O job corrente, se for `job_id`."""
        cur = self.current()
        return cur if cur is not None and cur.get("job_id") == job_id else None

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
        if self._current is not None:
            self._cancel_req.discard(self._current["job_id"])
            self._interrupt_req.discard(self._current["job_id"])
        self._progress = None
        self._current = None

    def subscribe(self, job_id: int | None = None) -> _q.Queue:
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

    def _claim_rules(self, alias: str) -> str:
        """Condições extras de reivindicação (SQL), escritas para o SELECT e repetidas no UPDATE atômico:
        nunca dois jobs `running` do mesmo bug e, sem `port_aware`, nunca dois do Jaguar 1."""
        rules = f" AND NOT EXISTS (SELECT 1 FROM jobs r WHERE r.state='running' AND r.bug_key={alias}.bug_key)"
        if not self.settings.get("jaguar1", "port_aware", False):
            rules += (f" AND ({alias}.kind <> 'jaguar1' OR NOT EXISTS "
                      "(SELECT 1 FROM jobs r2 WHERE r2.state='running' AND r2.kind='jaguar1'))")
        # lote de cobertura: só as etapas ligadas no painel (lidas a cada reivindicação, valem na hora)
        ligadas = self._cobertura_kinds()
        if len(ligadas) < len(COBERTURA_KINDS):
            lista = ",".join("'%s'" % k for k in ligadas) or "''"
            rules += (f" AND ({alias}.kind IN ({lista}) OR NOT COALESCE((SELECT CASE WHEN json_valid(cb.meta) "
                      f"THEN json_extract(cb.meta,'$.cobertura') END FROM batches cb WHERE cb.id={alias}.batch_id), 0))")
        return rules

    def _cobertura_kinds(self) -> list[str]:
        """Etapas habilitadas para o lote de cobertura (lista vazia ou inválida = todas)."""
        v = self.settings.get("cobertura", "kinds", None)
        if not isinstance(v, (list, tuple)):
            return list(COBERTURA_KINDS)
        ok = [k for k in COBERTURA_KINDS if k in v]
        return ok or list(COBERTURA_KINDS)

    def _next_job(self):
        try:
            self._disk.verificar()      # sem espaço, a cobertura é pausada antes de reivindicar
        except Exception as e:      # noqa: BLE001
            self._record_error(e)
        with contextlib.closing(_connect(self.home)) as con, con:
            row = con.execute(
                "SELECT j.* FROM jobs j JOIN batches b ON b.id = j.batch_id "
                "WHERE j.state='pending' AND b.state='active'" + self._claim_rules("j") +
                " ORDER BY j.batch_id, j.idx LIMIT 1").fetchone()
            if row is None:
                return None
            self._after_select()
            # reivindicação atômica: só vale se o job ainda está pendente, o lote ativo e as regras seguem
            # valendo (o UPDATE roda sob a trava de escrita do SQLite: dois workers nunca passam juntos)
            cur = con.execute("UPDATE jobs SET state='running' WHERE id=? AND state='pending' "
                              "AND batch_id IN (SELECT id FROM batches WHERE state='active')"
                              + self._claim_rules("jobs"), (row["id"],))
            if cur.rowcount == 0:
                return None
            opts = json.loads(con.execute("SELECT options FROM batches WHERE id=?",
                                          (row["batch_id"],)).fetchone()[0] or "{}")
            return dict(row), opts

    def _record_error(self, e: BaseException) -> None:
        self._last_error = repr(e)[-500:]

    def status(self) -> dict:
        return {"alive": self.is_alive(), "last_error": self._last_error, "current": self.current(),
                "other_instance": self._other, "aviso_disco": self._disk.aviso}

    def _recover(self) -> None:
        """Só roda com a trava: recupera .partial órfãs, devolve jobs `running` e apaga cópias de trabalho."""
        for e in recover(self.home):
            self._record_error(e)

    def run(self) -> None:
        try:
            self._loop()
        finally:
            if self._wlock is not None:
                self._wlock.release()
            if self._on_exit is not None:
                self._on_exit(self)

    def _loop(self) -> None:
        proc.set_abort(self._abort, self._cancel)
        if not self._owns_lock and self.settings.get("jaguar1", "port_aware", False):
            jaguar.set_port(jaguar.BASE_PORT + 1 + self.index)    # uma porta do agente por worker
        recovered = False
        while not self._stop_ev.is_set():
            try:
                if not self._owns_lock:
                    if self._gate is not None and not self._gate():   # o pool ainda não tem a trava
                        self._stop_ev.wait(1.0)
                        continue
                elif not self._wlock.acquire():   # outra instância processa a fila: só espera
                    self._other = True
                    self._stop_ev.wait(1.0)
                    continue
                else:
                    self._other = False
                    if not recovered:
                        recovered = True          # antes de tentar: uma falha não repete para sempre
                        self._recover()
                picked = self._next_job()
                if picked is None:
                    self._stop_ev.wait(self._idle)
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
            self._interrupt.clear()
            self._settled = False
            self._current = {"job_id": job["id"], "batch_id": job["batch_id"],
                             "bug": job["bug_key"], "kind": job["kind"], "started": time.time()}
            self._progress = Progress(job["kind"])
            if job["id"] in self._cancel_req:     # cancelado entre a reivindicação e o início
                self._cancel.set()
            if job["id"] in self._interrupt_req:  # "parar agora" entre a reivindicação e o início
                self._interrupt.set()
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
            interrupted = self._interrupt.is_set()
            cancelled = job["id"] in self._cancel_req or (self._cancel.is_set() and not interrupted)
            self._settled = True      # a partir daqui um cancelamento não tem mais efeito
        if interrupted and not cancelled:    # "Parar agora": a execução parcial sai e o job volta à fila
            self._discard_run(rel or self._find_run(job, since))
            self._requeue_job(job)
            with self._lock:
                self._cancel.clear()
                self._interrupt.clear()
            return
        if cancelled:                 # o usuário cancelou: nada do que foi gravado fica
            self._discard_job(job, rel or self._find_run(job, since), time.monotonic() - t0)
            return
        if self._abort.is_set():   # app fechando: o job é refeito na próxima abertura
            self._discard_run(rel or self._find_run(job, since))    # a execução parcial não fica órfã
            self._requeue_job(job)
            return
        self._finish_job(job, rel, error, time.monotonic() - t0)

    def _discard_job(self, job: dict, rel: str | None, seconds: float) -> None:
        """Fim de um job cancelado: apaga a execução parcial e a cópia de trabalho e marca `cancelled`."""
        self._discard_run(rel)
        try:
            def gravar(con):
                con.execute("UPDATE jobs SET state='cancelled', run_rel=NULL, error=NULL, seconds=? WHERE id=?",
                            (round(seconds, 3), job["id"]))
                _close_if_finished(con, job["batch_id"])
            self._gravar_com_retry(gravar)
        finally:
            self._publish(None)
            with self._lock:
                self._end_current()
                self._cancel.clear()
                self._cancel_req.discard(job["id"])
        self._limpar_checkout(job)
        self._avisar_bug(job)

    def _gravar_com_retry(self, fn) -> None:
        """Roda `fn(con)` numa transação, repetindo com espera curta se o banco estiver ocupado
        (senão o job ficaria `running` para sempre e travaria as outras etapas do bug)."""
        for tentativa in range(FINISH_TRIES):
            try:
                with contextlib.closing(_connect(self.home)) as con, con:
                    fn(con)
                return
            except sqlite3.OperationalError:
                if tentativa == FINISH_TRIES - 1:
                    raise
                time.sleep(0.2 * (tentativa + 1))

    def _limpar_checkout(self, job: dict) -> None:
        """Fim de um bug da cobertura: apaga o checkout cacheado se nenhuma outra etapa dele está pendente
        ou em andamento no lote. Um erro aqui só é registrado."""
        try:
            with contextlib.closing(_connect(self.home)) as con:
                r = con.execute("SELECT meta FROM batches WHERE id=?", (job["batch_id"],)).fetchone()
                if not (r and (json.loads(r["meta"] or "{}")).get("cobertura")):
                    return
                resta = con.execute("SELECT COUNT(*) FROM jobs WHERE batch_id=? AND bug_key=? AND id<>? "
                                    "AND state IN ('pending','running')",
                                    (job["batch_id"], job["bug_key"], job["id"])).fetchone()[0]
            if not resta:
                remove_pristine(self.home, _bug_of(self.settings, job))
        except Exception as e:      # noqa: BLE001
            self._record_error(e)

    def _discard_run(self, rel: str | None) -> None:
        """Apaga só a execução deste job (as cópias de trabalho são do `execute`, que as remove)."""
        try:
            if rel:
                store.delete_run(self.home, rel)
        except Exception as e:
            self._record_error(e)

    def _requeue_job(self, job: dict) -> None:
        try:
            with contextlib.closing(_connect(self.home)) as con, con:
                con.execute("UPDATE jobs SET state='pending' WHERE id=? AND state='running'", (job["id"],))
        finally:
            self._publish(None)
            with self._lock:
                self._end_current()
        self._avisar_bug(job)

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
            def gravar(con):
                con.execute("UPDATE jobs SET state='done', run_rel=?, error=?, seconds=? WHERE id=?",
                            (rel, error, round(seconds, 3), job["id"]))
                _close_if_finished(con, job["batch_id"])
            self._gravar_com_retry(gravar)
        finally:
            self._publish(None)
            with self._lock:
                self._end_current()
        self._limpar_checkout(job)
        self._avisar_bug(job)

    def _avisar_bug(self, job: dict) -> None:
        """Avisa (se houver quem ouça) que um bug do lote de cobertura mudou; um erro aqui nunca chega ao worker."""
        hook = self.on_bug_change
        if hook is None:
            return
        try:
            with contextlib.closing(_connect(self.home)) as con:
                r = con.execute("SELECT meta FROM batches WHERE id=?", (job["batch_id"],)).fetchone()
            if r and (json.loads(r["meta"] or "{}")).get("cobertura"):
                hook(job["bug_key"])
        except Exception as e:      # noqa: BLE001
            self._record_error(e)


class WorkerPool:
    """Vários `Worker` em paralelo sobre a mesma fila. O pool segura a trava de instância e recupera a
    fila uma vez; a regra "nunca dois jobs do mesmo bug" mora na reivindicação SQL de cada worker."""

    def __init__(self, home: Home, settings: Settings, n: int | None = None, lock: WorkerLock | None = None,
                 on_bug_change=None):
        """`on_bug_change(bug_key)`: chamado quando um job de cobertura termina, é descartado ou volta à fila."""
        if n is None:
            n = settings.get("cobertura", "workers", 4)
        try:
            n = int(n)
        except (TypeError, ValueError):
            n = 4
        self.n = max(1, n)
        self.home, self.settings = home, settings
        self._wlock = lock or WorkerLock(home)
        self._plock = threading.Lock()
        self._have_lock = False
        self._other = False
        self._live = 0
        self._last_error: str | None = None
        self._cancel_req: set[int] = set()       # pedidos compartilhados por todos os workers
        self._interrupt_req: set[int] = set()
        self._disk = DiskGuard(home, settings)
        self._on_bug_change = on_bug_change
        self._started = False
        self._stopping = False
        self._workers = [self._novo_worker(i) for i in range(self.n)]

    def _novo_worker(self, i: int) -> Worker:
        w = Worker(self.home, self.settings, index=i, owns_lock=False)
        w._disk = self._disk
        w._cancel_req, w._interrupt_req = self._cancel_req, self._interrupt_req
        w._gate, w._on_exit = self._gate, self._worker_exited
        w.on_bug_change = self._on_bug_change
        return w

    def set_workers(self, n) -> int:
        """Muda o número de workers com o app rodando (limitado a 1-12) e devolve o alvo. Aumentar inicia
        workers novos (índices livres, portas próprias). Diminuir aposenta os de maior índice: eles só
        param de reivindicar e saem DEPOIS do job atual (nunca o matam); o worker 0 nunca sai."""
        try:
            n = int(n)
        except (TypeError, ValueError):
            n = self.n
        n = max(1, min(WORKERS_MAX, n))
        with self._plock:
            if self._stopping:
                return self.n
            self._workers = [w for w in self._workers if w.ident is None or w.is_alive()]
            ativos = sorted((w for w in self._workers if not w._stop_ev.is_set()), key=lambda w: w.index)
            for w in ativos[n:]:                    # do topo: o 0 (primeiro) fica sempre
                w._stop_ev.set()                    # sem _abort: o job em andamento termina normalmente
            usados = {w.index for w in self._workers}
            novos = []
            for _ in range(n - len(ativos)):
                i = next(k for k in range(WORKERS_MAX + len(self._workers) + 1) if k not in usados)
                usados.add(i)
                novos.append(self._novo_worker(i))
            self._workers.extend(novos)
            self.n = n
            if self._started:
                self._live += len(novos)
        for w in novos:
            if self._started:
                w.start()
        return n

    # -- vida --
    def _gate(self) -> bool:
        """Chamado pelos workers: garante a trava e a recuperação (uma vez) antes de reivindicar jobs."""
        with self._plock:
            if self._have_lock:
                return True
            if not self._wlock.acquire():
                self._other = True
                return False
            self._other = False
            self._have_lock = True        # antes de recuperar: uma falha não repete para sempre
            for e in recover(self.home):
                self._last_error = repr(e)[-500:]
            return True

    def _worker_exited(self, _w) -> None:
        with self._plock:
            self._live -= 1
            if self._live <= 0:
                self._wlock.release()
                self._have_lock = False

    def start(self) -> None:
        try:
            self.home.work.mkdir(parents=True, exist_ok=True)   # a pasta configurada pode não existir ainda
        except OSError as e:
            self._last_error = repr(e)[-500:]
        with self._plock:
            self._started = True
            self._live = len(self._workers)
            todos = list(self._workers)
        for w in todos:
            w.start()

    def stop(self, kill: bool = True) -> None:
        with self._plock:
            self._stopping = True
            todos = list(self._workers)
        for w in todos:
            w._stop_ev.set()
            if kill:
                w._abort.set()
        if kill:
            proc.kill_all()

    def join(self, timeout: float | None = None) -> None:
        end = None if timeout is None else time.monotonic() + timeout
        for w in self._workers:
            if w.ident is not None:
                w.join(None if end is None else max(0.0, end - time.monotonic()))

    def is_alive(self) -> bool:
        return any(w.is_alive() for w in self._workers)

    # -- observação --
    def status(self) -> dict:
        errs = [w._last_error for w in self._workers if w._last_error]
        return {"alive": self.is_alive(), "last_error": self._last_error or (errs[-1] if errs else None),
                "current": self.current(), "other_instance": self._other, "workers": self.n,
                "workers_ativos": sum(1 for w in list(self._workers) if w.is_alive()),
                "aviso_disco": self._disk.aviso}

    def currents(self) -> list[dict]:
        out = []
        for w in self._workers:
            cur = w.current()
            if cur is not None:
                out.append({**cur, "index": w.index})
        return out

    def current(self) -> dict | None:
        cur = self.currents()
        return cur[0] if cur else None

    def current_of(self, job_id: int) -> dict | None:
        for c in self.currents():
            if c.get("job_id") == job_id:
                return c
        return None

    def progress_of(self, job_id: int) -> dict | None:
        for w in self._workers:
            prog = w.progress_of(job_id)
            if prog is not None:
                return prog
        return None

    def _owner(self, job_id: int) -> Worker | None:
        for w in self._workers:
            with w._lock:
                cur = w._current
            if cur is not None and cur.get("job_id") == job_id:
                return w
        return None

    def subscribe(self, job_id: int | None = None) -> _q.Queue:
        """Fila de linhas do worker que roda `job_id`; sem ele (ou se ninguém o roda), de todos."""
        owner = self._owner(job_id) if job_id is not None else None
        workers = [owner] if owner is not None else list(self._workers)
        q: _q.Queue = _q.Queue(maxsize=SUB_MAX)
        for w in workers:
            with w._lock:
                w._subs.add(q)
        q._jflp_workers = workers      # type: ignore[attr-defined]
        return q

    def unsubscribe(self, q: _q.Queue) -> None:
        for w in getattr(q, "_jflp_workers", self._workers):
            w.unsubscribe(q)

    # -- controle (as operações são todas SQL: qualquer worker serve) --
    def cancel_job(self, job_id: int) -> str:
        """Cancela um job: pendente sai da fila; em andamento, só o worker dono o mata. Um job já
        reivindicado mas ainda sem dono vale pelo pedido compartilhado, que o `_run_job` confere."""
        if self.remove_pending(job_id):
            return "pendente"
        job = get_job(self.home, job_id)
        if job is None or job["state"] != "running":
            return ""
        self._cancel_req.add(job_id)           # antes de procurar o dono: não há janela sem cobertura
        owner = self._owner(job_id)
        if owner is not None:
            r = owner.cancel_job(job_id)
            if not r:
                self._cancel_req.discard(job_id)
            return r
        return "andamento"                     # reivindicado, ainda sem dono: o `_run_job` vê o pedido

    def remove_pending(self, job_id: int) -> bool:
        return self._workers[0].remove_pending(job_id)

    def cancel(self, batch_id: int) -> None:
        self._workers[0].cancel(batch_id)

    def pause(self, batch_id: int) -> None:
        self._workers[0].pause(batch_id)

    def resume(self, batch_id: int) -> None:
        self._workers[0].resume(batch_id)

    def cancel_pending(self) -> int:
        return self._workers[0].cancel_pending()

    def cancel_everything(self) -> int:
        n = self.cancel_pending()
        for cur in self.currents():
            if self.cancel_job(cur["job_id"]):
                n += 1
        return n

    def interromper(self) -> int:
        """"Parar agora": pausa os lotes ativos (nada é reivindicado de novo), mata os jobs em andamento,
        inclusive os reivindicados que ainda não começaram, e os devolve à fila. `resume` recomeça."""
        with contextlib.closing(_connect(self.home)) as con, con:
            con.execute("UPDATE batches SET state='paused' WHERE state='active'")
            ids = [r[0] for r in con.execute("SELECT id FROM jobs WHERE state='running'")]
        for jid in ids:
            self._interrupt_req.add(jid)       # antes de procurar o dono: cobre o job sem `_current`
            owner = self._owner(jid)
            if owner is not None:
                owner.interromper_job()
        return len(ids)
