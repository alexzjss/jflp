"""Defects4J via CLI `defects4j` (Linux/macOS/WSL; usar Java 8)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ..diffgt import parse_left_hunks, faults_from_hunks
from ..proc import run, make_env
from .base import Adapter, Bug, Ctx, StageError, scan_test_classes


class Defects4J(Adapter):
    name = "d4j"

    def _bin(self) -> str:
        home = self.cfg.get("defects4j", "home")
        return str(Path(home).expanduser() / "framework" / "bin" / "defects4j") if home else "defects4j"

    def _d4j(self, *args, cwd=None, check=True) -> str:
        r = subprocess.run([self._bin(), *args], cwd=cwd, env=make_env(self.cfg),
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if check and r.returncode != 0:
            raise StageError("d4j_failed", f"defects4j {' '.join(args)}: {(r.stderr or r.stdout)[-400:]}")
        return r.stdout

    def list_bugs(self, project=None, bug=None):
        projects = [project] if project else self.cfg.get("defects4j", "projects")
        bugs = []
        for p in projects:
            for bid in self._d4j("bids", "-p", p).split():
                if bug and str(bug) != bid:
                    continue
                bugs.append(Bug("d4j", p, bid))
        return bugs

    def prepare(self, bug: Bug, log_path: Path) -> Ctx:
        cfg = self.cfg
        base = cfg.work / "d4j" / bug.project
        b, f = base / f"{bug.bug_id}b", base / f"{bug.bug_id}f"
        for d in (b, f):
            shutil.rmtree(d, ignore_errors=True)
        base.mkdir(parents=True, exist_ok=True)

        def cleanup():
            shutil.rmtree(b, ignore_errors=True)
            shutil.rmtree(f, ignore_errors=True)

        try:
            self._d4j("checkout", "-p", bug.project, "-v", f"{bug.bug_id}b", "-w", str(b))
            self._d4j("checkout", "-p", bug.project, "-v", f"{bug.bug_id}f", "-w", str(f))
            r = run([self._bin(), "compile", "-w", str(b)], env=make_env(cfg),
                    timeout=cfg.get("maven", "build_timeout_s"), log_path=log_path)
            if r["rc"] != 0:
                raise StageError("build_failed", "".join(r["tail"][-15:]))
            exp = lambda prop: self._d4j("export", "-p", prop, "-w", str(b)).strip()
            src_rel, classes, tests = exp("dir.src.classes"), exp("dir.bin.classes"), exp("dir.bin.tests")
            cp = exp("cp.test")
            trigger = [x for x in self._d4j("export", "-p", "tests.trigger", "-w", str(b)).split() if x]
            if cfg.get("jaguar", "test_scope") == "relevant":
                rel = [x for x in self._d4j("export", "-p", "tests.relevant", "-w", str(b),
                                            check=False).split() if x]
                tcls = rel or scan_test_classes(b / tests)
            else:
                tcls = scan_test_classes(b / tests)
            # diff com o LADO ESQUERDO = versão com defeito
            pre = f"{bug.bug_id}b/{src_rel}/"
            d = subprocess.run(["git", "diff", "--no-index", "-U0", "--no-color", "--no-renames",
                                f"{bug.bug_id}b/{src_rel}", f"{bug.bug_id}f/{src_rel}"],
                               cwd=base, capture_output=True, text=True, encoding="utf-8",
                               errors="replace").stdout

            def path_fn(p):
                return p[len(pre):] if p.startswith(pre) and p.endswith(".java") else None

            hunks = parse_left_hunks(d, path_fn)
            faults = faults_from_hunks(hunks, lambda p: p[:-5].replace("/", "."))
            if not faults:
                raise StageError("no_truth", "diff b/f sem linhas em código de produção")
            shutil.rmtree(f, ignore_errors=True)
            if not tcls:
                raise StageError("no_tests", "sem classes de teste")
            return Ctx(b, classes, tests, [cp], tcls, faults, cleanup,
                       {"trigger_tests": trigger[:50]})
        except BaseException:
            cleanup()
            raise
