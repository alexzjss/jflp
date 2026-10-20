"""Correções nomeadas para problemas conhecidos de projetos legados + base do Jaguar 2."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable

from ..jdk import declared_level
from .pom import DEFAULT_GROUP, PomContext, PomDoc

JAGUAR_GROUP = "br.usp.each.saeg"
JAGUAR_ARTIFACTS = ["jaguar2-junit4", "jaguar2-jacoco-provider", "jaguar2-core",
                    "jaguar2-xml-exporter", "jaguar2-csv-exporter"]
LISTENER = "br.usp.each.saeg.jaguar2.junit.JaguarJUnitRunListener"
SNIFFER_DEFAULT = "1.16"
SUREFIRE = (DEFAULT_GROUP, "maven-surefire-plugin")
COMPILER = (DEFAULT_GROUP, "maven-compiler-plugin")


@dataclass
class Fix:
    id: str
    title: str
    reason: str
    detect: Callable[[PomContext], bool]
    apply: Callable[[PomDoc, PomContext, dict], None]
    default_params: dict = field(default_factory=dict)


# ---------------------------------------------------------------- base (sempre aplicada)
def apply_base(doc: PomDoc, ctx: PomContext) -> None:
    v = ctx.versions
    for artifact in JAGUAR_ARTIFACTS:
        doc.ensure_dependency(JAGUAR_GROUP, artifact, v["version"], "test")

    jac = doc.ensure_plugin("org.jacoco", "jacoco-maven-plugin", v["jacoco"])
    execs = doc.child(jac, "executions", create=True)
    has_goal = any((g.text or "").strip() == "prepare-agent"
                   for g in execs.iter(doc.q("goal")))
    if not has_goal:
        e = ET.SubElement(execs, doc.q("execution"))
        doc.set_child(e, "id", "prepare-agent")
        doc.set_child(doc.child(e, "goals", create=True), "goal", "prepare-agent")

    sf = doc.ensure_plugin(*SUREFIRE, v["surefire"])
    cfg = doc.child(sf, "configuration", create=True)
    props = doc.child(cfg, "properties", create=True)
    props.set("combine.children", "append")
    listener = next(_listener_properties(doc, props), None)
    if listener is None:
        listener = ET.SubElement(props, doc.q("property"))
        doc.set_child(listener, "name", "listener")
        doc.set_child(listener, "value", LISTENER)
    else:
        value = doc.child(listener, "value", create=True)
        current = (value.text or "").strip()
        if LISTENER not in [x.strip() for x in current.split(",")]:
            value.text = f"{current},{LISTENER}" if current else LISTENER

    argline = ctx.surefire_argline
    if argline and "@{argLine}" not in argline and "${argLine}" not in argline:
        doc.set_child(cfg, "argLine", f"@{{argLine}} {argline}")


def _listener_properties(doc: PomDoc, props):
    for p in props.findall(doc.q("property")):
        name = doc.child(p, "name")
        if name is not None and (name.text or "").strip() == "listener":
            yield p


# ---------------------------------------------------------------- detecção
def _detect_sniffer(ctx: PomContext) -> bool:
    return any("animal-sniffer" in t for t in ctx.chain_texts)


def _detect_compiler(ctx: PomContext) -> bool:
    for text in ctx.chain_texts:
        lv = declared_level(text)
        if lv is not None:
            return lv < 8
    return True


def _detect_source_dirs(ctx: PomContext) -> bool:
    return bool((ctx.test_dir and ctx.test_dir != "src/test/java")
                or (ctx.main_dir and ctx.main_dir != "src/main/java"))


def _detect_junit4(ctx: PomContext) -> bool:
    return not ctx.has_junit4


def _never(ctx: PomContext) -> bool:
    return False


# ---------------------------------------------------------------- aplicação
def _apply_sniffer(doc: PomDoc, ctx: PomContext, params: dict) -> None:
    version = params.get("version")
    if not version:
        for text in ctx.chain_texts:
            m = re.search(r"<artifactId>\s*animal-sniffer-maven-plugin\s*</artifactId>\s*"
                          r"<version>\s*([^<\s]+)\s*</version>", text)
            if m:
                version = m.group(1)
                break
    p = doc.ensure_plugin("org.codehaus.mojo", "animal-sniffer-maven-plugin",
                          version or SNIFFER_DEFAULT)
    doc.set_child(doc.child(p, "configuration", create=True), "skip", "true")


def _java_level(jdk: int) -> str:
    return "1.8" if jdk == 8 else str(jdk)


def _apply_compiler(doc: PomDoc, ctx: PomContext, params: dict) -> None:
    p = doc.ensure_plugin(*COMPILER, params.get("version") or ctx.versions["compiler"])
    cfg = doc.child(p, "configuration", create=True)
    level = _java_level(ctx.jdk)
    doc.set_child(cfg, "source", level)
    doc.set_child(cfg, "target", level)
    doc.set_child(cfg, "encoding", "UTF-8")


def _apply_source_dirs(doc: PomDoc, ctx: PomContext, params: dict) -> None:
    if ctx.test_dir and ctx.test_dir != "src/test/java":
        doc.set_build_value("testSourceDirectory", ctx.test_dir)
    if ctx.main_dir and ctx.main_dir != "src/main/java":
        doc.set_build_value("sourceDirectory", ctx.main_dir)


def _apply_junit4(doc: PomDoc, ctx: PomContext, params: dict) -> None:
    doc.ensure_dependency("junit", "junit", ctx.versions["junit"], "test")


def _split(coord: str) -> tuple[str, str]:
    group, _, artifact = coord.partition(":")
    return (group, artifact) if artifact else (DEFAULT_GROUP, group)


def _apply_override(doc: PomDoc, ctx: PomContext, params: dict) -> None:
    for coord, version in (params.get("deps") or {}).items():
        doc.ensure_dependency(*_split(coord), version)
    for coord, version in (params.get("plugins") or {}).items():
        doc.ensure_plugin(*_split(coord), version)


def _apply_excludes(doc: PomDoc, ctx: PomContext, params: dict) -> None:
    p = doc.ensure_plugin(*COMPILER, ctx.versions["compiler"], overwrite=False)
    excludes = doc.path(p, "configuration", "excludes", create=True)
    have = {(e.text or "").strip() for e in excludes}
    for f in params.get("files") or []:
        if f not in have:
            ET.SubElement(excludes, doc.q("exclude")).text = f


FIXES: dict[str, Fix] = {f.id: f for f in [
    Fix("skip-animal-sniffer", "Ignorar o animal-sniffer",
        "O animal-sniffer verifica a API do Java antiga e quebra a compilação em JDKs novos.",
        _detect_sniffer, _apply_sniffer, {}),
    Fix("compiler-java", "Compilar com o JDK atual",
        "O projeto declara Java antigo (ou nada); o maven-compiler-plugin é fixado no JDK escolhido.",
        _detect_compiler, _apply_compiler, {}),
    Fix("source-dirs", "Ajustar pastas de código-fonte",
        "Os fontes estão fora de src/main/java ou src/test/java; o POM gerado aponta para elas "
        "sem mover arquivos.",
        _detect_source_dirs, _apply_source_dirs, {}),
    Fix("junit4", "Adicionar JUnit 4",
        "Não há junit:junit na cadeia de POMs, e o Jaguar 2 só roda testes JUnit 4.",
        _detect_junit4, _apply_junit4, {}),
    Fix("override-version", "Forçar versões",
        "Redeclara versões de dependências e plugins no POM gerado.",
        _never, _apply_override, {"deps": {}, "plugins": {}}),
    Fix("exclude-duplicates", "Excluir classes duplicadas",
        "Exclui da compilação arquivos que duplicam classes.",
        _never, _apply_excludes, {"files": []}),
]}
