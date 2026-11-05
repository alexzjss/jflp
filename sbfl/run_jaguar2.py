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
from .run_maven import (AttemptConfig, AttemptResult, install_reactor, log_phase, module_dir, phase_record,
                        reactor_flags, settings_args, reactor_failure, test_selection_args, UNBLOCK_HTTP)
from .toolchain.profiles import ToolEnv, find_jdk_with_javac, javac_of, resolve

# O CSV usa o locale da JVM (decimal "0,7071" em pt-BR, quebrando as colunas): força en-US na JVM do
# Surefire via JAVA_TOOL_OPTIONS (só nesta chamada). O perfil já remove o JAVA_TOOL_OPTIONS do usuário.
LOCALE_OPTS = "-Duser.language=en -Duser.country=US"
FAIL_IGNORE = ["-Dmaven.test.failure.ignore=true", "-DfailIfNoTests=false",
               "-Dsurefire.failIfNoSpecifiedTests=false"]


JAVAC7 = "javac7"
SCR_JDK7 = "scr-jdk7"
REACTOR_MAVEN = "3.6.3"        # último Maven que roda em Java 7


def with_javac7(home: Home, settings: Settings, cfg: AttemptConfig) -> AttemptConfig:
    """Com a correção `javac7` ligada, localiza o JDK 7 (MissingTool se não houver) e acrescenta aos parâmetros
    o executável do javac e as flags do reactor (`maven.compiler.fork`, `compiler.fork` - propriedade que o POM do
    Camel usa em `<fork>` - e `maven.compiler.executable`). A `scr-jdk7` já exige o JDK 7 e o código desse tipo
    de projeto costuma ter o mesmo problema de javac estrito (Oak: `reference to assertEquals is ambiguous`), então
    ela liga também a compilação do POM gerado no javac 7 (sem flags no reactor, que já roda no JDK 7)."""
    if JAVAC7 not in cfg.fixes_on and SCR_JDK7 not in cfg.fixes_on:
        return cfg
    exe = str(javac_of(find_jdk_with_javac(home, settings, 7)))
    sub = {**(cfg.params.get(JAVAC7) or {}), "executable": exe}
    if JAVAC7 in cfg.fixes_on:
        sub["reactor_flags"] = ["-Dmaven.compiler.fork=true", "-Dcompiler.fork=true",
                                f"-Dmaven.compiler.executable={exe}"]
    return dataclasses.replace(cfg, fixes_on=frozenset(cfg.fixes_on) | {JAVAC7},
                               params={**cfg.params, JAVAC7: sub})


def reactor_toolenv(home: Home, settings: Settings, te: ToolEnv, cfg: AttemptConfig, mod: Path) -> ToolEnv:
    """Com `scr-jdk7`, o reactor (e só ele) roda no JDK 7 com Maven 3.6.3 e TLS 1.2 explícito (o Java 7 não o liga
    sozinho); MissingTool sobe se faltar o JDK 7 ou o Maven 3.6.3."""
    if SCR_JDK7 not in cfg.fixes_on:
        return te
    r7 = resolve("maven", home, settings, pom_dir=mod, jdk=7, maven=REACTOR_MAVEN)
    r7.env["MAVEN_OPTS"] = " ".join(x for x in (r7.env.get("MAVEN_OPTS"), "-Dhttps.protocols=TLSv1.2") if x)
    return r7


