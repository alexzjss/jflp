from __future__ import annotations

import collections
import os
import re
import signal
import subprocess
import threading
import time


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
        count=None, collect=None, tail_lines=3000) -> dict:
    """Executa um comando streamando a saída.

    drop    regex de linhas a NÃO gravar no log (ex.: ruído do Jaguar)
    count   {nome: regex}  -> quantas linhas casam
    collect {nome: regex com 1 grupo} -> lista dos valores capturados
    """
    drop_re = re.compile(drop) if drop else None
    count_res = {k: re.compile(v) for k, v in (count or {}).items()}
    collect_res = {k: re.compile(v) for k, v in (collect or {}).items()}
    counts = dict.fromkeys(count_res, 0)
    collected = {k: [] for k in collect_res}
    tail = collections.deque(maxlen=tail_lines)
    t0 = time.time()
    p = subprocess.Popen(
        [str(c) for c in cmd], cwd=cwd, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        bufsize=1, start_new_session=(os.name != "nt"),
    )
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
    finally:
        p.stdout.close()
        p.wait()
        if timer:
            timer.cancel()
        if f:
            f.close()
    return {"rc": p.returncode, "tail": list(tail), "timed_out": state["timed_out"],
            "counts": counts, "collected": collected, "seconds": round(time.time() - t0, 1)}


def make_env(cfg) -> dict:
    env = dict(os.environ)
    jh = cfg.get("java", "java_home")
    if jh:
        env["JAVA_HOME"] = jh
        env["PATH"] = os.path.join(jh, "bin") + os.pathsep + env.get("PATH", "")
    return env
