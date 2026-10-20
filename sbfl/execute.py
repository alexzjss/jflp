"""Orquestrador: prepara o bug, roda as tentativas (com busca automática), classifica e grava uma execução imutável."""
from __future__ import annotations

import importlib.metadata
import platform
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import recipes
from . import status as st
from .adapters.base import Bug
from .autosearch import candidates
from .diagnose import Diagnosis, classify
from .errors import StageError
from .home import Home, Settings
from .prepare import _rmtree, prepare, working_copy
from .run_jaguar1 import run_jaguar1
from .run_jaguar2 import run_jaguar2
from .run_maven import AttemptConfig, AttemptResult, run_maven
from .store import RunWriter
from .toolchain.profiles import MissingTool

RUNNERS = {"maven": "run_maven", "jaguar2": "run_jaguar2", "jaguar1": "run_jaguar1"}


@dataclass
class RunOptions:
    test_scope: str | None = None
    fixes_on: frozenset | None = None
    fixes_off: frozenset = frozenset()
    params: dict = field(default_factory=dict)
    jdk: int | None = None
    maven: str | None = None
    auto_search: bool = True
    archive_checkout: bool | None = None
    parent: str | None = None
    log_cb: Callable[[str], None] | None = None


def _runner(kind: str):
    # procurado no módulo a cada chamada (os testes trocam as funções)
    return globals()[RUNNERS[kind]]


def _first_config(settings: Settings, recipe, opts: RunOptions) -> AttemptConfig:
    fixes: set[str] = set(recipe.fixes) if recipe else set()
    if opts.fixes_on:
        fixes |= set(opts.fixes_on)
    params = {**(recipe.params if recipe else {}), **opts.params}
    return AttemptConfig(
        frozenset(fixes), frozenset(opts.fixes_off), params,
        opts.jdk if opts.jdk is not None else (recipe.jdk if recipe else None),
        opts.maven or (recipe.maven if recipe else None),
        opts.test_scope or settings.get("run", "test_scope", "relevant"))


def _count_tests(tests: dict) -> dict:
    vals = list(tests.values())
    return {"total": len(vals), **{k: vals.count(k) for k in ("passed", "failed", "error", "skipped")}}


