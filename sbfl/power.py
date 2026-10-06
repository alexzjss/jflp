"""Impede o computador de dormir durante campanhas longas (best effort, nunca falha)."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from contextlib import contextmanager


def _stop(proc: subprocess.Popen) -> None:
    try:
        if os.name != "nt":
            os.killpg(proc.pid, signal.SIGTERM)   # o grupo inteiro: systemd-inhibit + `sleep infinity`
        else:
            proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass


@contextmanager
def keep_awake():
    proc = None
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)  # CONTINUOUS | SYSTEM_REQUIRED
        elif sys.platform == "darwin" and shutil.which("caffeinate"):
            proc = subprocess.Popen(["caffeinate", "-i"], start_new_session=True)
        elif shutil.which("systemd-inhibit"):
            proc = subprocess.Popen(["systemd-inhibit", "--what=sleep:idle", "--why=sbfl campaign",
                                     "sleep", "infinity"], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        proc = None
    try:
        yield
    finally:
        if os.name == "nt":
            try:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)  # CONTINUOUS
            except Exception:
                pass
        elif proc:
            _stop(proc)
