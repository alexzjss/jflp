"""Armazenamento imutável de execuções: pasta final somente leitura + índice SQLite reconstruível."""
from __future__ import annotations

import contextlib
import gzip
import json
import os
import re
import shutil
import sqlite3
import stat
import tarfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

from .adapters.base import Bug
from .home import Home
from .instance import pid_alive, process_start
from .winpath import long_path, lp

_INVALID = re.compile(r'[:*?"<>|/\\\x00-\x1f]')
_RESERVED = re.compile(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?")

_MANIFEST = ".writer.json"  # dados originais, para recuperar uma .partial
_SCHEMA = """CREATE TABLE IF NOT EXISTS runs(
    path TEXT PRIMARY KEY, benchmark TEXT, project TEXT, bug TEXT, kind TEXT,
    status TEXT, started TEXT, seconds REAL, n_tests INT, n_failed INT, parent TEXT)"""
_INSERT = "INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)"


def _safe(part: str) -> str:
    """Componente de caminho válido no Windows (troca : * ? " < > | por _)."""
    s = _INVALID.sub("_", str(part)).rstrip(" .")
    s = re.sub(r"^\.+", lambda m: "_" * len(m.group()), s)   # ponto inicial: nunca parece uma .partial
    if not s:
        return "_"  # vazio, "." ou ".."
    return "_" + s if _RESERVED.fullmatch(s) else s


def make_writable(path) -> None:
    """Remove o somente-leitura de um arquivo ou árvore (para apagar execuções)."""
    p = lp(path)
    items = [p] + (list(p.rglob("*")) if p.is_dir() else [])
    for item in items:
        with contextlib.suppress(OSError):
            os.chmod(item, stat.S_IWRITE | stat.S_IREAD)


def _lock_files(folder: Path) -> None:
    for item in lp(folder).rglob("*"):
        if item.is_file():
            os.chmod(item, stat.S_IREAD)


def _connect(home: Home) -> sqlite3.Connection:
    home.db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(home.db_path), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute(_SCHEMA)
    return con


def _row(rel: str, run: dict, parts: tuple = ()) -> tuple:
    tests = run.get("tests") or {}
    failed = None
    if "failed" in tests or "error" in tests:
        failed = (tests.get("failed") or 0) + (tests.get("error") or 0)
    pick = lambda i: parts[i] if len(parts) > i else None  # noqa: E731
    return (rel, run.get("benchmark") or pick(0), run.get("project") or pick(1),
            run.get("bug_id") or pick(2), run.get("kind"), run.get("status"),
            run.get("started"), run.get("seconds"), tests.get("total"), failed,
            run.get("parent"))


def _index(home: Home, rel: str, run: dict) -> None:
    with contextlib.closing(_connect(home)) as con, con:
        con.execute(_INSERT, _row(rel, run, tuple(rel.split("/")[:3])))


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _count_attempts(folder: Path) -> int:
    root = folder / "attempts"
    return sum(1 for d in root.iterdir() if d.is_dir()) if root.is_dir() else 0


class RunWriter:
    """Grava uma execução em `.<carimbo>-<kind>.partial` e a publica atomicamente em finish()."""

    def __init__(self, home: Home, bug: Bug, kind: str, parent: str | None = None):
        self.home = home
        self.bug = bug
        self.kind = kind
        self.parent = parent
        parts = (_safe(bug.benchmark), _safe(bug.project), _safe(bug.bug_id))
        base = home.runs.joinpath(*parts)
        base.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        self.started = now.isoformat(timespec="seconds")
        self._t0 = time.monotonic()
        stamp = f"{now:%Y%m%d-%H%M%S}-{_safe(kind)}"
        n = 1
        while True:
            name = stamp if n == 1 else f"{stamp}-{n}"
            final = base / name
            partial = base / f".{name}.partial"
            if not final.exists():
                try:
                    partial.mkdir()  # falha se outro writer já reservou o nome
                    break
                except FileExistsError:
                    pass
            n += 1
        self.dir = partial
        self._final = final
        self.rel = "/".join((*parts, name))
        self._done = False
        (self.dir / _MANIFEST).write_text(json.dumps({
            "bug": bug.key, "benchmark": bug.benchmark, "project": bug.project,
            "bug_id": bug.bug_id, "kind": kind, "parent": parent, "started": self.started,
            "pid": os.getpid(), "pid_started": process_start(os.getpid()),
        }, ensure_ascii=False), encoding="utf-8")

    # -- gravação --
    def _dest(self, rel: str) -> Path:
        dest = self.dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        return dest

    def attempt_dir(self, n: int) -> Path:
        d = self.dir / "attempts" / str(n)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_json(self, rel: str, obj) -> None:
        self._dest(rel).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

    def write_text(self, rel: str, text: str) -> None:
        self._dest(rel).write_text(text, encoding="utf-8")

    def copy_into(self, src: Path, rel: str) -> None:
        src = Path(src)
        dest = self._dest(rel)
        if src.is_dir():
            shutil.copytree(long_path(src), long_path(dest), dirs_exist_ok=True)
        else:
            shutil.copy2(long_path(src), long_path(dest))

    def gzip_into(self, src: Path, rel: str) -> None:
        """Comprime `src` em `rel` (o chamador inclui o `.gz` no nome)."""
        with open(src, "rb") as f_in, gzip.open(self._dest(rel), "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)

    def archive_checkout(self, src_dir: Path) -> str:
        """Arquiva o checkout inteiro (com target/); tar.zst se possível, senão zip."""
        src_dir = Path(src_dir)
        try:
            import zstandard
        except ImportError:
            zstandard = None
        if zstandard is not None:
            name = "checkout.tar.zst"
            with open(self._dest(name), "wb") as raw:
                with zstandard.ZstdCompressor().stream_writer(raw) as zw:
                    with tarfile.open(fileobj=zw, mode="w|") as tar:
                        tar.add(long_path(src_dir), arcname=".")
            return name
        name = "checkout.zip"
        with zipfile.ZipFile(self._dest(name), "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as zf:
            root = lp(src_dir)
            for item in sorted(root.rglob("*")):
                if item.is_file():
                    zf.write(item, item.relative_to(root).as_posix())
        return name

    # -- publicação --
    def finish(self, run: dict) -> Path:
        if self._done:
            raise RuntimeError("finish() já foi chamado nesta execução")
        self._done = True
        doc = {
            "kind": self.kind,
            "bug": self.bug.key,
            "benchmark": self.bug.benchmark,
            "project": self.bug.project,
            "bug_id": self.bug.bug_id,
            "status": "desconhecido",
            "started": self.started,
            "finished": datetime.now().isoformat(timespec="seconds"),
            "seconds": round(time.monotonic() - self._t0, 3),
            "attempts": _count_attempts(self.dir),
            "parent": self.parent,
        }
        doc.update(run)
        (self.dir / "run.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        (self.dir / _MANIFEST).unlink(missing_ok=True)
        os.replace(self.dir, self._final)
        self.dir = self._final
        _lock_files(self._final)
        _index(self.home, self.rel, doc)
        return self._final

    def __enter__(self) -> RunWriter:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None and not self._done:
            self.finish({"status": "desconhecido", "error": repr(exc)[-1500:]})
        return False


# -- consultas --
def run_dir(home: Home, rel: str) -> Path:
    return home.runs.joinpath(*rel.split("/"))


def load_run(home: Home, rel: str) -> dict:
    return json.loads((run_dir(home, rel) / "run.json").read_text(encoding="utf-8"))


def list_runs(home: Home, benchmark=None, project=None, bug=None, kind=None) -> list[dict]:
    where, args = [], []
    for col, val in (("benchmark", benchmark), ("project", project), ("bug", bug), ("kind", kind)):
        if val is not None:
            where.append(f"{col} = ?")
            args.append(val)
    sql = "SELECT * FROM runs" + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY started DESC, path DESC"
    with contextlib.closing(_connect(home)) as con:
        return [dict(r) for r in con.execute(sql, args)]


def reindex(home: Home) -> int:
    """Reconstrói a tabela varrendo runs/**/run.json."""
    rows = []
    if home.runs.is_dir():
        for f in sorted(home.runs.glob("*/*/*/*/run.json")):
            parts = f.parent.relative_to(home.runs).parts
            if any(p.startswith(".") for p in parts):
                continue  # .partial nunca é execução válida
            try:
                run = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            rows.append(_row("/".join(parts), run, parts[:3]))
    with contextlib.closing(_connect(home)) as con, con:
        con.execute("DELETE FROM runs")
        con.executemany(_INSERT, rows)
    return len(rows)


def recover_partials(home: Home) -> list[str]:
    """Toda .partial órfã vira execução final com status `interrompido`."""
    recovered: list[str] = []
    if not home.runs.is_dir():
        return recovered
    for partial in sorted(home.runs.glob("*/*/*/.*.partial")):
        if not partial.is_dir():
            continue
        base = partial.parent
        man = _read_json(partial / _MANIFEST)
        if pid_alive(man.get("pid"), man.get("pid_started")) and man.get("pid") != os.getpid():
            continue    # dono ainda vivo (outra instância): não é órfã. O próprio PID é órfã: quem recupera segura a trava
        stem = partial.name[1:-len(".partial")]
        final, n = base / stem, 1
        while final.exists():
            n += 1
            final = base / f"{stem}-{n}"
        parts = base.relative_to(home.runs).parts
        m = re.fullmatch(r"\d{8}-\d{6}-(.+?)(?:-\d+)?", stem)
        doc = _read_json(partial / "run.json")
        if not doc:  # sem run.json completo: monta o `interrompido` a partir do manifesto
            try:
                started = datetime.fromtimestamp(partial.stat().st_ctime).isoformat(timespec="seconds")
            except OSError:
                started = None
            doc = {"kind": man.get("kind") or (m.group(1) if m else "desconhecido"),
                   "bug": man.get("bug") or "/".join(parts),
                   "benchmark": man.get("benchmark") or parts[0],
                   "project": man.get("project") or parts[1],
                   "bug_id": man.get("bug_id") or parts[2],
                   "status": "interrompido", "started": man.get("started") or started,
                   "finished": datetime.now().isoformat(timespec="seconds"), "seconds": None,
                   "attempts": _count_attempts(partial), "parent": man.get("parent"),
                   "error": "execução interrompida antes de terminar"}
            (partial / "run.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        (partial / _MANIFEST).unlink(missing_ok=True)
        os.replace(partial, final)
        _lock_files(final)
        rel = "/".join((*parts, final.name))
        _index(home, rel, doc)
        recovered.append(rel)
    return recovered
