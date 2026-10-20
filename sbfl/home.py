from __future__ import annotations

import copy
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import config as C

# Valores padrão das configurações do usuário (config.toml).
DEFAULTS = {
    "tools": {"jdks": {}, "mavens": {}},  # overrides manuais: {"11": "C:/..."}
    "jaguar2": {"version": "0.0.2", "jacoco": "0.8.7", "surefire": "3.5.3",
                "compiler": "3.8.0", "junit": "4.12"},
    "jaguar1": {"lib": "", "timeout_s": 3600, "max_retries": 6, "jvm_args": []},
    "run": {"test_scope": "relevant", "timeout_s": 7200, "archive_checkout": True,
            "max_attempts": 6, "maven_opts": "",
            "skip_flags": list(C.DEFAULTS["maven"]["skip_flags"])},
    "bugsjar": {"root": "", "projects": [],
                "url_template": "https://github.com/bugs-dot-jar/{project}.git"},
    "defects4j": {"home": "", "wsl_distro": "",
                  "projects": ["Lang", "Math", "Time", "Chart", "Closure", "Mockito", "Jsoup"]},
    "ui": {"port": 0},
}

# Comentários escritos acima de cada seção no config.toml inicial.
_NOTES = {
    "tools": "# Caminhos manuais de JDKs e Mavens, ex.: jdks = {\"11\" = \"C:/Java/jdk-11\"}",
    "jaguar2": "# Versões dos plugins usados pelo perfil Jaguar 2",
    "jaguar1": "# Perfil Jaguar 1 (JDK 8 + Maven 3.6.3)",
    "run": "# Execuções: escopo de testes, tempos limite e flags de Maven",
    "bugsjar": "# Projetos do BugsJar",
    "defects4j": "# Defects4J via WSL",
    "ui": "# Interface web (0 = porta automática)",
}

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class Home:
    """Pasta-raiz do JFLP Studio e suas subpastas."""

    root: Path

    @property
    def tools(self) -> Path:
        return self.root / "tools"

    @property
    def m2(self) -> Path:
        return self.root / "m2"

    @property
    def work(self) -> Path:
        return self.root / "work"

    @property
    def runs(self) -> Path:
        return self.root / "runs"

    @property
    def recipes(self) -> Path:
        return self.root / "recipes"

    @property
    def datasets(self) -> Path:
        return self.root / "datasets"

    @property
    def db_path(self) -> Path:
        return self.root / "jflp.db"

    @property
    def config_path(self) -> Path:
        return self.root / "config.toml"

    @classmethod
    def default(cls) -> Home:
        env = os.environ.get("JFLP_HOME")
        if env:
            return cls(Path(env))
        if os.name == "nt":
            base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
            return cls(Path(base) / "jflp")
        return cls(Path.home() / ".jflp")

    def ensure(self) -> Home:
        """Cria as pastas e, se faltar, grava um config.toml comentado."""
        for folder in (self.tools, self.m2, self.work, self.runs, self.recipes, self.datasets):
            folder.mkdir(parents=True, exist_ok=True)
        if not self.config_path.exists():
            load_settings(self).save()
        return self


class Settings:
    """Configurações do usuário, lidas e gravadas em config.toml."""

    def __init__(self, home: Home, data: dict):
        self.home = home
        self.data = data

    def get(self, section: str, key: str, default=None):
        return self.data.get(section, {}).get(key, default)

    def section(self, name: str) -> dict:
        return dict(self.data.get(name, {}))

    def set(self, section: str, key: str, value) -> None:
        self.data.setdefault(section, {})[key] = value

    def save(self) -> None:
        text = "# Configurações do JFLP Studio.\n\n"
        body = _dump(self.data, _NOTES)
        self.home.root.mkdir(parents=True, exist_ok=True)
        self.home.config_path.write_text(text + body, encoding="utf-8")


def load_settings(home: Home) -> Settings:
    """Lê config.toml (se existir) e mescla com DEFAULTS."""
    data = copy.deepcopy(DEFAULTS)
    if home.config_path.exists():
        with open(home.config_path, "rb") as f:
            _merge(data, tomllib.load(f))
    return Settings(home, data)


def _merge(base: dict, over: dict) -> None:
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def _key(k) -> str:
    s = str(k)
    return s if _BARE_KEY.fullmatch(s) else _basic(s)


def _basic(s: str) -> str:
    out = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif _CONTROL.fullmatch(ch):
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _str(s: str) -> str:
    # Caminhos do Windows ficam como string literal (aspas simples), sem escapes.
    if "\\" in s and "'" not in s and not _CONTROL.search(s):
        return "'" + s + "'"
    return _basic(s)


def _value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return _str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    raise TypeError(f"valor não suportado no config.toml: {v!r}")


def _dump(data: dict, notes: dict | None = None) -> str:
    lines: list[str] = []

    def emit(table: dict, path: tuple, note: str | None):
        if path:
            if lines:
                lines.append("")
            if note:
                lines.append(note)
            lines.append("[" + ".".join(_key(p) for p in path) + "]")
        for k, v in table.items():
            if not isinstance(v, dict):
                lines.append(f"{_key(k)} = {_value(v)}")
        for k, v in table.items():
            if isinstance(v, dict):
                emit(v, path + (k,), (notes or {}).get(k) if not path else None)

    emit(data, (), None)
    return "\n".join(lines) + "\n"
