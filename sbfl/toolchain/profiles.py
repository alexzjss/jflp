from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..home import Home, Settings
from ..jdk import declared_level, pick_home
from . import detect
from .detect import Jdk, Maven, find_jdks, find_mavens

PROFILES = {
    "jaguar2": {"jdk": 11, "maven_min": (3, 9, 9)},
    "jaguar1": {"jdk": 8, "maven": (3, 6, 3), "maven_max": (3, 8, 0)},
    "maven": {"jdk": None, "maven_pref": (3, 6, 3), "maven_min": (3, 6, 0)},
}

# Variáveis do ambiente do usuário que mudariam o comportamento do Maven/JVM.
_STRIPPED = ("M2_HOME", "MAVEN_HOME", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "MAVEN_ARGS")


class MissingTool(Exception):
    """JDK ou Maven pedido não está disponível."""

    def __init__(self, kind: str, wanted: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.wanted = wanted
        self.message = message


@dataclass
class ToolEnv:
    jdk: Jdk
    maven: Maven
    env: dict[str, str]
    m2: Path

    def mvn(self, *args: str) -> list[str]:
        name = "mvn.cmd" if detect._is_windows() else "mvn"
        exe = Path(self.maven.home) / "bin" / name
        return [str(exe), "-B", f"-Dmaven.repo.local={self.m2}", *args]

    def java(self) -> str:
        name = "java.exe" if detect._is_windows() else "java"
        return str(Path(self.jdk.home) / "bin" / name)

    def describe(self) -> dict:
        return {
            "jdk": {"home": str(self.jdk.home), "major": self.jdk.major,
                    "version": self.jdk.version, "source": self.jdk.source},
            "maven": {"home": str(self.maven.home),
                      "version": ".".join(str(n) for n in self.maven.version),
                      "source": self.maven.source},
            "m2": str(self.m2),
        }


def _missing_jdk(major) -> MissingTool:
    return MissingTool("jdk", str(major), f"JDK {major} não encontrado — baixe na tela Ambiente")


def _missing_maven(wanted: str) -> MissingTool:
    return MissingTool("maven", wanted, f"Maven {wanted} não encontrado — baixe na tela Ambiente")


def _vstr(v: tuple) -> str:
    return ".".join(str(n) for n in v)


def _pick_jdk(spec: dict, jdks: list[Jdk], jdk: int | None, pom_dir: Path | None) -> Jdk:
    wanted = jdk if jdk is not None else spec["jdk"]
    if wanted is not None:
        for j in jdks:
            if j.major == wanted:
                return j
        raise _missing_jdk(wanted)
    # perfil maven: menor JDK >= nível do pom (mínimo 8), senão o maior
    level = 8
    pom = Path(pom_dir) / "pom.xml" if pom_dir else None
    if pom is not None and pom.is_file():
        level = declared_level(pom.read_text(encoding="utf-8", errors="replace")) or 8
    if not jdks:
        raise _missing_jdk(max(level, 8))
    homes: dict[int, str] = {}
    for j in jdks:
        homes.setdefault(j.major, str(j.home))
    chosen = pick_home(level, homes)
    return next(j for j in jdks if str(j.home) == chosen)


def _pick_maven(spec: dict, mavens: list[Maven], maven: str | None) -> Maven:
    if maven is not None:
        for m in mavens:
            if _vstr(m.version) == maven:
                return m
        raise _missing_maven(maven)
    pref = spec.get("maven") or spec.get("maven_pref")
    if pref:
        for m in mavens:
            if m.version == pref:
                return m
    lo = spec.get("maven_min", spec.get("maven"))
    hi = spec.get("maven_max", spec.get("maven"))
    ok = [m for m in mavens
          if (lo is None or m.version >= lo) and (hi is None or m.version <= hi)]
    if not ok:
        wanted = _vstr(lo) if lo else "?"
        if spec.get("maven_min") and not spec.get("maven_max"):
            wanted += "+"
        raise _missing_maven(wanted)
    return max(ok, key=lambda m: m.version)


def _build_env(jdk: Jdk, maven: Maven, settings: Settings) -> dict[str, str]:
    env = dict(os.environ)
    for var in _STRIPPED:
        env.pop(var, None)
    env["JAVA_HOME"] = str(jdk.home)
    front = [str(Path(jdk.home) / "bin"), str(Path(maven.home) / "bin")]
    env["PATH"] = os.pathsep.join(front + ([env["PATH"]] if env.get("PATH") else []))
    env["MAVEN_OPTS"] = settings.get("run", "maven_opts", "") or ""
    return env


def resolve(profile: str, home: Home, settings: Settings, pom_dir: Path | None = None,
            jdk: int | None = None, maven: str | None = None) -> ToolEnv:
    """Escolhe JDK e Maven do perfil e monta o ambiente isolado."""
    spec = PROFILES[profile]
    chosen_jdk = _pick_jdk(spec, find_jdks(home, settings), jdk, pom_dir)
    chosen_maven = _pick_maven(spec, find_mavens(home, settings), maven)
    return ToolEnv(chosen_jdk, chosen_maven, _build_env(chosen_jdk, chosen_maven, settings), home.m2)