def _git_out(*args: str) -> str | None:
    try:
        r = subprocess.run(["git", *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _env_info(prep, res: AttemptResult | None) -> dict:
    git = _git_out("--version")
    commit = None
    if (prep.pristine / ".git").exists():
        commit = _git_out("-C", str(prep.pristine), "rev-parse", "HEAD")
    try:
        version = importlib.metadata.version("jflp")
    except importlib.metadata.PackageNotFoundError:
        version = "dev"
    return {"os": platform.platform(), "python": sys.version.split()[0],
            "git": git.splitlines()[0] if git else None,
            "toolenv": res.toolenv if res else {}, "pristine_commit": commit,
            "jflp_version": version}


def _copy_outputs(w: RunWriter, kind: str, res: AttemptResult, last: int) -> None:
    names = {"surefire": "outputs/surefire-reports", "jaguar2_csv": "outputs/jaguar2.csv",
             "jaguar2_xml": "outputs/jaguar2.xml", "jacoco_exec": "outputs/jacoco.exec",
             "jaguar1_xml": "outputs/jaguar1.xml"}
    (w.dir / "outputs").mkdir(exist_ok=True)
    for key, rel in names.items():
        src = res.outputs.get(key)
        if src and Path(src).exists():
            w.copy_into(Path(src), rel)
    gz = w.dir / "attempts" / str(last) / "log.txt.gz"
    if kind == "jaguar1" and gz.exists():   # o log da tentativa já inclui o do JaguarRunner
        shutil.copy2(gz, w.dir / "outputs" / "jaguar1.log.gz")
    pom = res.outputs.get("pom")
    if pom and Path(pom).exists():
        w.copy_into(Path(pom), "pom/" + Path(pom).name)
    diff = (res.extra.get("pom_plan") or {}).get("diff")
    if diff:
        w.write_text("pom/jaguar2-pom.diff", diff + "\n")


def _write_attempt(w: RunWriter, n: int, cfg: AttemptConfig, diag: Diagnosis,
                   res: AttemptResult, log: Path) -> None:
    if log.exists():
        w.gzip_into(log, f"attempts/{n}/log.txt.gz")
        log.unlink()
    w.write_json(f"attempts/{n}/attempt.json", {
        "config": cfg.to_dict(), "diagnosis": diag.to_dict(),
        "command": [str(c) for c in res.command], "toolenv": res.toolenv,
        "status": diag.status, "rc": res.rc, "timed_out": res.timed_out,
        "seconds": res.seconds, "extra": res.extra})


def _stopped(w: RunWriter, status: str, cause: str, **extra) -> Path:
    return w.finish({"status": status, "diagnosis": {"status": status, "cause": cause, "evidence": [],
                                                      "suggestions": [], "warnings": []},
                     "attempts": extra.pop("attempts", 0), **extra})


def execute(home: Home, settings: Settings, bug: Bug, kind: str, opts: RunOptions) -> str:
    """Executa um bug de ponta a ponta e devolve o `rel` da execução gravada."""
    cb = opts.log_cb
    max_attempts = int(settings.get("run", "max_attempts", 6))
    with RunWriter(home, bug, kind, opts.parent) as w:
        try:
            prep = prepare(home, settings, bug, w.dir / "attempts" / "0-prepare.log")
        except StageError as e:
            _stopped(w, e.status, e.detail)
            return w.rel

        recipe = recipes.find(home, bug, kind)
        first = _first_config(settings, recipe, opts)
        base = {"test_scope": first.test_scope, "relevant_tests": prep.relevant_tests,
                "truth": [f.to_dict() for f in prep.truth],
                "recipe": ({"match": recipe.match, "kind": recipe.kind, "fixes": recipe.fixes,
                            "params": recipe.params, "jdk": recipe.jdk, "maven": recipe.maven,
                            "note": recipe.note, "source": str(recipe.source)} if recipe else None)}

        configs: list[AttemptConfig] = []
        queue: list[AttemptConfig] = []
        cfg: AttemptConfig | None = first
        diag: Diagnosis | None = None
        res: AttemptResult | None = None
        workdir: Path | None = None
        try:
            n = 0
            while cfg is not None:
                n += 1
                configs.append(cfg)
                if cb:
                    cb(f"# jflp tentativa {n}")
                workdir = working_copy(prep, home)
                log = w.attempt_dir(n) / "log.txt"
                try:
                    res = _runner(kind)(home, settings, prep, workdir, cfg, log, on_line=cb)
                except MissingTool as e:
                    status = st.JDK_INCOMPATIVEL if e.kind == "jdk" else st.DESCONHECIDO
                    _stopped(w, status, e.message, attempts=n - 1,
                             attempt_configs=[c.to_dict() for c in configs], **base)
                    return w.rel
                diag = classify(kind, res)
                _write_attempt(w, n, cfg, diag, res, log)
                stop = (st.is_success(diag.status) or not opts.auto_search or n >= max_attempts
                        or diag.status == st.TIMEOUT)
                if n == 1 and not stop:
                    detected = set((res.extra.get("pom_plan") or {}).get("detected") or [])
                    queue = candidates(first, diag, kind, settings, detected)
                if stop or not queue:
                    break
                cfg = queue.pop(0)
                _rmtree(workdir)
                workdir = None

            _finalize(settings, w, kind, prep, opts, res, diag, workdir, configs, base)
        finally:
            if workdir is not None:
                _rmtree(workdir)
    return w.rel


def _finalize(settings, w: RunWriter, kind, prep, opts, res: AttemptResult, diag: Diagnosis,
              workdir: Path, configs, base) -> None:
    _copy_outputs(w, kind, res, len(configs))
    w.write_json("env.json", _env_info(prep, res))
    w.write_text("command.txt", shlex.join(str(c) for c in res.command) + "\n" if res.command else "")
    warnings = list(diag.warnings)
    run = {"status": diag.status, "diagnosis": diag.to_dict(), "warnings": warnings,
           "tests": _count_tests(res.tests), "attempts": len(configs),
           "attempt_configs": [c.to_dict() for c in configs], **base}
    archive = opts.archive_checkout if opts.archive_checkout is not None         else settings.get("run", "archive_checkout", True)
    if archive:
        try:
            run["checkout"] = w.archive_checkout(workdir)
        except Exception as e:  # o arquivamento nunca invalida a execução
            warnings.append(f"não foi possível arquivar o checkout: {e}")
    w.finish(run)
