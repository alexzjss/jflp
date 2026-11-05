"""Busca automática: lista ordenada e limitada de configurações alternativas após uma falha de infraestrutura."""
from __future__ import annotations

import copy
import json
from dataclasses import replace

from . import status as st
from .diagnose import Diagnosis, Suggestion, _keep
from .run_maven import UNBLOCK_HTTP, AttemptConfig, reactor_flags
from .toolchain.detect import find_jdks, find_mavens
from .toolchain.profiles import PROFILES

SUREFIRE = "org.apache.maven.plugins:maven-surefire-plugin"
SUREFIRE_VERSIONS = ("3.5.3", "3.2.5", "3.0.0-M5")
COMPILER_VERSIONS = ("3.8.0", "3.1", "3.11.0")
JDKS = (8, 17, 11)
REACTOR_FIXES = frozenset({"javac7", "scr-jdk7"})   # mudam o reactor sem passar por reactor_flags fixas


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _with_fix(first: AttemptConfig, fix: str, params: dict, detected: set[str]) -> AttemptConfig:
    # fixes_on vazio = "detectadas": ao acrescentar uma correção, mantém as detectadas também
    on = set(first.fixes_on) or set(detected)
    on.add(fix)
    merged = dict(first.params)
    if params:
        merged[fix] = deep_merge(first.params.get(fix) or {}, params)
    return replace(first, fixes_on=frozenset(on), params=merged)


def _default_jdk(first: AttemptConfig, kind: str, jdk: int | None) -> int | None:
    """JDK igual ao padrão do perfil (e a 1ª tentativa sem JDK fixo) seria uma tentativa duplicada."""
    if jdk is not None and first.jdk is None and PROFILES.get(kind, {}).get("jdk") == jdk:
        return None
    return jdk


def _from_suggestion(first: AttemptConfig, s: Suggestion, kind: str, detected: set[str]) -> AttemptConfig:
    cfg = replace(first)
    if s.fix and (kind == "jaguar2" or s.fix in ("unblock-http", "skip-jacoco")):   # só o Jaguar 2 usa correções do POM
        cfg = _with_fix(cfg, s.fix, s.params, detected)
    if s.jdk is not None:
        cfg = replace(cfg, jdk=_default_jdk(first, kind, s.jdk) or first.jdk)
    if s.maven:
        cfg = replace(cfg, maven=s.maven)
    if s.params.get("test_scope"):
        cfg = replace(cfg, test_scope=s.params["test_scope"])
    return cfg


def effective_jdk_maven(toolenv: dict | None) -> tuple[int | None, str | None]:
    """JDK (major) e Maven (versão) que uma tentativa realmente usou, a partir de `AttemptResult.toolenv`."""
    te = toolenv or {}
    jdk = (te.get("jdk") or {}).get("major")
    mvn = (te.get("maven") or {}).get("version")
    return (int(jdk) if jdk is not None else None), (str(mvn) if mvn else None)


def effective_key(cfg: AttemptConfig, jdk: int | None, maven: str | None) -> str:
    """Chave da configuração EFETIVA: JDK/Maven "padrão" (None) viram os que o perfil de fato resolve."""
    return replace(cfg, jdk=cfg.jdk if cfg.jdk is not None else jdk,
                   maven=cfg.maven if cfg.maven is not None else maven).key()


LEGACY_JDK = 7
LEGACY_MAVEN = "3.0.5"


def _legacy(first: AttemptConfig, kind: str, settings) -> list[AttemptConfig]:
    """Perfil legado (JDK 7 + Maven 3.0.5) para a cobertura; com `legado_offline`, também a versão offline."""
    if kind != "maven" or not first.params.get("cobertura") or not settings.get("cobertura", "legado", False):
        return []
    home = getattr(settings, "home", None)
    if home is None:
        return []
    try:
        has_jdk = any(j.major == LEGACY_JDK for j in find_jdks(home, settings))
        has_mvn = any(".".join(map(str, m.version)) == LEGACY_MAVEN for m in find_mavens(home, settings))
    except Exception:
        return []
    if not (has_jdk and has_mvn):
        return []
    legacy = replace(first, jdk=LEGACY_JDK, maven=LEGACY_MAVEN)
    out = [legacy]
    if settings.get("cobertura", "legado_offline", False):
        out.append(replace(legacy, params={**first.params, "offline": True}))
    return out


