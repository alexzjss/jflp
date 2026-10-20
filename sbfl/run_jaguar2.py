"""Uma tentativa do Jaguar 2: reactor opcional, jaguar2-pom.xml e `mvn -f jaguar2-pom.xml clean test`."""
from __future__ import annotations

import dataclasses
from pathlib import Path

from . import proc
from .home import Home, Settings
from .outputs import parse_surefire
from .pomgen import generate, write
from .pomgen.fixes import JAGUAR_ARTIFACTS, JAGUAR_GROUP
from .prepare import Prepared
from .run_maven import AttemptConfig, AttemptResult, module_dir, test_selection_args
from .toolchain.profiles import ToolEnv, resolve

FAIL_IGNORE = ["-Dmaven.test.failure.ignore=true", "-DfailIfNoTests=false",
               "-Dsurefire.failIfNoSpecifiedTests=false"]


def log_phase(log: Path, name: str) -> None:
    """Cabeçalho de cada fase no mesmo arquivo de log."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"# jflp fase: {name}\n")


def install_reactor(te: ToolEnv, settings: Settings, prep: Prepared, workdir: Path,
                    log: Path) -> dict | None:
    """Em bug multi-módulo, instala o reactor na raiz com os POMs originais.
    Devolve o resultado do `mvn install` (dict de proc.run) ou None se o módulo é a raiz."""
    if prep.module == ".":
        return None
    log_phase(log, "reactor")
    cmd = te.mvn("install", "-DskipTests", *(settings.get("run", "skip_flags", []) or []))
    r = proc.run(cmd, cwd=workdir, env=te.env, timeout=settings.get("run", "timeout_s"), log_path=log)
    r["command"] = cmd
    return r


def run_jaguar2(home: Home, settings: Settings, prep: Prepared, workdir: Path, cfg: AttemptConfig,
                log: Path, toolenv: ToolEnv | None = None) -> AttemptResult:
    """`toolenv` só existe para injetar um Maven falso nos testes.
    MissingTool de `resolve` sobe para o chamador (como em run_maven)."""
    mod = module_dir(prep, workdir)
    te = toolenv or resolve("jaguar2", home, settings, pom_dir=mod, jdk=cfg.jdk, maven=cfg.maven)
    skip = list(settings.get("run", "skip_flags", []) or [])
    timeout = settings.get("run", "timeout_s")
    desc = te.describe()
    total = 0.0

    r = install_reactor(te, settings, prep, workdir, log)
    if r is not None:
        total += r["seconds"]
        if r["rc"] != 0 or r["timed_out"]:
            return AttemptResult(r["rc"], r["timed_out"], total, log, {}, {}, r["command"], desc,
                                 {"fase": "reactor"})

    log_phase(log, "pom")
    # fixes_on vazio (padrão do AttemptConfig) = "usar as correções detectadas" (None no gerador)
    plan = generate(mod, settings, te.jdk.major, set(cfg.fixes_on) if cfg.fixes_on else None,
                    set(cfg.fixes_off), cfg.params)
    extra: dict = {"pom_plan": dataclasses.asdict(plan)}
    try:
        pom = write(plan, mod)
    except ValueError:
        extra["fase"] = "pom"
        extra["erro"] = "; ".join(plan.warnings) or "não foi possível gerar o jaguar2-pom.xml"
        return AttemptResult(None, False, total, log, {}, {}, [], desc, extra)

    log_phase(log, "jaguar2")
    # O Jaguar 2 só analisa as classes listadas em jaguar2.classesDirs (propriedade de sistema da JVM
    # do Surefire, que repassa as -D do Maven). Sem ela o relatório sai vazio.
    classes_dirs = f"-Djaguar2.classesDirs={mod / 'target' / 'classes'}"
    cmd = te.mvn("-f", "jaguar2-pom.xml", "clean", "test", classes_dirs, *FAIL_IGNORE, *skip,
                 *test_selection_args(prep, cfg.test_scope, workdir))
    # O CSV usa o locale da JVM (decimal "0,7071" em pt-BR, quebrando as colunas): força en-US
    # na JVM do Surefire via JAVA_TOOL_OPTIONS (só nesta chamada).
    env = {**te.env, "JAVA_TOOL_OPTIONS": "-Duser.language=en -Duser.country=US"}
    r = proc.run(cmd, cwd=mod, env=env, timeout=timeout, log_path=log)
    total += r["seconds"]

    outputs: dict[str, Path] = {"pom": pom}
    target = mod / "target"
    for key, rel in (("jaguar2_csv", "jaguar2.csv"), ("jaguar2_xml", "jaguar2.xml"),
                     ("jacoco_exec", "jacoco.exec")):
        if (target / rel).is_file():
            outputs[key] = target / rel
    tests: dict[str, str] = {}
    reports = target / "surefire-reports"
    if reports.is_dir():
        outputs["surefire"] = reports
        tests = parse_surefire(reports)
    return AttemptResult(r["rc"], r["timed_out"], round(total, 1), log, outputs, tests, cmd, desc, extra)


def prefetch_jaguar2(home: Home, settings: Settings, toolenv: ToolEnv | None = None) -> bool:
    """Baixa para o m2 próprio tudo que o Jaguar 2 precisa (uso offline). True se todos deram rc 0."""
    te = toolenv or resolve("jaguar2", home, settings)
    j = settings.get("jaguar2", "version", "0.0.2")
    coords = [f"{JAGUAR_GROUP}:{a}:{j}" for a in JAGUAR_ARTIFACTS]
    coords += [f"org.jacoco:jacoco-maven-plugin:{settings.get('jaguar2', 'jacoco', '0.8.7')}",
               f"org.apache.maven.plugins:maven-surefire-plugin:{settings.get('jaguar2', 'surefire', '3.5.3')}",
               f"junit:junit:{settings.get('jaguar2', 'junit', '4.12')}"]
    cwd = home.work
    cwd.mkdir(parents=True, exist_ok=True)
    ok = True
    for c in coords:
        r = proc.run(te.mvn("dependency:get", f"-Dartifact={c}"), cwd=cwd, env=te.env,
                     timeout=settings.get("run", "timeout_s"))
        ok = ok and r["rc"] == 0 and not r["timed_out"]
    return ok
