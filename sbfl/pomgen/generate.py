"""Gera o jaguar2-pom.xml ao lado do pom.xml do módulo (o original nunca é alterado)."""
from __future__ import annotations

import difflib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .fixes import FIXES, apply_base
from .pom import PomDoc, build_context

WARN_JUNIT5 = "projeto só tem JUnit 5: o Jaguar 2 só roda testes JUnit 4"
WARN_NO_POM = "sem pom.xml: Jaguar 2 não suportado"


@dataclass
class PomPlan:
    text: str
    diff: str
    detected: list[str] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)
    params: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    jaguar2_version: str = ""      # versão do Jaguar 2 que entrou no POM gerado


def generate(module_dir: Path, settings, jdk: int, fixes_on: set[str] | None = None,
             fixes_off: set[str] = frozenset(), params: dict | None = None) -> PomPlan:
    module_dir = Path(module_dir)
    params = params or {}
    pom = module_dir / "pom.xml"
    if not pom.is_file():
        return PomPlan("", "", warnings=[WARN_NO_POM])

    try:                                # pom.xml ilegível: plano vazio com aviso (não levanta)
        original = PomDoc.load(pom).serialize()
        doc = PomDoc.load(pom)
    except ET.ParseError as e:
        return PomPlan("", "", warnings=[f"pom.xml inválido: {e}"])
    ctx = build_context(module_dir, settings, jdk)

    warnings: list[str] = []
    detected = [fid for fid, fx in FIXES.items() if fx.detect(ctx)]
    wanted = set(detected) if fixes_on is None else set(fixes_on)
    for unknown in sorted(wanted - set(FIXES)):
        warnings.append(f"correção desconhecida ignorada: {unknown}")
    wanted -= set(fixes_off)

    apply_base(doc, ctx)
    applied: list[str] = []
    used: dict = {}
    for fid, fx in FIXES.items():       # ordem fixa do registro
        if fid in wanted:
            p = {**fx.default_params, **(params.get(fid) or {})}
            try:
                fx.apply(doc, ctx, p)
            except ValueError as e:     # parâmetro inválido: avisa e segue sem a correção
                warnings.append(f"correção {fid} ignorada: {e}")
                continue
            applied.append(fid)
            used[fid] = p

    if ctx.has_junit5_only:
        warnings.append(WARN_JUNIT5)
    text = doc.serialize()
    diff = "\n".join(difflib.unified_diff(original.splitlines(), text.splitlines(),
                                          "pom.xml", "jaguar2-pom.xml", lineterm=""))
    return PomPlan(text, diff, detected, applied, used, warnings,
                   str((ctx.versions or {}).get("version") or ""))


def write(plan: PomPlan, module_dir: Path) -> Path:
    if not plan.text:
        raise ValueError("plano sem texto: nada para gravar")
    out = Path(module_dir) / "jaguar2-pom.xml"
    out.write_text(plan.text, encoding="utf-8", newline="\n")
    return out
