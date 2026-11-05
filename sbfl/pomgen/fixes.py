"""Correções nomeadas para problemas conhecidos de projetos legados + base do Jaguar 2."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable

from ..jdk import declared_level
from .pom import DEFAULT_GROUP, PomContext, PomDoc, junit_declared, junit_is_old, junit_main_scope

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

    inherited = _chain_docs(ctx)
    jac_id = None if _has_prepare_agent(doc, False) else _chain_prepare_agent_id(inherited)
    jac = doc.ensure_plugin("org.jacoco", "jacoco-maven-plugin", v["jacoco"])
    execs = doc.child(jac, "executions", create=True)
    has_goal = any((g.text or "").strip() == "prepare-agent"
                   for g in execs.iter(doc.q("goal")))
    if not has_goal:
        e = ET.SubElement(execs, doc.q("execution"))
        if jac_id != "default":        # sem <id> = execução "default" do Maven
            doc.set_child(e, "id", jac_id or "prepare-agent")
        doc.set_child(doc.child(e, "goals", create=True), "goal", "prepare-agent")

    sf = doc.ensure_plugin(*SUREFIRE, v["surefire"])
    cfg = doc.child(sf, "configuration", create=True)
    props = doc.child(cfg, "properties", create=True)
    own = next(_listener_properties(doc, props), None)
    chain_value = None if own is not None else _chain_listener(inherited)
    if own is not None:
        value = doc.child(own, "value", create=True)
        value.text = _with_listener((value.text or "").strip())
    elif chain_value is not None:
        # herdado do pai/pluginManagement: uma única chave `listener` com o valor herdado + o nosso
        if LISTENER not in _split_list(chain_value):
            listener = ET.SubElement(props, doc.q("property"))
            doc.set_child(listener, "name", "listener")
            doc.set_child(listener, "value", _with_listener(chain_value))
    else:
        props.set("combine.children", "append")
        listener = ET.SubElement(props, doc.q("property"))
        doc.set_child(listener, "name", "listener")
        doc.set_child(listener, "value", LISTENER)

    argline = ctx.surefire_argline
    if argline and "@{argLine}" not in argline and "${argLine}" not in argline:
        doc.set_child(cfg, "argLine", f"@{{argLine}} {argline}")


def _split_list(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def _with_listener(current: str) -> str:
    items = _split_list(current)
    return ",".join(items if LISTENER in items else items + [LISTENER])


def _chain_docs(ctx: PomContext) -> list[PomDoc]:
    """POMs da cadeia (o do módulo primeiro) já lidos; os ilegíveis são ignorados."""
    docs = []
    for text in ctx.chain_texts:
        try:
            docs.append(PomDoc.from_text(text))
        except ET.ParseError:
            pass
    return docs


def _plugin_entries(doc: PomDoc, group: str, artifact: str, management_too: bool = True):
    for mgmt in ((False, True) if management_too else (False,)):
        p = doc.find_plugin(group, artifact, in_management=mgmt)
        if p is not None:
            yield p


def _chain_listener(docs: list[PomDoc]) -> str | None:
    """Valor de `listener` do Surefire mais próximo do módulo na cadeia (plugins, depois management)."""
    for d in docs:
        for p in _plugin_entries(d, *SUREFIRE):
            props = d.path(p, "configuration", "properties")
            if props is None:
                continue
            for prop in _listener_properties(d, props):
                return (d.child(prop, "value").text or "").strip() if d.child(prop, "value") is not None else ""
    return None


def _has_prepare_agent(doc: PomDoc, management_too: bool) -> bool:
    return _prepare_agent_id(doc, management_too) is not None


def _prepare_agent_id(doc: PomDoc, management_too: bool) -> str | None:
    for p in _plugin_entries(doc, "org.jacoco", "jacoco-maven-plugin", management_too):
        for e in p.findall(f"{doc.q('executions')}/{doc.q('execution')}"):
            if any((g.text or "").strip() == "prepare-agent" for g in e.iter(doc.q("goal"))):
                return (doc.child(e, "id").text or "").strip() if doc.child(e, "id") is not None else "default"
    return None


def _chain_prepare_agent_id(docs: list[PomDoc]) -> str | None:
    for d in docs:
        found = _prepare_agent_id(d, True)
        if found:
            return found
    return None


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
    if not ctx.has_junit4:
        return True
    return junit_is_old(junit_declared(ctx.chain_texts)[0])


def _detect_rat(ctx: PomContext) -> bool:
    return any("apache-rat-plugin" in t for t in ctx.chain_texts)


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


def _apply_rat(doc: PomDoc, ctx: PomContext, params: dict) -> None:
    version = params.get("version")
    if not version:
        for text in ctx.chain_texts:
            m = re.search(r"<artifactId>\s*apache-rat-plugin\s*</artifactId>\s*"
                          r"<version>\s*([^<\s]+)\s*</version>", text)
            if m:
                version = m.group(1)
                break
    p = doc.ensure_plugin("org.apache.rat", "apache-rat-plugin", version, overwrite=False)
    cfg = doc.child(p, "configuration", create=True)
    doc.set_child(cfg, "skip", "true")
    doc.set_child(cfg, "numUnapprovedLicenses", "999999")


def _java_level(jdk: int) -> str:
    return "1.8" if jdk == 8 else str(jdk)


def _target_level(ctx: PomContext) -> int:
    """Bytecode alvo: max(8, nível declarado). Plugins de build antigos não leem classes mais novas."""
    for text in ctx.chain_texts:
        lv = declared_level(text)
        if lv is not None:
            return max(8, lv)
    return 8


def _apply_compiler(doc: PomDoc, ctx: PomContext, params: dict) -> None:
    p = doc.ensure_plugin(*COMPILER, params.get("version") or ctx.versions["compiler"])
    cfg = doc.child(p, "configuration", create=True)
    level = _java_level(int(params.get("target") or _target_level(ctx)))
    doc.set_child(cfg, "source", level)
    doc.set_child(cfg, "target", level)
    doc.set_child(cfg, "encoding", "UTF-8")


def _apply_source_dirs(doc: PomDoc, ctx: PomContext, params: dict) -> None:
    if ctx.test_dir and ctx.test_dir != "src/test/java":
        doc.set_build_value("testSourceDirectory", ctx.test_dir)
    if ctx.main_dir and ctx.main_dir != "src/main/java":
        doc.set_build_value("sourceDirectory", ctx.main_dir)


def _apply_junit4(doc: PomDoc, ctx: PomContext, params: dict) -> None:
    version = ctx.versions["junit"]
    # JUnit usado pelo código de produção: troca só a versão; forçar `test` quebra a compilação do main
    doc.ensure_dependency("junit", "junit", version, None if junit_main_scope(ctx.chain_texts) else "test")
    prop = junit_declared(ctx.chain_texts)[1]
    if prop:        # versão vem de uma propriedade: quem usa dependencyManagement também passa a ver 4.x
        doc.set_child(doc.child(doc.root, "properties", create=True), prop, version)


def _split(coord: str, is_plugin: bool) -> tuple[str, str]:
    group, _, artifact = coord.partition(":")
    if artifact:
        return group, artifact
    if is_plugin:
        return DEFAULT_GROUP, group
    raise ValueError(f"dependência deve ser 'grupo:artefato': {coord}")


def _apply_override(doc: PomDoc, ctx: PomContext, params: dict) -> None:
    deps = [(_split(c, False), v) for c, v in (params.get("deps") or {}).items()]
    plugins = [(_split(c, True), v) for c, v in (params.get("plugins") or {}).items()]
    for (g, a), version in deps:        # valida tudo antes de alterar o documento
        doc.ensure_dependency(g, a, version)
    for (g, a), version in plugins:
        doc.ensure_plugin(g, a, version)


def _apply_excludes(doc: PomDoc, ctx: PomContext, params: dict) -> None:
    p = doc.ensure_plugin(*COMPILER, ctx.versions["compiler"], overwrite=False)
    excludes = doc.path(p, "configuration", "excludes", create=True)
    have = {(e.text or "").strip() for e in excludes}
    for f in params.get("files") or []:
        if f not in have:
            ET.SubElement(excludes, doc.q("exclude")).text = f


def _legacy_level(ctx: PomContext) -> str:
    """Nível declarado limitado ao que o javac 7 aceita (1.5 a 1.7); sem declaração, 1.7."""
    for text in ctx.chain_texts:
        lv = declared_level(text)
        if lv is not None:
            return f"1.{min(max(lv, 5), 7)}"
    return "1.7"


def _apply_javac7(doc: PomDoc, ctx: PomContext, params: dict) -> None:
    """Maven e Surefire seguem no JDK da tentativa; só a compilação roda no javac 7 (fork).
    `executable` é preenchido por run_jaguar2 com o javac do JDK 7 encontrado."""
    exe = params.get("executable")
    if not exe:
        raise ValueError("JDK 7 não localizado (informe em Configurações ou baixe na tela Ambiente)")
    p = doc.ensure_plugin(*COMPILER, params.get("version") or ctx.versions["compiler"])
    cfg = doc.child(p, "configuration", create=True)
    level = params.get("level") or _legacy_level(ctx)
    doc.set_child(cfg, "source", level)
    doc.set_child(cfg, "target", level)
    doc.set_child(cfg, "encoding", "UTF-8")
    doc.set_child(cfg, "fork", "true")
    doc.set_child(cfg, "executable", str(exe))


# Jars que embutem uma cópia do ASM 1.x-3.x (`org.objectweb.asm.ClassVisitor` como interface) e, no classpath
# de teste, sobrepõem o ASM moderno do JaCoCo (`ClassProbesVisitor has interface ... ClassVisitor as super class`).
# Só entram os que têm substituto de mesma API (cglib-nodep tem o ASM sob outro pacote).
ASM_ANTIGO_PADRAO = ["cglib:cglib-full"]
CGLIB_NODEP = ("cglib", "cglib-nodep", "2.2.2")


def _apply_asm_shadow(doc: PomDoc, ctx: PomContext, params: dict) -> None:
    excludes = list(params.get("excludes") or ASM_ANTIGO_PADRAO)
    for coord in excludes:
        _split(coord, False)            # valida "grupo:artefato"
    sf = doc.ensure_plugin(*SUREFIRE, ctx.versions["surefire"])
    cfg = doc.child(sf, "configuration", create=True)
    block = doc.child(cfg, "classpathDependencyExcludes", create=True)
    have = {(e.text or "").strip() for e in block}
    for coord in excludes:
        if coord not in have:
            ET.SubElement(block, doc.q("classpathDependencyExclude")).text = coord
    if "cglib:cglib-full" in excludes:      # o que usava o cglib-full passa a usar o cglib-nodep (mesmo pacote)
        doc.ensure_dependency(CGLIB_NODEP[0], CGLIB_NODEP[1], params.get("cglib_version") or CGLIB_NODEP[2], "test")


# Correções que só mexem na linha de comando (flags `-D` do reactor e do jaguar2): o POM gerado não muda.
# Os flags vêm daqui, a não ser que a sugestão traga `reactor_flags` próprios.
FIX_FLAGS: dict[str, list[str]] = {
    # RAT 0.10 não tem `rat.skip`: ignora o erro e libera qualquer número de arquivos sem licença
    "rat-flags": ["-Drat.ignoreErrors=true", "-Drat.numUnapprovedLicenses=999999"],
    # exec-maven-plugin 1.2.1 (`skip` = ${skip}) rodando .sh no Windows; só vale quando o gerado já está no repositório
    "exec-skip": ["-Dskip=true"],
    # maven-sortpom-plugin:verify reprova o jaguar2-pom.xml (não ordenado); `warn` só avisa
    "sortpom-warn": ["-Dsort.verifyFail=warn"],
}
SCR = ("org.apache.felix", "maven-scr-plugin")
SCR_EXECUCAO = "generate-scr-scrdescriptor"


def _apply_nada(doc: PomDoc, ctx: PomContext, params: dict) -> None:
    """Correção só de linha de comando (ver FIX_FLAGS): o POM fica como está."""


SORTPOM = ("com.google.code.sortpom", "maven-sortpom-plugin")


def _apply_sortpom_warn(doc: PomDoc, ctx: PomContext, params: dict) -> None:
    """O POM do Accumulo fixa `<verifyFail>Stop</verifyFail>`, que vence a propriedade `sort.verifyFail`: no
    POM gerado a configuração vira `warn`. Sem o plugin na cadeia de POMs não há o que mudar."""
    version = None
    found = False
    for d in _chain_docs(ctx):
        for p in _plugin_entries(d, *SORTPOM):
            found = True
            if not version and d.child(p, "version") is not None:
                version = (d.child(p, "version").text or "").strip()
    if not found:
        return
    p = doc.ensure_plugin(*SORTPOM, version, overwrite=False)
    doc.set_child(doc.child(p, "configuration", create=True), "verifyFail", "warn")


def _apply_scr_skip(doc: PomDoc, ctx: PomContext, params: dict) -> None:
    """O maven-scr-plugin 1.9.0 não roda em JDK >= 8; o reactor o executa no JDK 7 (scr-jdk7) e o POM gerado
    desliga as execuções dele (fase `none`), então os descritores OSGi-INF só existem nos módulos do reactor."""
    ids: list[str] = []
    version = params.get("version")
    for d in _chain_docs(ctx):
        for p in _plugin_entries(d, *SCR):
            if not version and d.child(p, "version") is not None:
                version = (d.child(p, "version").text or "").strip()
            for e in p.findall(f"{d.q('executions')}/{d.q('execution')}"):
                eid = (d.child(e, "id").text or "").strip() if d.child(e, "id") is not None else "default"
                if eid not in ids:
                    ids.append(eid)
    p = doc.ensure_plugin(*SCR, version or "1.9.0", overwrite=False)
    execs = doc.child(p, "executions", create=True)
    have = {(doc.child(e, "id").text or "").strip(): e for e in execs.findall(doc.q("execution"))
            if doc.child(e, "id") is not None}
    for eid in ids or [SCR_EXECUCAO]:
        e = have.get(eid)
        if e is None:
            e = ET.SubElement(execs, doc.q("execution"))
            if eid != "default":
                doc.set_child(e, "id", eid)
        doc.set_child(e, "phase", "none")


FIXES: dict[str, Fix] = {f.id: f for f in [
    Fix("skip-animal-sniffer", "Ignorar o animal-sniffer",
        "O animal-sniffer verifica a API do Java antiga e quebra a compilação em JDKs novos.",
        _detect_sniffer, _apply_sniffer, {}),
    Fix("compiler-java", "Compilar com bytecode antigo",
        "O projeto declara Java antigo (ou nada); o maven-compiler-plugin é fixado no JDK escolhido.",
        _detect_compiler, _apply_compiler, {}),
    Fix("source-dirs", "Ajustar pastas de código-fonte",
        "Os fontes estão fora de src/main/java ou src/test/java; o POM gerado aponta para elas "
        "sem mover arquivos.",
        _detect_source_dirs, _apply_source_dirs, {}),
    Fix("junit4", "Adicionar JUnit 4",
        "JUnit 3 detectado: o Surefire usaria o provedor JUnit 3, que ignora o listener do Jaguar 2 "
        "(ou não há junit:junit na cadeia de POMs; o Jaguar 2 só roda testes JUnit 4).",
        _detect_junit4, _apply_junit4, {}),
    Fix("override-version", "Forçar versões",
        "Redeclara versões de dependências e plugins no POM gerado.",
        _never, _apply_override, {"deps": {}, "plugins": {}}),
    Fix("exclude-duplicates", "Excluir classes duplicadas",
        "Exclui da compilação arquivos que duplicam classes.",
        _never, _apply_excludes, {"files": []}),
    Fix("skip-rat", "Ignorar o Apache RAT",
        "A verificação de licenças (apache-rat-plugin) reprova arquivos do checkout antigo.",
        _detect_rat, _apply_rat, {}),
    Fix("javac7", "Compilar com o javac do JDK 7",
        "O código antigo não compila no javac 8/11 (inferência de genéricos mais estrita, `-source 5` removido); "
        "o Maven e o Surefire seguem no JDK da tentativa e só o javac roda no JDK 7 (fork).",
        _never, _apply_javac7, {}),
    Fix("asm-shadow", "Tirar do classpath de teste o ASM antigo",
        "Um jar de teste (cglib-full) embute o ASM 1.x e sobrepõe o ASM do JaCoCo, que então não consegue analisar "
        "as classes; o jar é excluído do classpath de teste e o cglib-nodep (mesma API) assume.",
        _never, _apply_asm_shadow, {"excludes": ASM_ANTIGO_PADRAO}),
    Fix("rat-flags", "Ignorar o Apache RAT 0.10 no reactor",
        "A versão 0.10 do apache-rat-plugin não tem `rat.skip`; `-Drat.ignoreErrors` e "
        "`-Drat.numUnapprovedLicenses` entram na linha de comando do reactor e do Jaguar 2.",
        _never, _apply_nada, {}),
    Fix("exec-skip", "Pular o exec-maven-plugin",
        "O exec-maven-plugin 1.2.1 tenta rodar um script .sh no Windows; `-Dskip=true` o pula "
        "(vale quando o código gerado por ele já está no repositório).",
        _never, _apply_nada, {}),
    Fix("sortpom-warn", "Só avisar no sortpom",
        "O maven-sortpom-plugin:verify reprova o jaguar2-pom.xml gerado; `-Dsort.verifyFail=warn` (e `verifyFail` "
        "no POM gerado, quando o POM fixa `Stop`) só avisa.",
        _never, _apply_sortpom_warn, {}),
    Fix("scr-jdk7", "Rodar o maven-scr-plugin no JDK 7",
        "O maven-scr-plugin 1.9.0 falha em JDK >= 8 (`not an ImageIO SPI class`): o reactor roda no JDK 7 com "
        "Maven 3.6.3 e o POM gerado desliga o plugin.",
        _never, _apply_scr_skip, {}),
]}
