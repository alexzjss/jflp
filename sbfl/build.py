"""Estratégias de build: Maven, Gradle ou comandos customizados (Ant, Docker, scripts...)."""
from __future__ import annotations

import os
import shlex
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .errors import StageError
from .proc import run


@dataclass
class Built:
    project_dir: Path
    classes_dir: str
    tests_dir: str
    extra_cp: list = field(default_factory=list)


def resolve_tool(name: str) -> str:
    """No Windows, `mvn` é `mvn.cmd` e o Popen sem shell não o encontra pelo nome curto;
    shutil.which respeita o PATHEXT e devolve o caminho completo."""
    return shutil.which(name) or name


def split_cmd(cmd: str, posix: bool | None = None) -> list[str]:
    """shlex em modo POSIX come as barras invertidas de caminhos do Windows."""
    return shlex.split(cmd, posix=(os.name != "nt") if posix is None else posix)


def detect(d: Path) -> str | None:
    if (d / "pom.xml").exists():
        return "maven"
    if any((d / n).exists() for n in ("build.gradle", "build.gradle.kts", "settings.gradle",
                                      "settings.gradle.kts", "gradlew")):
        return "gradle"
    return None


def _must(r, status="build_failed"):
    if r["rc"] != 0:
        raise StageError(status, "".join(r["tail"][-15:]))


# ---------------------------------------------------------------- Maven
def build_maven(cfg, wt: Path, module: str, log, env) -> Built:
    mod_dir = wt if module == "." else wt / module
    mvn = resolve_tool(cfg.get("java", "mvn"))
    skips = list(cfg.get("maven", "skip_flags"))
    lr = cfg.get("maven", "local_repo")
    if lr:
        skips.append(f"-Dmaven.repo.local={Path(lr).expanduser().resolve()}")
    tmo = cfg.get("maven", "build_timeout_s")
    install = [mvn, "-B", "-q", "install", "-DskipTests", *skips]
    if module != ".":
        install += ["-pl", module, "-am"]
    _must(run(install, cwd=wt, env=env, timeout=tmo, log_path=log))
    _must(run([mvn, "-B", "-q", "dependency:copy-dependencies",
               "-DoutputDirectory=target/dependency", *skips],
              cwd=mod_dir, env=env, timeout=tmo, log_path=log))
    return Built(mod_dir, "target/classes", "target/test-classes",
                 [str(Path("target/dependency") / "*")])


# ---------------------------------------------------------------- Gradle
GRADLE_INIT = """\
allprojects { p ->
  p.afterEvaluate {
    if (p.plugins.hasPlugin('java')) {
      p.tasks.register('sbflInfo') {
        doLast {
          println "SBFL_MAIN=" + p.sourceSets.main.output.classesDirs.files.join(File.pathSeparator)
          println "SBFL_TEST=" + p.sourceSets.test.output.classesDirs.files.join(File.pathSeparator)
          println "SBFL_CP=" + p.sourceSets.test.runtimeClasspath.asPath
        }
      }
    }
  }
}
"""


def parse_gradle_info(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        for k in ("SBFL_MAIN", "SBFL_TEST", "SBFL_CP"):
            if line.startswith(k + "="):
                out[k] = line.split("=", 1)[1].strip()
    return out


def gradle_path(module: str) -> str:
    return "" if module == "." else ":" + module.strip("/").replace("/", ":") + ":"


def build_gradle(cfg, wt: Path, module: str, log, env) -> Built:
    mod_dir = wt if module == "." else wt / module
    gw = wt / ("gradlew.bat" if os.name == "nt" else "gradlew")
    g = [str(gw)] if gw.exists() else [resolve_tool("gradle")]
    tmo = cfg.get("maven", "build_timeout_s")
    prefix = gradle_path(module)
    _must(run([*g, "-q", f"{prefix}testClasses"], cwd=wt, env=env, timeout=tmo, log_path=log))
    with tempfile.NamedTemporaryFile("w", suffix=".gradle", delete=False) as f:
        f.write(GRADLE_INIT)
    try:
        r = run([*g, "-q", "-I", f.name, f"{prefix}sbflInfo"], cwd=wt, env=env, timeout=tmo, log_path=log)
    finally:
        os.unlink(f.name)
    _must(r)
    info = parse_gradle_info("".join(r["tail"]))
    if not info.get("SBFL_MAIN") or not info.get("SBFL_TEST"):
        raise StageError("build_failed", "Gradle não informou classesDirs (plugin 'java' ausente?)")

    def rel(dirs: str) -> str:
        first = next((d for d in dirs.split(os.pathsep) if d), "")
        try:
            return os.path.relpath(first, mod_dir)
        except ValueError:
            return first

    return Built(mod_dir, rel(info["SBFL_MAIN"]), rel(info["SBFL_TEST"]),
                 [info.get("SBFL_CP", "")] if info.get("SBFL_CP") else [])


# ---------------------------------------------------------------- Custom
def build_custom(cfg, wt: Path, module: str, log, env, spec: dict) -> Built:
    """spec: {"cmds": ["ant compile", ...], "classes": "build/classes", "tests": "build/test",
              "classpath": ["lib/*"], "cwd": "."}  — {wt} e {module_dir} são substituídos."""
    mod_dir = wt if module == "." else wt / module
    sub = lambda s: s.replace("{wt}", str(wt)).replace("{module_dir}", str(mod_dir))
    cwd = wt / spec.get("cwd", ".")
    for c in spec.get("cmds", []):
        _must(run(split_cmd(sub(c)), cwd=cwd, env=env,
                  timeout=cfg.get("maven", "build_timeout_s"), log_path=log))
    base = wt / spec["cwd"] if spec.get("cwd") else mod_dir
    return Built(base, spec["classes"], spec["tests"], [sub(x) for x in spec.get("classpath", [])])


def build(cfg, wt: Path, module: str, kind: str, log, env, custom: dict | None = None) -> Built:
    if kind == "custom":
        return build_custom(cfg, wt, module, log, env, custom or {})
    mod_dir = wt if module == "." else wt / module
    if kind in (None, "", "auto"):
        kind = detect(mod_dir) or detect(wt)
        if kind is None:
            raise StageError("unknown_build", f"sem pom.xml/build.gradle em {mod_dir} nem em {wt}")
        if module != "." and not detect(mod_dir):
            module = "."   # patch fora de um módulo com build próprio: usa a raiz
    if kind == "maven":
        return build_maven(cfg, wt, module, log, env)
    if kind == "gradle":
        return build_gradle(cfg, wt, module, log, env)
    raise StageError("unknown_build", f"build desconhecido: {kind}")
