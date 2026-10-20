"""Busca automática: lista ordenada e limitada de configurações alternativas após uma falha de infraestrutura."""
from __future__ import annotations

import copy
from dataclasses import replace

from .diagnose import Diagnosis, Suggestion
from .run_maven import AttemptConfig

SUREFIRE = "org.apache.maven.plugins:maven-surefire-plugin"
SUREFIRE_VERSIONS = ("3.5.3", "3.2.5", "3.0.0-M5")
COMPILER_VERSIONS = ("3.8.0", "3.1", "3.11.0")
JDKS = (11, 17, 8)


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _with_fix(first: AttemptConfig, fix: str, params: dict, detected: set[str]) -> AttemptConfig:
    # fixes_on vazio = "detectadas": ao acrescentar uma correção, mantém as detectadas também
    on = set(first.fixes_on) or set(detected)
    on.add(fix)
    merged = dict(first.params)
    if params:
        merged[fix] = _merge(first.params.get(fix) or {}, params)
    return replace(first, fixes_on=frozenset(on), params=merged)


def _from_suggestion(first: AttemptConfig, s: Suggestion, kind: str, detected: set[str]) -> AttemptConfig:
    cfg = replace(first)
    if s.fix and kind == "jaguar2":   # só o Jaguar 2 usa correções do POM
        cfg = _with_fix(cfg, s.fix, s.params, detected)
    if s.jdk is not None:
        cfg = replace(cfg, jdk=s.jdk)
    if s.maven:
        cfg = replace(cfg, maven=s.maven)
    if s.params.get("test_scope"):
        cfg = replace(cfg, test_scope=s.params["test_scope"])
    return cfg


def candidates(first: AttemptConfig, diag: Diagnosis, kind: str, settings,
               detected: set[str] | None = None) -> list[AttemptConfig]:
    """Sugestões do diagnóstico e depois Surefire, compiler e JDK; sem repetir e limitado por run.max_attempts."""
    detected = set(detected or ())
    out = [_from_suggestion(first, s, kind, detected)
           for s in diag.suggestions if s.fix != "exclude-duplicates"]
    if kind == "jaguar2":
        for v in SUREFIRE_VERSIONS:
            out.append(_with_fix(first, "override-version", {"plugins": {SUREFIRE: v}}, detected))
        for v in COMPILER_VERSIONS:
            out.append(_with_fix(first, "compiler-java", {"version": v}, detected))
    for j in JDKS:
        out.append(replace(first, jdk=j))
    seen, result = {first.key()}, []
    for c in out:
        if c.key() not in seen:
            seen.add(c.key())
            result.append(c)
    limit = max(0, int(settings.get("run", "max_attempts", 6)) - 1)
    return result[:limit]
