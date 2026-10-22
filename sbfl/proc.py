from __future__ import annotations

import atexit
import collections
import os
import re
import signal
import subprocess
import threading
import time
from typing import Callable


class Aborted(Exception):
    """A thread foi interrompida (fechamento do app): nenhum processo novo pode começar."""


_LIVE: set[subprocess.Popen] = set()
_OWNER: dict[subprocess.Popen, int] = {}     # processo -> id da thread que o iniciou
_LIVE_LOCK = threading.Lock()
_LOCAL = threading.local()


def set_abort(*events: threading.Event | None) -> None:
    """Liga à thread atual eventos de interrupção: com qualquer um aceso, `run` recusa novos processos.

    O worker passa dois: o do fechamento do app e o do cancelamento pelo usuário."""
    _LOCAL.abort = tuple(e for e in events if e is not None)


def aborted() -> bool:
    return any(ev.is_set() for ev in getattr(_LOCAL, "abort", ()))


def live_count() -> int:
    with _LIVE_LOCK:
        return len(_LIVE)


def kill_all() -> int:
    """Mata a árvore de todos os processos em andamento (fechar o app não deixa Maven órfão)."""
    with _LIVE_LOCK:
        procs = list(_LIVE)
    for p in procs:
        kill_tree(p)
    return len(procs)


def kill_thread(ident: int) -> int:
    """Mata a árvore dos processos iniciados por uma thread (cancelar um job sem tocar nos demais)."""
    with _LIVE_LOCK:
        procs = [p for p in _LIVE if _OWNER.get(p) == ident]
    for p in procs:
        kill_tree(p)
    return len(procs)


atexit.register(kill_all)


def kill_tree(p: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
        else:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def run(cmd, cwd=None, env=None, timeout=None, log_path=None, drop=None,
        count=None, collect=None, tail_lines=3000,
        on_line: Callable[[str], None] | None = None) -> dict:
    """Executa um comando streamando a saída.

    drop    regex de linhas a NÃO gravar no log (ex.: ruído do Jaguar)
    count   {nome: regex}  -> quantas linhas casam
    collect {nome: regex com 1 grupo} -> lista dos valores capturados
    on_line chamada para cada linha gravada no log (ao vivo, para SSE)
    """
    drop_re = re.compile(drop) if drop else None
    count_res = {k: re.compile(v) for k, v in (count or {}).items()}
    collect_res = {k: re.compile(v) for k, v in (collect or {}).items()}
    counts = dict.fromkeys(count_res, 0)
    collected = {k: [] for k in collect_res}
    tail = collections.deque(maxlen=tail_lines)
    t0 = time.time()
    if aborted():
        raise Aborted("interrompido")
    with _LIVE_LOCK:        # registrado e checado sob a mesma trava: kill_all nunca perde um processo
        p = subprocess.Popen(
            [str(c) for c in cmd], cwd=cwd, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            bufsize=1, start_new_session=(os.name != "nt"),
        )
        _LIVE.add(p)
        _OWNER[p] = threading.get_ident()
    if aborted():           # o aviso chegou entre a checagem e o registro
        kill_tree(p)
    state = {"timed_out": False}

    def _kill():
        state["timed_out"] = True
        kill_tree(p)

    timer = threading.Timer(timeout, _kill) if timeout else None
    if timer:
        timer.start()
    f = open(log_path, "a", encoding="utf-8") if log_path else None
    try:
        for line in p.stdout:
            for k, rx in count_res.items():
                if rx.search(line):
                    counts[k] += 1
            for k, rx in collect_res.items():
                m = rx.search(line)
                if m and len(collected[k]) < 20000:
                    collected[k].append(m.group(1))
            if drop_re and drop_re.search(line):
                continue
            tail.append(line)
            if f:
                f.write(line)
            if on_line:
                try:
                    on_line(line.rstrip("\r\n"))
                except Exception:  # um callback ruim nunca derruba a leitura
                    pass
    finally:
        p.stdout.close()
        p.wait()
        with _LIVE_LOCK:
            _LIVE.discard(p)
            _OWNER.pop(p, None)
        if timer:
            timer.cancel()
        if f:
            f.close()
    return {"rc": p.returncode, "tail": list(tail), "timed_out": state["timed_out"],
            "counts": counts, "collected": collected, "seconds": round(time.time() - t0, 1)}

