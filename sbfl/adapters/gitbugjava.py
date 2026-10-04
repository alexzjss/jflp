"""GitBug-Java via CLI (`gitbug-java checkout BID WORK_DIR [--fixed]`).

ATENÇÃO: o GitBug-Java reproduz builds dentro de uma imagem Docker offline (via `act`).
Este adaptador faz o checkout buggy/fixed pelo CLI, deriva o ground truth comparando as
duas árvores e tenta o build LOCAL (Maven/Gradle). Se o seu JDK/dependências locais não
reproduzem o build do container, o status será build_failed. Rodar o Jaguar dentro da
imagem não está implementado."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .. import build as build_mod
from ..errors import StageError
from ..proc import make_env
from ..truth import tree_truth
from .base import Adapter, Bug, Ctx, scan_tests


class GitBugJava(Adapter):
    name = "gitbugjava"

    def _cli(self, *args, check=True) -> str:
        r = subprocess.run([self.cfg.get("gitbugjava", "bin"), *args], capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        if check and r.returncode != 0:
            raise StageError("gitbug_failed", f"gitbug-java {' '.join(args)}: {(r.stderr or r.stdout)[-300:]}")
        return r.stdout

    def list_bugs(self, project=None, bug=None):
        wanted = [project] if project else self.cfg.get("gitbugjava", "projects")
        bugs = []
        for bid in self._cli("bids").split():
            if bug and bug != bid:
                continue
            if wanted and not any(w in bid for w in wanted):
                continue
            bugs.append(Bug(self.name, bid.rsplit("-", 1)[0], bid))
        return bugs

    def prepare(self, bug: Bug, log_path: Path) -> Ctx:
        cfg = self.cfg
        base = cfg.work / "gitbugjava" / bug.bug_id
        b, f = base / "buggy", base / "fixed"
        shutil.rmtree(base, ignore_errors=True)
        base.mkdir(parents=True)
        cleanup = lambda: shutil.rmtree(base, ignore_errors=True)
        try:
            self._cli("checkout", bug.bug_id, str(b))
            self._cli("checkout", bug.bug_id, str(f), "--fixed")
            faults, module = tree_truth(b, f)
            shutil.rmtree(f, ignore_errors=True)
            jh = cfg.section("gitbugjava").get("java_home") or None
            bl = build_mod.build(cfg, b, module, "auto", log_path, make_env(cfg, jh))
            classes, stats = scan_tests(bl.project_dir / bl.tests_dir)
            if not classes:
                raise StageError("junit5_only" if stats["junit5_skipped"] else "no_tests", str(stats))
            return Ctx(bl.project_dir, bl.classes_dir, bl.tests_dir, bl.extra_cp, classes, faults,
                       cleanup, {"module": module, **stats}, jh)
        except BaseException:
            cleanup()
            raise
