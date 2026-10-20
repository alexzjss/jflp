"""Uma tentativa de `mvn clean test` puro numa cópia de trabalho, com seleção de testes relevantes."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import proc
from .home import Home, Settings
from .outputs import parse_surefire
from .prepare import Prepared
from .toolchain.profiles import ToolEnv, resolve

MAX_TEST_ARG = 6000
INCLUDES_FILE = "jflp-tests.txt"


@dataclass
class AttemptConfig:
    fixes_on: frozenset[str] = frozenset()
    fixes_off: frozenset[str] = frozenset()
    params: dict = field(default_factory=dict)
    jdk: int | None = None
    maven: str | None = None
    test_scope: str = "relevant"

    def to_dict(self) -> dict:
        return {"fixes_on": sorted(self.fixes_on), "fixes_off": sorted(self.fixes_off),
                "params": self.params, "jdk": self.jdk, "maven": self.maven,
                "test_scope": self.test_scope}

    def key(self) -> str:
        """Texto estável para deduplicar tentativas equivalentes."""
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False, default=str)


@dataclass
class AttemptResult:
    rc: int | None
    timed_out: bool
    seconds: float
    log: Path
    outputs: dict[str, Path] = field(default_factory=dict)
    tests: dict[str, str] = field(default_factory=dict)
    command: list[str] = field(default_factory=list)
    toolenv: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)


def module_dir(prep: Prepared, workdir: Path) -> Path:
    return workdir if prep.module == "." else workdir / prep.module


def test_selection_args(prep: Prepared, scope: str, workdir: Path) -> list[str]:
    """`-Dtest=...` com os testes relevantes; lista longa vai para um includesFile no módulo."""
    if scope == "all" or not prep.relevant_tests:  # None e [] = sem seleção
        return []
    arg = "-Dtest=" + ",".join(prep.relevant_tests)
    if len(arg) <= MAX_TEST_ARG:
        return [arg]
    base = module_dir(prep, workdir)
    lines = sorted({t.split("$", 1)[0].replace(".", "/") + ".java" for t in prep.relevant_tests})
    base.mkdir(parents=True, exist_ok=True)
    with open(base / INCLUDES_FILE, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return [f"-Dsurefire.includesFile={INCLUDES_FILE}"]


UNBLOCK_HTTP = "unblock-http"
_UNBLOCK_XML = """<?xml version="1.0" encoding="UTF-8"?>
<settings>
  <mirrors>
    <mirror>
      <id>maven-default-http-blocker</id>
      <mirrorOf>external:dummy:*</mirrorOf>
      <name>jflp: desbloqueio http</name>
      <url>http://0.0.0.0/</url>
      <blocked>false</blocked>
    </mirror>
  </mirrors>
</settings>
"""


def settings_args(home: Home, cfg: AttemptConfig) -> list[str]:
    """`-s <settings>` que desliga o bloqueio de repositórios http:// do Maven >= 3.8.1 (correção unblock-http)."""
    if UNBLOCK_HTTP not in cfg.fixes_on:
        return []
    path = home.tools / "jflp-settings-unblock.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_UNBLOCK_XML, encoding="utf-8", newline="\n")
    return ["-s", str(path)]


def run_maven(home: Home, settings: Settings, prep: Prepared, workdir: Path, cfg: AttemptConfig,
              log: Path, toolenv: ToolEnv | None = None, on_line=None) -> AttemptResult:
    mod = module_dir(prep, workdir)
    te = toolenv or resolve("maven", home, settings, pom_dir=mod, jdk=cfg.jdk, maven=cfg.maven)
    cmd = te.mvn(*settings_args(home, cfg), "clean", "test", *(settings.get("run", "skip_flags", []) or []),
                 "-Dmaven.test.failure.ignore=true", "-DfailIfNoTests=false",
                 "-Dsurefire.failIfNoSpecifiedTests=false",
                 *test_selection_args(prep, cfg.test_scope, workdir))
    log.parent.mkdir(parents=True, exist_ok=True)
    r = proc.run(cmd, cwd=mod, env=te.env, timeout=settings.get("run", "timeout_s"), log_path=log,
                 on_line=on_line)
    outputs: dict[str, Path] = {}
    tests: dict[str, str] = {}
    reports = mod / "target" / "surefire-reports"
    if reports.is_dir():
        outputs["surefire"] = reports
        tests = parse_surefire(reports)
    return AttemptResult(r["rc"], r["timed_out"], r["seconds"], log, outputs, tests, cmd,
                         te.describe(), {})


def log_phase(log: Path, name: str, on_line=None) -> None:
    """Cabeçalho de cada fase no mesmo arquivo de log."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"# jflp fase: {name}\n")
    if on_line:
        try:
            on_line(f"# jflp fase: {name}")
        except Exception:
            pass


def install_reactor(te: ToolEnv, settings: Settings, prep: Prepared, workdir: Path,
                    log: Path, on_line=None, extra: list | None = None) -> dict | None:
    """Em bug multi-módulo, instala o reactor na raiz com os POMs originais.
    Devolve o resultado do `mvn install` (dict de proc.run + "command") ou None se o módulo é a raiz."""
    if prep.module == ".":
        return None
    log_phase(log, "reactor", on_line)
    cmd = te.mvn(*(extra or []), "install", "-DskipTests", *(settings.get("run", "skip_flags", []) or []))
    r = proc.run(cmd, cwd=workdir, env=te.env, timeout=settings.get("run", "timeout_s"), log_path=log,
                 on_line=on_line)
    r["command"] = cmd
    return r


def reactor_failure(r: dict, log: Path, toolenv: dict) -> AttemptResult | None:
    """AttemptResult de uma tentativa que parou no reactor, ou None se o install deu certo."""
    if r["rc"] == 0 and not r["timed_out"]:
        return None
    return AttemptResult(r["rc"], r["timed_out"], r["seconds"], log, {}, {}, r["command"], toolenv,
                         {"fase": "reactor"})