def run_jaguar2(home: Home, settings: Settings, prep: Prepared, workdir: Path, cfg: AttemptConfig,
                log: Path, toolenv: ToolEnv | None = None, on_line=None) -> AttemptResult:
    """`toolenv` só existe para injetar um Maven falso nos testes.
    MissingTool de `resolve` sobe para o chamador (como em run_maven)."""
    mod = module_dir(prep, workdir)
    cfg = with_javac7(home, settings, cfg)
    te = toolenv or resolve("jaguar2", home, settings, pom_dir=mod, jdk=cfg.jdk, maven=cfg.maven)
    rte = te if toolenv else reactor_toolenv(home, settings, te, cfg, mod)
    skip = list(settings.get("run", "skip_flags", []) or [])
    timeout = settings.get("run", "timeout_s")
    desc = te.describe()
    total = 0.0

    sargs = settings_args(home, cfg)
    phases: list[dict] = []
    rflags = reactor_flags(cfg)
    r = install_reactor(rte, settings, prep, workdir, log, on_line, sargs, rflags)
    if r is not None:
        total += r["seconds"]
        phases.append(phase_record("reactor", r["command"], rte.env))
        failed = reactor_failure(r, log, rte.describe())
        if failed:
            failed.phases = phases
            return failed

    log_phase(log, "pom", on_line)
    # fixes_on vazio (padrão do AttemptConfig) = "usar as correções detectadas" (None no gerador)
    # correções de execução (unblock-http) não são do POM; se só restarem elas, vale "detectadas"
    pom_fixes = set(cfg.fixes_on) - {UNBLOCK_HTTP}
    plan = generate(mod, settings, te.jdk.major, pom_fixes or None, set(cfg.fixes_off), cfg.params)
    extra: dict = {"pom_plan": dataclasses.asdict(plan)}
    try:
        pom = write(plan, mod)
    except ValueError:
        extra["fase"] = "pom"
        extra["erro"] = "; ".join(plan.warnings) or "não foi possível gerar o jaguar2-pom.xml"
        return AttemptResult(None, False, total, log, {}, {}, [], desc, extra, phases)

    log_phase(log, "jaguar2", on_line)
    # O Jaguar 2 só analisa as classes listadas em jaguar2.classesDirs (propriedade de sistema da JVM
    # do Surefire, que repassa as -D do Maven). Sem ela o relatório sai vazio.
    classes_dirs = f"-Djaguar2.classesDirs={mod / 'target' / 'classes'}"
    cmd = te.mvn(*sargs, "-f", "jaguar2-pom.xml", "clean", "test", classes_dirs, *FAIL_IGNORE, *skip, *rflags,
                 *test_selection_args(prep, cfg.test_scope, workdir))
    env = {**te.env, "JAVA_TOOL_OPTIONS": LOCALE_OPTS}
    phases.append(phase_record("jaguar2", cmd, env))
    r = proc.run(cmd, cwd=mod, env=env, timeout=timeout, log_path=log, on_line=on_line)
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
    return AttemptResult(r["rc"], r["timed_out"], round(total, 1), log, outputs, tests, cmd, desc, extra,
                         phases)


def prefetch_jaguar2(home: Home, settings: Settings, toolenv: ToolEnv | None = None) -> bool:
    """Baixa para o m2 próprio tudo que o Jaguar 2 precisa (uso offline). True se todos deram rc 0."""
    te = toolenv or resolve("jaguar2", home, settings)
    j = settings.get("jaguar2", "version", "0.0.2")
    coords = [f"{JAGUAR_GROUP}:{a}:{j}" for a in JAGUAR_ARTIFACTS]
    sf = settings.get("jaguar2", "surefire", "3.5.3")
    ju = settings.get("jaguar2", "junit", "4.12")
    coords += [f"org.jacoco:jacoco-maven-plugin:{settings.get('jaguar2', 'jacoco', '0.8.7')}",
               f"org.apache.maven.plugins:maven-surefire-plugin:{sf}",
               f"org.apache.maven.surefire:surefire-junit4:{sf}",
               f"org.apache.maven.plugins:maven-compiler-plugin:{settings.get('jaguar2', 'compiler', '3.8.0')}",
               f"junit:junit:{ju}"]
    cwd = home.work
    cwd.mkdir(parents=True, exist_ok=True)
    ok = True
    log = home.work / "prefetch_jaguar2.log"
    for c in coords:
        log_phase(log, f"prefetch {c}")
        r = proc.run(te.mvn("dependency:get", f"-Dartifact={c}"), cwd=cwd, env=te.env,
                     timeout=settings.get("run", "timeout_s"), log_path=log)
        if r["rc"] != 0 or r["timed_out"]:
            ok = False
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"# jflp falhou: {c}\n")
    return ok