def attempt_cap(settings, params: dict, kind: str) -> int:
    """Máximo de tentativas por bug. Cobertura nos Jaguar usa `cobertura.max_tentativas_jaguar` (1 a 8; as
    finais quase nunca resolvem); o Maven mantém o do lote (perfis legados); fora da cobertura vale run.max_attempts."""
    base = int(settings.get("run", "max_attempts", 6))
    if params.get("cobertura") and kind in ("jaguar1", "jaguar2"):
        try:
            n = int(settings.get("cobertura", "max_tentativas_jaguar", 4))
        except (TypeError, ValueError):
            n = 4
        return min(8, max(1, n))
    return max(base, int(params.get("max_attempts") or 0))


def candidates(first: AttemptConfig, diag: Diagnosis, kind: str, settings,
               detected: set[str] | None = None, toolenv: dict | None = None,
               truncate: bool = True) -> list[AttemptConfig]:
    """Sugestões do diagnóstico e depois Surefire, compiler e JDK; sem repetir (pela configuração efetiva,
    com o JDK/Maven que a 1ª tentativa resolveu em `toolenv`) e limitado por run.max_attempts.
    Com `truncate=False` devolve a lista inteira: quem executa aplica o limite só às tentativas que rodam."""
    ejdk, emvn = effective_jdk_maven(toolenv)
    if diag.status == st.PROJETO_INVALIDO:    # erro de escolha da pasta/módulo: nenhuma configuração resolve
        return []
    detected = set(detected or ())
    out = [_from_suggestion(first, s, kind, detected)
           for s in diag.suggestions if s.fix != "exclude-duplicates" and _keep(kind, s.fix, s.maven)]
    for j in JDKS:      # JDK antes de variar plugins: sintomas de bytecode novo pedem JDK 8
        out.append(replace(first, jdk=_default_jdk(first, kind, j) or first.jdk))
    if kind == "jaguar2":
        for v in SUREFIRE_VERSIONS:
            out.append(_with_fix(first, "override-version", {"plugins": {SUREFIRE: v}}, detected))
        for v in COMPILER_VERSIONS:
            out.append(_with_fix(first, "compiler-java", {"version": v}, detected))
    out += _legacy(first, kind, settings)
    seen, result = {effective_key(first, ejdk, emvn)}, []
    for c in out:
        k = effective_key(c, ejdk, emvn)
        if k not in seen:
            seen.add(k)
            result.append(c)
    if not truncate:
        return result
    cap = attempt_cap(settings, first.params, kind)
    limit = max(0, cap - 1)
    return result[:limit]


def reactor_key(cfg: AttemptConfig, jdk: int | None, maven: str | None) -> str:
    """O que decide o resultado do `install` do reactor (POMs originais): JDK e Maven efetivos, o que muda
    os argumentos de settings (unblock-http, mirror da cobertura, offline), as flags do reactor e as correções
    que trocam o javac ou o JDK/Maven só do reactor (javac7, scr-jdk7)."""
    return json.dumps([cfg.jdk if cfg.jdk is not None else jdk,
                       cfg.maven if cfg.maven is not None else maven,
                       UNBLOCK_HTTP in cfg.fixes_on, bool(cfg.params.get("cobertura")),
                       bool(cfg.params.get("offline")), reactor_flags(cfg),
                       sorted(REACTOR_FIXES & set(cfg.fixes_on))],
                      sort_keys=True, default=str)


def _no_surefire(cfg: AttemptConfig, detected: set[str], jdk, maven) -> str:
    """Chave efetiva da configuração sem a versão do Surefire em `override-version`."""
    on = set(cfg.fixes_on) or set(detected)
    params = copy.deepcopy(cfg.params)
    ov = params.get("override-version")
    if isinstance(ov, dict):
        plugins = ov.get("plugins")
        if isinstance(plugins, dict):
            plugins.pop(SUREFIRE, None)
            if not plugins:
                ov.pop("plugins")
        if not ov:
            params.pop("override-version")
            on.discard("override-version")
    return effective_key(replace(cfg, fixes_on=frozenset(on), params=params), jdk, maven)


def surefire_only_variant(cand: AttemptConfig, ran: AttemptConfig, detected: set[str],
                          cand_env: tuple, ran_env: tuple) -> bool:
    """`cand` só difere de `ran` pela versão do Surefire (`override-version`)? `*_env` = (JDK, Maven) efetivos."""
    return (cand.key() != ran.key()
            and _no_surefire(cand, detected, *cand_env) == _no_surefire(ran, detected, *ran_env))
