"""Uma tentativa do Jaguar 1: compila com o perfil jaguar1 e roda o JaguarRunner com recuperação de crash."""
from __future__ import annotations

import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

from . import proc
from .adapters.base import Ctx, scan_tests
from .home import Home, Settings
from .jaguar import JaguarOptions, run_jaguar
from .outputs import MARK, jaguar1_counts, parse_jaguar1_log, reconcile_jaguar1
from .prepare import Prepared
from .run_maven import (AttemptConfig, AttemptResult, install_reactor, log_phase, module_dir, settings_args,
                        reactor_failure, phase_record, SKIP_JACOCO, JACOCO_SKIP)
from .toolchain.profiles import ToolEnv, resolve


def _fail(log: Path, fase: str, erro: str, seconds: float = 0.0, cmd=None, desc=None) -> AttemptResult:
    return AttemptResult(None, False, seconds, log, {}, {}, cmd or [], desc or {},
                         {"fase": fase, "erro": erro})


def run_jaguar1(home: Home, settings: Settings, prep: Prepared, workdir: Path, cfg: AttemptConfig,
                log: Path, toolenv: ToolEnv | None = None, on_line=None) -> AttemptResult:
    """`toolenv` só existe para injetar um Maven falso nos testes."""
    mod = module_dir(prep, workdir)
    lib = Path(settings.get("jaguar1", "lib") or (home.tools / "jaguar1" / "lib"))
    if not (lib / "jacocoagent.jar").is_file():
        return _fail(log, "jaguar1_lib",
                     f"Biblioteca do Jaguar 1 não encontrada em {lib} (falta jacocoagent.jar); "
                     "configure jaguar1.lib em Ambiente.")
    te = toolenv or resolve("jaguar1", home, settings, pom_dir=mod, jdk=cfg.jdk, maven=cfg.maven)
    desc = te.describe()
    skip = list(settings.get("run", "skip_flags", []) or [])
    if SKIP_JACOCO in cfg.fixes_on:      # JaCoCo do POM do projeto atrapalhando o build
        skip.append(JACOCO_SKIP)
    timeout = settings.get("run", "timeout_s")
    total = 0.0

    sargs = settings_args(home, cfg)
    phases: list[dict] = []
    r = install_reactor(te, settings, prep, workdir, log, on_line, sargs)
    if r is not None:
        total += r["seconds"]
        phases.append(phase_record("reactor", r["command"], te.env))
        failed = reactor_failure(r, log, desc)
        if failed:
            failed.phases = phases
            return failed

    cmd: list = []
    for fase, args in (("compilar", ("test-compile", *skip)),
                       ("dependencias", ("dependency:copy-dependencies",
                                         "-DoutputDirectory=target/dependency", *skip))):
        log_phase(log, fase, on_line)
        cmd = te.mvn(*sargs, *args)
        phases.append(phase_record(fase, cmd, te.env))
        r = proc.run(cmd, cwd=mod, env=te.env, timeout=timeout, log_path=log, on_line=on_line)
        total += r["seconds"]
        if r["rc"] != 0 or r["timed_out"]:
            return AttemptResult(r["rc"], r["timed_out"], round(total, 1), log, {}, {}, cmd, desc,
                                 {"fase": fase}, phases)

    classes_dir, tests_dir = "target/classes", "target/test-classes"
    if cfg.test_scope == "all" or not prep.relevant_tests:   # mesma regra de test_selection_args
        test_classes, _ = scan_tests(mod / tests_dir)
    else:
        test_classes = sorted({t.split("#", 1)[0].split("$", 1)[0] for t in prep.relevant_tests})
    ctx = Ctx(mod, classes_dir, tests_dir, [str(mod / "target" / "dependency" / "*")],
              test_classes, [])
    opts = JaguarOptions(
        lib=lib, java=te.java(), env=te.env,
        timeout_s=settings.get("jaguar1", "timeout_s", 3600),
        max_retries=settings.get("jaguar1", "max_retries", 6),
        jvm_args=list(settings.get("jaguar1", "jvm_args", []) or []))
    out_dir = mod / "target" / "jflp-jaguar1"
    shutil.rmtree(out_dir, ignore_errors=True)   # nada de saída velha de outra tentativa
    out_dir.mkdir(parents=True, exist_ok=True)
    log_phase(log, "jaguar1", on_line)
    meta = run_jaguar(opts, ctx, out_dir)
    total += meta.get("seconds", 0)

    outputs: dict[str, Path] = {}
    xml = out_dir / f"jaguar_{opts.heuristic}.xml"
    if meta.get("status") == "ok" and xml.is_file():
        outputs["jaguar1_xml"] = xml
    tests: dict[str, str] = {}
    jlog, tlog = out_dir / "jaguar.log", out_dir / "jaguar-tests.log"
    if jlog.is_file():
        try:     # resultado de cada teste (última tentativa do JaguarRunner), como no Maven: classe#teste
            tests = parse_jaguar1_log(jlog)
        except OSError:
            tests = {}
    if tlog.is_file():   # o JaguarLogger gravado em arquivo é completo mesmo que um teste tenha trocado System.out
        try:
            tests.update(parse_jaguar1_log(tlog))
        except OSError:
            pass
    if jlog.is_file():   # junta o log do JaguarRunner ao log da tentativa
        with open(log, "a", encoding="utf-8") as f:
            f.write(jlog.read_text(encoding="utf-8", errors="replace"))
    if tlog.is_file():   # e as linhas de teste do arquivo, que valem mais que as do stdout cortado
        try:
            segment = tlog.read_text(encoding="utf-8", errors="replace")
            k = segment.rfind(MARK)
            with open(log, "a", encoding="utf-8") as f:
                f.write(segment[k:] if k >= 0 else segment)
        except OSError:
            pass
    counts = None
    if "jaguar1_xml" in outputs:
        try:
            counts = jaguar1_counts(outputs["jaguar1_xml"])
        except (ValueError, OSError, ET.ParseError):
            counts = None
    known = [t for t in (prep.relevant_tests or []) if "#" in t]
    tests, tcounts, truncated = reconcile_jaguar1(tests, counts, known)
    extra = {"excluded_classes": meta.get("excluded_classes", []), "meta_status": meta.get("status"),
             "attempts": meta.get("attempts", 0), "test_counts": tcounts, "log_truncado": truncated}
    ok = meta.get("status") == "ok"
    return AttemptResult(0 if ok else None, meta.get("status") == "jaguar_timeout", round(total, 1),
                         log, outputs, tests, cmd, desc, extra, phases)
