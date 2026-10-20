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
from .autosearch import candidates, deep_merge
from .diagnose import Diagnosis, classify
from .errors import StageError
from .home import Home, Settings
from .pomgen import generate as pom_generate
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
    params = deep_merge(recipe.params if recipe else {}, opts.params)
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
        # última tentativa REAL (que rodou): é ela que define status, saídas e checkout
        diag: Diagnosis | None = None
        res: AttemptResult | None = None
        real_n = 0
        keep: Path | None = None       # cópia de trabalho da última tentativa real
        missing: MissingTool | None = None
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
                    r = _runner(kind)(home, settings, prep, workdir, cfg, log, on_line=cb)
                except MissingTool as e:
                    _rmtree(workdir)
                    w.write_json(f"attempts/{n}/attempt.json", {"config": cfg.to_dict(), "skipped": e.message})
                    if cb:
                        cb(f"# jflp tentativa {n} ignorada: {e.message}")
                    if n == 1:
                        missing = e
                        break
                    cfg = queue.pop(0) if queue and n < max_attempts else None
                    continue
                except BaseException:
                    _rmtree(workdir)
                    raise
                if keep is not None:
                    _rmtree(keep)
                res, keep, real_n = r, workdir, n
                diag = classify(kind, res)
                _write_attempt(w, n, cfg, diag, res, log)
                stop = (st.is_success(diag.status) or not opts.auto_search or n >= max_attempts
                        or diag.status == st.TIMEOUT)
                if n == 1 and not stop:
                    queue = candidates(first, diag, kind, settings, _detected(prep, settings, kind, res))
                cfg = None if stop or not queue else queue.pop(0)

            if missing is not None:
                status = st.JDK_INCOMPATIVEL if missing.kind == "jdk" else st.DESCONHECIDO
                w.write_json("env.json", _env_info(prep, None))
                w.write_text("command.txt", "")
                _stopped(w, status, missing.message, attempts=1,
                         attempt_configs=[c.to_dict() for c in configs], **base)
            else:
                _finalize(settings, w, kind, prep, opts, res, diag, keep, configs, real_n, base)
        finally:
            if keep is not None:
                _rmtree(keep)
    return w.rel


def _detected(prep, settings, kind: str, res: AttemptResult) -> set[str]:
    """Correções detectadas no POM; se a 1ª tentativa falhou antes de gerá-lo, calcula sem gravar nada."""
    plan = res.extra.get("pom_plan")
    if plan is not None:
        return set(plan.get("detected") or [])
    if kind != "jaguar2":
        return set()
    try:
        mod = prep.pristine if prep.module == "." else prep.pristine / prep.module
        return set(pom_generate(mod, settings, 11).detected)
    except Exception:
        return set()


def _finalize(settings, w: RunWriter, kind, prep, opts, res: AttemptResult, diag: Diagnosis,
              workdir: Path, configs, real_n: int, base) -> None:
    _copy_outputs(w, kind, res, real_n)
    w.write_json("env.json", _env_info(prep, res))
    w.write_text("command.txt", shlex.join(str(c) for c in res.command) + "\n" if res.command else "")
    warnings = list(diag.warnings)
    run = {"status": diag.status, "diagnosis": diag.to_dict(), "warnings": warnings,
           "tests": _count_tests(res.tests), "attempts": len(configs),
           "attempt_configs": [c.to_dict() for c in configs], **base}
    archive = (opts.archive_checkout if opts.archive_checkout is not None
               else settings.get("run", "archive_checkout", True))
    if archive:
        try:
            run["checkout"] = w.archive_checkout(workdir)
        except Exception as e:  # o arquivamento nunca invalida a execução
            warnings.append(f"não foi possível arquivar o checkout: {e}")
    w.finish(run)
