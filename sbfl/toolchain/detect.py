from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..envcheck import java_major, maven_version
from ..home import Home, Settings

TIMEOUT = 20
_WIN_JAVA_VENDORS = ("Java", "Eclipse Adoptium", "Zulu", "Microsoft", "Amazon Corretto")


@dataclass
class Jdk:
    home: Path
    major: int
    version: str
    source: str  # "config" | "managed" | "system"


@dataclass
class Maven:
    home: Path
    version: tuple[int, int, int]
    source: str


@dataclass
class WslStatus:
    installed: bool
    distros: list[str]
    d4j: str | None
    message: str


def decode_console(raw: bytes) -> str:
    """Decodifica a saída de um console: UTF-16LE (wsl.exe) ou UTF-8."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    if len(raw) >= 2 and raw[1:2] == b"\x00" and raw.count(b"\x00") >= len(raw) // 4:
        return raw.decode("utf-16-le", errors="replace")
    return raw.decode("utf-8", errors="replace")


def _is_windows() -> bool:
    return os.name == "nt"


def _run(cmd: list[str], env: dict | None = None, merge: bool = True) -> tuple[int, str] | None:
    """Roda um comando; devolve (código, saída) ou None se não executou.

    Com merge=True a saída é stdout+stderr; senão, só stdout."""
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    err = decode_console(p.stderr or b"") if merge else ""
    return p.returncode, decode_console(p.stdout or b"") + err


def _exe(home: Path, name: str) -> Path:
    suffix = {"java": ".exe", "mvn": ".cmd"}[name] if _is_windows() else ""
    return home / "bin" / (name + suffix)


def _real(p: Path) -> str:
    try:
        return os.path.normcase(str(p.resolve()))
    except OSError:
        return os.path.normcase(str(p))


def java_info(java_home: Path) -> tuple[int, str] | None:
    """(major, versão) do JDK em `java_home`, ou None se não funcionar."""
    exe = _exe(Path(java_home), "java")
    if not exe.exists():
        return None
    res = _run([str(exe), "-version"])
    if res is None:
        return None
    major = java_major(res[1])
    if major is None:
        return None
    m = re.search(r'version "([^"]+)"', res[1])
    return major, m.group(1) if m else str(major)


def _system_java_homes() -> list[Path]:
    homes: list[Path] = []
    env = os.environ.get("JAVA_HOME")
    if env:
        homes.append(Path(env))
    which = shutil.which("java")
    if which:
        homes.append(Path(which).resolve().parent.parent)
    if _is_windows():
        base = os.environ.get("ProgramFiles", r"C:\Program Files")
        for vendor in _WIN_JAVA_VENDORS:
            root = Path(base) / vendor
            if root.is_dir():
                homes.extend(sorted(p for p in root.iterdir() if p.is_dir()))
        homes.extend(_registry_java_homes())
    else:
        root = Path("/usr/lib/jvm")
        if root.is_dir():
            homes.extend(sorted(p for p in root.iterdir() if p.is_dir()))
    return homes


def _registry_java_homes() -> list[Path]:
    try:
        import winreg
    except ImportError:
        return []
    out: list[Path] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\JavaSoft") as top:
            for i in range(winreg.QueryInfoKey(top)[0]):
                with winreg.OpenKey(top, winreg.EnumKey(top, i)) as kind:
                    for j in range(winreg.QueryInfoKey(kind)[0]):
                        try:
                            with winreg.OpenKey(kind, winreg.EnumKey(kind, j)) as ver:
                                out.append(Path(winreg.QueryValueEx(ver, "JavaHome")[0]))
                        except OSError:
                            continue
    except OSError:
        pass
    return out


def find_jdks(home: Home, settings: Settings) -> list[Jdk]:
    """JDKs conhecidos, sem repetição: config, gerenciados e do sistema."""
    candidates: list[tuple[Path, str]] = []
    for path in (settings.get("tools", "jdks", {}) or {}).values():
        candidates.append((Path(path), "config"))
    if home.tools.is_dir():
        candidates += [(p, "managed") for p in sorted(home.tools.glob("jdk-*")) if p.is_dir()]
    candidates += [(p, "system") for p in _system_java_homes()]
    found: list[Jdk] = []
    seen: set[str] = set()
    for path, source in candidates:
        key = _real(path)
        if key in seen:
            continue
        seen.add(key)
        info = java_info(path)
        if info:
            found.append(Jdk(path, info[0], info[1], source))
    return found


def maven_info(maven_home: Path, java_home: Path | None = None) -> tuple[int, int, int] | None:
    """Versão do Maven em `maven_home`, ou None. `java_home` vira o JAVA_HOME."""
    exe = _exe(Path(maven_home), "mvn")
    if not exe.exists():
        return None
    env = dict(os.environ)
    if java_home is not None:
        env["JAVA_HOME"] = str(java_home)
    res = _run([str(exe), "-v"], env=env)
    return maven_version(res[1]) if res else None


def _system_maven_homes() -> list[Path]:
    homes: list[Path] = []
    which = shutil.which("mvn")
    if which:
        homes.append(Path(which).resolve().parent.parent)
    for var in ("MAVEN_HOME", "M2_HOME"):
        if os.environ.get(var):
            homes.append(Path(os.environ[var]))
    homes += [Path("/opt/maven"), Path("/usr/share/maven")]
    return homes


def find_mavens(home: Home, settings: Settings) -> list[Maven]:
    """Mavens conhecidos, sem repetição: config, gerenciados e do sistema."""
    candidates: list[tuple[Path, str]] = []
    for path in (settings.get("tools", "mavens", {}) or {}).values():
        candidates.append((Path(path), "config"))
    if home.tools.is_dir():
        candidates += [(p, "managed") for p in sorted(home.tools.glob("apache-maven-*")) if p.is_dir()]
    candidates += [(p, "system") for p in _system_maven_homes()]
    jdks = find_jdks(home, settings)
    java_home = jdks[0].home if jdks else None
    found: list[Maven] = []
    seen: set[str] = set()
    for path, source in candidates:
        key = _real(path)
        if key in seen:
            continue
        seen.add(key)
        ver = maven_info(path, java_home)
        if ver:
            found.append(Maven(path, ver, source))
    return found


def wsl_status(settings: Settings) -> WslStatus:
    """Situação do WSL e do Defects4J dentro dele (só no Windows)."""
    if not _is_windows():
        return WslStatus(False, [], None, "não se aplica")
    res = _run(["wsl.exe", "-l", "-q"])
    if res is None:
        return WslStatus(False, [], None, "WSL não encontrado neste computador.")
    code, text = res
    text = text.replace("\x00", "").strip()
    distros = [ln.strip() for ln in text.splitlines() if ln.strip()] if code == 0 else []
    if not distros:
        detail = text or "nenhuma distribuição instalada"
        return WslStatus(False, [], None, f"WSL indisponível: {detail}")
    distro = _pick_distro(settings, distros)
    d4j_home = (settings.get("defects4j", "home") or "").rstrip("/")
    script = "command -v defects4j"
    if d4j_home:
        script += " || ls " + shlex.quote(d4j_home + "/framework/bin/defects4j")
    r = _run(["wsl", "-d", distro, "--", "bash", "-lc", script], merge=False)
    d4j = None
    if r and r[0] == 0:
        lines = [ln.strip() for ln in r[1].splitlines() if ln.strip()]
        # banners de login vêm antes; o resultado do comando é a última linha
        if lines and lines[-1].endswith("defects4j"):
            d4j = lines[-1]
    msg = f"WSL disponível ({', '.join(distros)})."
    if d4j is None:
        msg += " Defects4J não encontrado na distribuição."
    return WslStatus(True, distros, d4j, msg)


def _pick_distro(settings: Settings, distros: list[str]) -> str:
    """Distro configurada, ou a primeira que não seja do Docker Desktop (sem bash)."""
    chosen = settings.get("defects4j", "wsl_distro")
    if chosen:
        return chosen
    usable = [d for d in distros if not d.lower().startswith("docker-desktop")]
    return (usable or distros)[0]


def defects4j_cmd(settings: Settings, wsl: WslStatus) -> list[str] | None:
    """Prefixo de comando para chamar o Defects4J, ou None se ausente."""
    if _is_windows():
        if not (wsl.installed and wsl.d4j):
            return None
        distro = _pick_distro(settings, wsl.distros)
        return ["wsl", "-d", distro, "--", "bash", "-lc"]
    d4j_home = settings.get("defects4j", "home")
    if d4j_home:
        exe = Path(d4j_home) / "framework" / "bin" / "defects4j"
        if exe.exists():
            return [str(exe)]
    found = shutil.which("defects4j")
    return [found] if found else None
