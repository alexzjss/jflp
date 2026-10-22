"""Classifica o resultado de uma tentativa: status, causa em português, evidências e correções sugeridas.

Falha de teste causada pelo bug é DADO (`ok_testes_falhando`), nunca erro e nunca gera sugestão.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import status as st
from .outputs import AVISO_LOG_INTERROMPIDO, jaguar1_scores, parse_jaguar2_csv
from .pomgen.pom import junit_declared, junit_is_old
from .run_maven import AttemptResult

MAX_LOG_BYTES = 400_000
MAX_EVIDENCE = 8
MAX_LINE = 300
SEM_INSTRUMENTACAO = ("nenhuma classe do código foi instrumentada "
                      "(módulo sem código-fonte ou classes fora de target/classes)")
_CACHE_VAZIO = re.compile(r"ClassFilesCache size = 0")


@dataclass
class Suggestion:
    fix: str | None = None
    params: dict = field(default_factory=dict)
    jdk: int | None = None
    maven: str | None = None
    label: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Diagnosis:
    status: str
    cause: str = ""
    evidence: list[str] = field(default_factory=list)
    suggestions: list[Suggestion] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"status": self.status, "cause": self.cause, "evidence": list(self.evidence),
                "suggestions": [s.to_dict() for s in self.suggestions], "warnings": list(self.warnings)}


@dataclass
class Rule:
    status: str
    pattern: str
    cause: str
    suggestions: list[Suggestion] = field(default_factory=list)
    only_errors: bool = False  # só casa em linhas de falha ([ERROR] / Failed to execute goal)


def _surefire(version: str) -> Suggestion:
    return Suggestion(fix="override-version", label=f"Forçar o Surefire {version}",
                      params={"plugins": {"org.apache.maven.plugins:maven-surefire-plugin": version}})


def _compiler(version: str) -> Suggestion:
    return Suggestion(fix="compiler-java", params={"version": version},
                      label=f"Usar o maven-compiler-plugin {version}")


_SKIP_JACOCO = Suggestion(fix="skip-jacoco", label="Desligar o JaCoCo do projeto (-Djacoco.skip=true)")
_JAVAAGENT = re.compile(r"processing of -javaagent failed|FATAL ERROR in native method|"
                        r"Error opening zip file or JAR manifest missing", re.I)
_UNBLOCK_HTTP = Suggestion(fix="unblock-http", label="Liberar repositórios http:// (settings do JFLP)")
_MAVEN_363 = Suggestion(maven="3.6.3", label="Usar Maven 3.6.3 (aceita repositórios http)")

# A primeira regra que casar vence (padrões casados sem diferenciar maiúsculas).
RULES: list[Rule] = [
    Rule(st.PROJETO_INVALIDO,
         r"there is no POM in this directory|MissingProjectException|"
         r"Could not find the selected project in the reactor",
         "O Maven não achou um pom.xml onde foi executado (ou o módulo informado não existe no reactor): "
         "escolha a pasta que contém o pom.xml ou informe o módulo", only_errors=True),
    Rule(st.PROJETO_INVALIDO,
         r"Non-resolvable parent POM.*(?:relativePath|points at wrong local POM)|"
         r"(?:relativePath|points at wrong local POM).*Non-resolvable parent POM",
         "O pom.xml herda de um pom pai que não está na pasta escolhida (nem em repositório remoto): "
         "escolha a pasta raiz do projeto, que contém o pom pai, e informe esta subpasta como módulo",
         only_errors=True),
    Rule(st.JDK_INCOMPATIVEL,
         r"Failed to extract descriptors|Failed to execute goal.*Index \d+ out of bounds for length \d+|"
         r"Unsupported class file major version|"
         r"IllegalArgumentException.*(?:org\.objectweb\.asm|ClassReader)|"
         r"(?:org\.objectweb\.asm|ClassReader).*IllegalArgumentException",
         "plugin antigo não lê classes do JDK atual (ASM antigo)",
         [Suggestion(jdk=8, label="Tentar com JDK 8"),
          Suggestion(fix="compiler-java", params={"target": 8},
                     label="Compilar com bytecode Java 8 (compiler-java)")], only_errors=True),
    Rule(st.JDK_INCOMPATIVEL,
         r"Unsupported class file major version|Unsupported class version|invalid target release|"
         r"invalid source release|release version \d+ not supported|UnsupportedClassVersionError|"
         r"(?:Source|Target) option \d+ is no longer supported|invalid flag: -release",
         "JDK incompatível com o projeto ou com a instrumentação do JaCoCo",
         [Suggestion(jdk=8, label="Tentar com JDK 8"), Suggestion(jdk=17, label="Tentar com JDK 17"),
          _compiler("3.11.0")]),
    Rule(st.PLUGIN_INCOMPATIVEL, r"animal-sniffer|Undefined reference",
         "O plugin animal-sniffer bloqueou o build",
         [Suggestion(fix="skip-animal-sniffer", label="Pular o animal-sniffer")], only_errors=True),
    Rule(st.PLUGIN_INCOMPATIVEL, r"apache-rat-plugin.*unapproved license",
         "verificação de licenças (Apache RAT) reprovou arquivos",
         [Suggestion(fix="skip-rat", label="Pular o Apache RAT")], only_errors=True),
    Rule(st.DEPENDENCIA_NAO_RESOLVIDA, r"maven-remote-resources-plugin",
         "recurso remoto indisponível (maven-remote-resources-plugin); "
         "inclua -Dremoteresources.skip=true em run.skip_flags", only_errors=True),
    Rule(st.PLUGIN_INCOMPATIVEL,
         r"maven-surefire-plugin:2\.|SurefireBooterForkException|The forked VM terminated",
         "Surefire incompatível ou JVM de teste caiu", [_surefire("3.2.5"), _surefire("3.0.0-M5")],
         only_errors=True),
    Rule(st.PLUGIN_INCOMPATIVEL, r"maven-compiler-plugin.*(Fatal error compiling|NoSuchMethodError)",
         "maven-compiler-plugin incompatível com o JDK", [_compiler("3.1"), _compiler("3.11.0")],
         only_errors=True),
    Rule(st.DEPENDENCIA_NAO_RESOLVIDA, r"Blocked mirror for repositories|maven-default-http-blocker",
         "Maven recente bloqueia repositórios http://", [_UNBLOCK_HTTP, _MAVEN_363]),
    Rule(st.DEPENDENCIA_NAO_RESOLVIDA,
         r"Could not resolve dependencies|Could not transfer artifact|Could not find artifact|"
         r"Failed to collect dependencies|Plugin \S+ or one of its dependencies could not be resolved",
         "Dependência ou plugin não pôde ser baixado", [_MAVEN_363]),
    Rule(st.CLASSES_DUPLICADAS, r"duplicate class", "Classes duplicadas entre diretórios de fontes",
         [Suggestion(fix="exclude-duplicates", label="Excluir os arquivos duplicados")]),
    Rule(st.COMPILACAO_FALHOU, r"COMPILATION ERROR|cannot find symbol|package \S+ does not exist",
         "Erro de compilação",
         [Suggestion(fix="source-dirs", label="Ajustar diretórios de fontes"),
          Suggestion(fix="junit4", label="Adicionar JUnit 4")]),
    Rule(st.TESTES_NAO_ENCONTRADOS, r"No tests (?:were )?(?:executed|to run)|Tests run: 0\b",
         "Nenhum teste foi executado",
         [Suggestion(fix="source-dirs", label="Ajustar diretórios de fontes"),
          Suggestion(fix="junit4", label="Adicionar JUnit 4"),
          Suggestion(params={"test_scope": "all"}, label="Rodar todos os testes (escopo all)")]),
]

_DUP_CLASS = re.compile(r"duplicate class:?\s+([\w.$]+)", re.I)
_DUP_FILE = re.compile(r"(\S+\.java):\[\d+,\d+\]\s+duplicate class", re.I)


def _pom_has_old_junit(res: AttemptResult) -> bool:
    pom = res.outputs.get("pom")
    try:
        return junit_is_old(junit_declared([Path(pom).read_text(encoding="utf-8", errors="replace")])[0])
    except (OSError, TypeError):
        return False


def _read_tail(log: Path | None) -> str:
    try:
        with open(log, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - MAX_LOG_BYTES))
            return f.read().decode("utf-8", errors="replace")
    except (OSError, TypeError):
        return ""


def _evidence(text: str, pos: int) -> list[str]:
    idx = text.count("\n", 0, pos)
    start = max(0, idx - 3)
    return [ln[:MAX_LINE] for ln in text.splitlines()[start:start + MAX_EVIDENCE]]


_ROOTS = ("src/main/java/", "src/test/java/", "src/java/", "src/")


def _relative_source(path: str) -> str:
    path = path.replace("\\", "/")
    for marker in _ROOTS:
        k = path.find(marker)
        if k >= 0:
            return path[k + len(marker):]
    return path


def _duplicate_files(text: str) -> list[str]:
    files = [_relative_source(f) for f in _DUP_FILE.findall(text)]
    files += [c.split("$", 1)[0].replace(".", "/") + ".java" for c in _DUP_CLASS.findall(text)]
    return list(dict.fromkeys(files))


_ERR_LINE = re.compile(r"\[ERROR\]|Failed to execute goal|BUILD FAILURE")
_FAIL_LINE = re.compile(r"\[ERROR\]|Failed to execute goal")


def _error_lines(text: str) -> list[tuple[int, str]]:
    """(offset, linha) das linhas de falha."""
    out, off = [], 0
    for ln in text.splitlines(keepends=True):
        if _FAIL_LINE.search(ln):
            out.append((off, ln))
        off += len(ln)
    return out


def _match_rules(text: str) -> tuple[Diagnosis, bool] | None:
    """Primeira causa encontrada e se ela veio de uma linha de falha.\n\n    Com linhas [ERROR]/BUILD FAILURE, vence a regra cujo match está mais perto do primeiro bloco de erro;\n    sem elas, vale a ordem das regras. Regras `only_errors` nunca casam em cabeçalhos de plugin."""
    errs = _error_lines(text)
    first = _ERR_LINE.search(text)
    cands: list[tuple[int, int, Rule, bool, int]] = []
    for n, r in enumerate(RULES):
        pos, from_err = None, False
        for off, ln in errs:
            m = re.search(r.pattern, ln, re.I)
            if m:
                pos, from_err = off + m.start(), True
                break
        if pos is None and not r.only_errors:
            m = re.search(r.pattern, text[first.start():] if first else text, re.I)
            if m:
                pos = m.start() + (first.start() if first else 0)
        if pos is not None:
            cands.append((pos if first else 0, n, r, from_err, pos))
    if not cands:
        return None
    _, _, r, from_err, pos = min(cands, key=lambda c: (c[0], c[1]))
    sugs = [Suggestion(s.fix, dict(s.params), s.jdk, s.maven, s.label) for s in r.suggestions]
    if r.status == st.CLASSES_DUPLICADAS:
        files = _duplicate_files(text)
        for s in sugs:
            s.params = {"files": files}
    return Diagnosis(r.status, r.cause, _evidence(text, pos), sugs), from_err


def _success(kind: str, res: AttemptResult) -> bool:
    if kind == "maven":
        sf = res.outputs.get("surefire")
        return bool(res.tests) or (bool(sf) and Path(sf).is_dir() and any(Path(sf).iterdir()))
    key = {"jaguar2": "jaguar2_csv", "jaguar1": "jaguar1_xml"}.get(kind)
    p = res.outputs.get(key) if key else None
    return bool(p) and Path(p).exists()


def _keep(kind: str, fix: str | None, maven: str | None) -> bool:
    """Correções do POM só valem para o Jaguar 2 (unblock-http vale para todos; skip-jacoco só para o Jaguar 1:
    o Maven puro já roda com -Djacoco.skip e o Jaguar 2 precisa do próprio JaCoCo); o Jaguar 2 exige Maven >= 3.9.9."""
    if fix == "skip-jacoco":
        return kind == "jaguar1"
    if fix and kind != "jaguar2" and fix != "unblock-http":
        return False
    return not (maven and kind == "jaguar2")


def applicable(kind: str, sugs: list[dict]) -> list[dict]:
    """Filtra sugestões já gravadas (dicts) pelo tipo da execução; usada na tela e no "tentar de novo"."""
    return [s for s in sugs if _keep(kind, s.get("fix"), s.get("maven"))]


def classify(kind: str, res: AttemptResult) -> Diagnosis:
    d = _classify(kind, res)
    d.suggestions = [s for s in d.suggestions if _keep(kind, s.fix, s.maven)]
    return d


def _junit4_suggestion() -> Suggestion:
    return Suggestion(fix="junit4", label="Forçar JUnit 4 (o Surefire pode estar usando o provedor JUnit 3)")


def _classify(kind: str, res: AttemptResult) -> Diagnosis:
    if res.timed_out:
        return Diagnosis(st.TIMEOUT, "A tentativa excedeu o tempo limite")
    extra = res.extra or {}
    fase, erro = extra.get("fase"), extra.get("erro") or ""
    if kind == "jaguar1" and extra.get("meta_status") == "jaguar_timeout":
        return Diagnosis(st.TIMEOUT, "O Jaguar 1 excedeu o tempo limite")
    if kind == "jaguar1" and extra.get("meta_status") == "no_tests":
        return Diagnosis(st.TESTES_NAO_ENCONTRADOS, "O Jaguar 1 não encontrou classes de teste")
    if fase == "jaguar1_lib":
        return Diagnosis(st.JAGUAR_SEM_SAIDA, erro)

    if _success(kind, res):
        failed = any(v in ("failed", "error") for v in res.tests.values())
        warnings: list[str] = []
        if kind == "jaguar1":     # o XML manda: o log do Jaguar pode ter sido cortado por um teste
            tc = (res.extra or {}).get("test_counts") or {}
            failed = failed or (tc.get("failed") or 0) > 0 or (tc.get("error") or 0) > 0
            if (res.extra or {}).get("log_truncado"):
                warnings.append(AVISO_LOG_INTERROMPIDO)
        scores = None
        if kind == "jaguar2":
            try:
                scores = parse_jaguar2_csv(Path(res.outputs["jaguar2_csv"]))
            except (ValueError, OSError) as e:
                return Diagnosis(st.JAGUAR_SEM_SAIDA, "relatório do Jaguar 2 ilegível", [str(e)[:MAX_LINE]])
            if not scores and failed:   # §8.5.5: sucesso exige relatório não vazio quando há falhas
                return Diagnosis(st.JAGUAR_SEM_SAIDA,
                                 "testes falharam mas o relatório do Jaguar 2 está vazio "
                                 "(cobertura/classesDirs não ligados)", suggestions=[_junit4_suggestion()],
                                 warnings=[SEM_INSTRUMENTACAO])
        elif kind == "jaguar1":
            try:
                scores = jaguar1_scores(Path(res.outputs["jaguar1_xml"]))
            except (ValueError, OSError, KeyError, ET.ParseError):
                scores = None
        if (scores is not None and not scores) or (kind == "jaguar1" and _CACHE_VAZIO.search(_read_tail(res.log))):
            warnings.append(SEM_INSTRUMENTACAO)
        if not failed and (res.tests or (res.extra or {}).get("test_counts", {}).get("total") or (kind != "maven" and scores is not None and not scores)):
            # rodou, mas nenhum teste falhou: não há o que localizar, e isso não pode parecer sucesso
            return Diagnosis(st.SEM_TESTE_FALHANDO, st.CAUSA_SEM_TESTE_FALHANDO, warnings=warnings)
        return Diagnosis(st.OK_TEST_FAIL if failed else st.OK, warnings=warnings)

    text = _read_tail(res.log)
    if kind == "jaguar1" and text and _JAVAAGENT.search(text) and "jacoco" in text.lower():
        m = _JAVAAGENT.search(text)
        return Diagnosis(st.PLUGIN_INCOMPATIVEL, "o agente do JaCoCo do projeto derrubou a JVM de teste",
                         _evidence(text, m.start()), [_SKIP_JACOCO])
    found = _match_rules(text) if text else None
    d, from_err = found if found else (None, False)
    if fase == "pom":
        return d if d and d.status == st.PLUGIN_INCOMPATIVEL else Diagnosis(st.DESCONHECIDO, erro)
    no_csv = kind == "jaguar2" and fase != "reactor" and res.rc == 0
    if d and not (no_csv and not from_err):
        return d
    if no_csv:
        junit4 = _junit4_suggestion()
        if res.outputs.get("surefire"):      # os testes rodaram, só o listener não foi acionado
            if "surefire-junit3" in text or _pom_has_old_junit(res):
                return Diagnosis(st.JAGUAR_SEM_SAIDA,
                                 "testes rodaram mas o Jaguar 2 não foi acionado (provedor JUnit 3)",
                                 suggestions=[junit4])
            return Diagnosis(st.JAGUAR_SEM_SAIDA, "O build terminou mas o Jaguar 2 não gerou o CSV",
                             suggestions=[junit4])
        return Diagnosis(st.JAGUAR_SEM_SAIDA, "O build terminou mas o Jaguar 2 não gerou o CSV")
    if kind == "jaguar1" and extra.get("meta_status") == st.JAGUAR_CRASH:
        return Diagnosis(st.JAGUAR_CRASH, "A JVM do Jaguar 1 caiu")
    return Diagnosis(st.DESCONHECIDO, erro or "Falha não reconhecida; veja o log")
