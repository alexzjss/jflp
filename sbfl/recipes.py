"""Receitas por projeto: correções que funcionaram, em home/recipes/<benchmark>/<projeto>.toml."""
from __future__ import annotations

import fnmatch
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .adapters.base import Bug
from .home import Home, toml_key as _key, toml_value as _value
from .run_maven import AttemptConfig


@dataclass
class Recipe:
    match: str | list[str]
    kind: str
    fixes: list[str] = field(default_factory=list)
    params: dict = field(default_factory=dict)
    jdk: int | None = None
    maven: str | None = None
    note: str = ""
    source: Path = Path()


def _file(home: Home, bug: Bug) -> Path:
    return home.recipes / bug.benchmark / f"{bug.project}.toml"


def _ids(r: Recipe) -> list[str]:
    return [r.match] if isinstance(r.match, str) else list(r.match)


def _rank(r: Recipe, bug_id: str) -> int:
    """3 = ID exato, 2 = glob, 1 = "*", 0 = não casa."""
    ids = _ids(r)
    if bug_id in ids:
        return 3
    if any(m != "*" and fnmatch.fnmatchcase(bug_id, m) for m in ids):
        return 2
    return 1 if "*" in ids else 0


def _load(path: Path) -> list[Recipe]:
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    out = []
    for t in data.get("recipe", []):
        out.append(Recipe(t.get("match", "*"), t.get("kind", "*"), list(t.get("fixes") or []),
                          dict(t.get("params") or {}), t.get("jdk"), t.get("maven"),
                          t.get("note", ""), path))
    return out


def find(home: Home, bug: Bug, kind: str) -> Recipe | None:
    """Receita de maior prioridade para o bug (empate: a mais recente do arquivo)."""
    best, best_rank = None, 0
    for r in _load(_file(home, bug)):
        if r.kind not in (kind, "*"):
            continue
        rank = _rank(r, bug.bug_id)
        if rank and rank >= best_rank:
            best, best_rank = r, rank
    return best


def _inline(v) -> str:
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_inline(x)}" for k, x in v.items()) + " }" if v else "{}"
    if isinstance(v, (list, tuple, set, frozenset)):
        items = sorted(v) if isinstance(v, (set, frozenset)) else v
        return "[" + ", ".join(_inline(x) for x in items) + "]"
    return _value(v)


def save(home: Home, bug: Bug, kind: str, scope: str, cfg: AttemptConfig,
         glob: str | None = None, note: str = "") -> Path:
    """Acrescenta uma receita ao arquivo do projeto, sem reescrever as existentes."""
    if scope == "bug":
        match = bug.bug_id
    elif scope == "glob":
        if not glob:
            raise ValueError("scope 'glob' exige o padrão")
        match = glob
    elif scope == "project":
        match = "*"
    else:
        raise ValueError(f"scope inválido: {scope}")
    lines = ["[[recipe]]", f"match = {_inline(match)}", f"kind = {_inline(kind)}",
             f"fixes = {_inline(sorted(cfg.fixes_on))}"]
    if cfg.params:
        lines.append(f"params = {_inline(cfg.params)}")
    if cfg.jdk is not None:
        lines.append(f"jdk = {cfg.jdk}")
    if cfg.maven:
        lines.append(f"maven = {_inline(cfg.maven)}")
    if note:
        lines.append(f"note = {_inline(note)}")
    path = _file(home, bug)
    path.parent.mkdir(parents=True, exist_ok=True)
    prev = path.read_text(encoding="utf-8") if path.exists() else ""
    sep = "" if not prev else ("\n" if prev.endswith("\n") else "\n\n")
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(sep + "\n".join(lines) + "\n")
    return path
