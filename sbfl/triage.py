"""`sbfl triage`: agrupa as falhas de uma campanha por causa provável e sugere o que ajustar."""
from __future__ import annotations

import gzip
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

# (id, regex, dica). A primeira regra que casar vence; são heurísticas, não diagnóstico.
RULES = [
    ("maven_http_blocked", r"Blocked mirror for repositories|maven-default-http-blocker",
     "Maven >= 3.8.1 bloqueia repositórios http://. Use Maven 3.6/3.8.0 ou um settings.xml com mirror."),
    ("plugin_unresolvable", r"Plugin \S+ or one of its dependencies could not be resolved",
     "Plugin Maven indisponível (repositório antigo/offline). Veja a rede e o settings.xml."),
    ("dependency_unresolvable", r"Could not resolve dependencies|Could not find artifact|Failed to collect dependencies",
     "Dependência ou repositório indisponível (comum em versões antigas). Raramente recuperável."),
    ("unsupported_classfile",
     r"Unsupported class file major version|Unsupported class version|Error while analyzing class|ClassReader",
     "O JaCoCo do Jaguar não lê esse bytecode. Compile e rode com JDK 8 (java_home ou [java.homes])."),
    ("jdk_mismatch",
     r"invalid (?:target|source) release|release version \d+ not supported|(?:Source|Target) option \d+ is no longer supported|invalid flag: -release",
     "JDK incompatível com o projeto. Ajuste java_home por bug/benchmark ou configure [java.homes]."),
    ("compilation_error", r"COMPILATION ERROR|cannot find symbol|package \S+ does not exist",
     "Erro de compilação (JDK diferente do original ou dependência faltando)."),
    ("out_of_memory", r"OutOfMemoryError|Java heap space|GC overhead",
     "Falta de memória. Aumente MAVEN_OPTS (-Xmx) ou reduza o escopo de testes."),
    ("classpath_problem", r"NoClassDefFoundError|NoSuchMethodError|ClassNotFoundException",
     "Problema de classpath (versão errada ou dependência ausente em target/dependency)."),
    ("port_in_use", r"Address already in use|BindException",
     "Porta 6300 do agente JaCoCo ocupada. Rode em sequência e encerre processos Java órfãos."),
    ("git_error", r"fatal: |not a git repository|pathspec",
     "Erro de Git (clone incompleto, ref inexistente ou caminho longo no Windows)."),
]

STATUS_HINTS = {
    "jaguar_timeout": "Aumente [jaguar] timeout_s ou reduza o escopo de testes.",
    "no_failing_tests": "Nenhum teste falhou: o bug pode não reproduzir com os testes selecionados "
                        "(test_scope) ou o teste que falha foi pulado (JUnit 5/abstrata).",
    "fault_not_covered": "A linha defeituosa não foi coberta: fora do código executado, em classe de "
                         "teste excluída ou ground truth impreciso.",
    "junit5_only": "Só há testes JUnit 5; o runner do Jaguar é JUnit 4. Fora do alcance sem alterar o Jaguar.",
    "no_tests": "Nenhuma classe de teste encontrada (nome fora do padrão ou build não gerou test-classes).",
    "no_truth": "O diff buggy→fixed não tem linhas em código de produção.",
    "empty_report": "O Jaguar gerou um relatório vazio (nada foi instrumentado).",
    "jaguar_crash": "A JVM caiu e o laço de recuperação esgotou as tentativas. Veja `inspect`.",
}


def classify(text: str) -> tuple[str | None, str | None]:
    for rid, rx, hint in RULES:
        if re.search(rx, text):
            return rid, hint
    return None, None


def signature(text: str) -> str:
    for line in text.splitlines():
        if re.search(r"ERROR|Exception|error:", line):
            line = re.sub(r"[A-Za-z]:\\\S+|/\S+", "<path>", line)
            return re.sub(r"\d+", "N", line).strip()[:120]
    return "(sem mensagem de erro identificável)"


def _log_tail(d: Path, max_chars: int = 400_000) -> str:
    p = d / "jaguar.log.gz"
    if not p.exists():
        return ""
    try:
        with gzip.open(p, "rt", encoding="utf-8", errors="replace") as f:
            return f.read()[-max_chars:]
    except OSError:
        return ""


def build_report(results: Path, benchmark: str | None = None, project: str | None = None) -> dict:
    groups: dict = defaultdict(list)
    flagged: dict = defaultdict(list)
    total = ok = 0
    for mp in sorted(Path(results).glob("*/*/*/meta.json")):
        b, p, bug = mp.parts[-4], mp.parts[-3], mp.parts[-2]
        if (benchmark and benchmark != b) or (project and project != p):
            continue
        meta = json.loads(mp.read_text(encoding="utf-8"))
        total += 1
        st = meta.get("status", "error")
        if st == "ok":
            ok += 1
            for fl in meta.get("quality") or []:
                flagged[fl].append(f"{b}/{p}/{bug}")
            continue
        text = "\n".join([str(meta.get("detail", "")), str(meta.get("tail", ""))])
        rid, hint = classify(text)
        if rid is None and st in ("build_failed", "jaguar_crash", "d4j_failed", "error", "git_failed"):
            log = _log_tail(mp.parent)
            rid, hint = classify(log)
            text = text + "\n" + log
        rule = rid or "unclassified"
        hint = hint or STATUS_HINTS.get(st) or "Sem sugestão automática; use `inspect`."
        key = (b, st, rule, signature(text) if rule == "unclassified" else "")
        groups[key].append((f"{b}/{p}/{bug}", hint))
    clusters = [{"benchmark": k[0], "status": k[1], "rule": k[2], "signature": k[3],
                 "count": len(v), "example": v[0][0], "hint": v[0][1], "bugs": [x[0] for x in v]}
                for k, v in groups.items()]
    clusters.sort(key=lambda c: -c["count"])
    return {"total": total, "ok": ok, "clusters": clusters,
            "flagged": {k: v for k, v in sorted(flagged.items(), key=lambda kv: -len(kv[1]))}}


def render_markdown(rep: dict) -> str:
    out = [f"{rep['total']} bug(s) analisados; {rep['ok']} com status ok; "
           f"{sum(c['count'] for c in rep['clusters'])} com falha.\n"]
    if rep["clusters"]:
        out.append("| # | Bugs | Status | Causa provável | Exemplo | O que fazer |\n|---:|---:|---|---|---|---|")
        for i, c in enumerate(rep["clusters"], 1):
            cause = c["rule"] if c["rule"] != "unclassified" else f"não classificada: `{c['signature']}`"
            out.append(f"| {i} | {c['count']} | `{c['status']}` | {cause} | {c['example']} | {c['hint']} |")
        out.append("\nDepois de ajustar o ambiente, refaça só o que falhou, por exemplo:\n\n"
                   "```bash\npython -m sbfl run --benchmark <benchmark> --retry build_failed,jaguar_crash\n```")
    if rep["flagged"]:
        out.append("\nBugs com status `ok` mas **dados duvidosos** (alertas de qualidade):\n")
        hints = {"many_failures": "mais da metade dos testes falha (ambiente suspeito)",
                 "instrumentation_errors": "erros de instrumentação: espectro possivelmente incompleto",
                 "tests_excluded": "mais de 10% das classes de teste foram excluídas",
                 "truth_disagrees": "o ground truth diverge do patch do benchmark"}
        for k, bugs in rep["flagged"].items():
            out.append(f"- `{k}` ({len(bugs)}): {hints.get(k, '')}. Ex.: {bugs[0]}")
    return "\n".join(out) + "\n"
