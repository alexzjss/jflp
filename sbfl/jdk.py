"""Escolha automática do JDK pelo nível de Java declarado no pom.xml (opcional)."""
from __future__ import annotations

import re
from pathlib import Path

LEVEL_RES = [
    r"<maven\.compiler\.(?:source|target|release)>\s*([^<\s]+)\s*<",
    r"<java\.version>\s*([^<\s]+)\s*<",
    r"<jdk\.version>\s*([^<\s]+)\s*<",
    r"<(?:source|target|release)>\s*([^<\s]+)\s*</(?:source|target|release)>",
]


def parse_level(v: str) -> int | None:
    v = v.strip()
    if v.startswith("1."):
        v = v[2:]
    m = re.match(r"\d+", v)
    return int(m.group()) if m else None   # "${java.version}" não tem dígitos -> None


def declared_level(pom_text: str) -> int | None:
    for rx in LEVEL_RES:
        for m in re.finditer(rx, pom_text):
            lv = parse_level(m.group(1))
            if lv:
                return lv
    return None


def pick_home(level: int, homes: dict) -> str:
    """Menor JDK configurado que seja >= max(level, 8); se nenhum, o maior disponível."""
    cands = sorted((int(k), v) for k, v in homes.items())
    target = max(level, 8)
    for k, v in cands:
        if k >= target:
            return v
    return cands[-1][1]


def auto_java_home(cfg, dirs) -> tuple[str | None, int | None]:
    """(java_home, nível declarado) ou (None, None) se `[java.homes]` não está configurado
    ou o pom não declara o nível."""
    homes = cfg.get("java", "homes") or {}
    if not homes:
        return None, None
    for d in dirs:
        pom = Path(d) / "pom.xml"
        if pom.exists():
            lv = declared_level(pom.read_text(encoding="utf-8", errors="replace"))
            if lv:
                return pick_home(lv, homes), lv
    return None, None
