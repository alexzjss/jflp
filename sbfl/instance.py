"""Trava de instância única do processador da fila e utilidades de processo (PID vivo, hora de início)."""
from __future__ import annotations

import os
from pathlib import Path

from .home import Home

if os.name == "nt":
    import msvcrt
else:
    import fcntl

MSG_OUTRA_INSTANCIA = ("outra instância do JFLP está processando a fila "
                       "(feche a interface ou o `jflp batch run` em andamento e tente de novo)")


class WorkerLock:
    """Trava exclusiva `<home>/worker.lock`: só quem a segura recupera execuções e reivindica jobs.

    msvcrt.locking no Windows e fcntl.flock no POSIX; o SO a libera se o processo morrer."""

    def __init__(self, home: Home):
        self.path = Path(home.root) / "worker.lock"
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> bool:
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(fd)


def process_start(pid: int) -> float | None:
    """Hora de início do processo (segundos desde a época) ou None se indisponível."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.windll.kernel32
            k32.OpenProcess.restype = wintypes.HANDLE
            h = k32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return None
            try:
                c, e, k, u = (wintypes.FILETIME() for _ in range(4))
                if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                    return None
                ticks = (c.dwHighDateTime << 32) | c.dwLowDateTime
                return ticks / 1e7 - 11644473600
            finally:
                k32.CloseHandle(h)
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat.rsplit(")", 1)[1].split()
        ticks = int(fields[19])
        boot = next(int(ln.split()[1]) for ln in Path("/proc/stat").read_text().splitlines()
                    if ln.startswith("btime"))
        return boot + ticks / os.sysconf("SC_CLK_TCK")
    except Exception:
        return None


def pid_alive(pid: int | None, started: float | None = None) -> bool:
    """O processo `pid` existe (e, se `started` foi gravado, é o mesmo e não um PID reaproveitado)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = wintypes.HANDLE
        h = k32.OpenProcess(0x1000, False, pid)
        if not h:
            return k32.GetLastError() == 5      # acesso negado: o processo existe
        try:
            code = wintypes.DWORD()
            if k32.GetExitCodeProcess(h, ctypes.byref(code)) and code.value != 259:   # STILL_ACTIVE
                return False
        finally:
            k32.CloseHandle(h)
    else:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
    if started is not None:
        now = process_start(pid)
        if now is not None and abs(now - started) > 5:
            return False
    return True
