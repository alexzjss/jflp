"""Leitura das saídas brutas: surefire do Maven, Jaguar 1 (XML e log) e Jaguar 2 (CSV e XML).

Tudo vira `LineScore` (Jaguar 2 e 1) ou um mapa {classe#teste: status}. Nada aqui calcula fórmulas."""
from __future__ import annotations

import gzip
import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .jaguar import TEST_RE

MARK = "# sbfl attempt"


@dataclass
class Req:
    cls: str
    line: int
    sus: float
    ef: int
    ep: int
    nf: int
    np: int


@dataclass
class LineScore:
    cls: str            # FQCN com ".", classes internas com "$"
    line: int
    cef: int
    cep: int
    cnf: int | None     # None quando a saída não traz esse contador (Jaguar 2 XML)
    cnp: int | None
    susp: float


def outer(cls: str) -> str:
    """Classe externa de uma classe interna (a.B$Inner -> a.B)."""
    return cls.split("$", 1)[0]


def parse_xml_jaguar1(path) -> list[Req]:
    reqs = []
    for _, el in ET.iterparse(path, events=("end",)):
        if el.tag == "requirements":
            a = el.attrib
            reqs.append(Req(a.get("name", ""), int(a.get("location", 0)),
                            float(a.get("suspicious-value", 0.0)),
                            int(a.get("cef", 0)), int(a.get("cep", 0)),
                            int(a.get("cnf", 0)), int(a.get("cnp", 0))))
            el.clear()
    return reqs


def jaguar1_scores(path: Path) -> list[LineScore]:
    return [LineScore(r.cls, r.line, r.ef, r.ep, r.nf, r.np, r.sus) for r in parse_xml_jaguar1(path)]


def parse_jaguar2_csv(path: Path) -> list[LineScore]:
    """CSV do Jaguar 2 sem cabeçalho: classe,linha,cef,cnf,cep,cnp,susp (classe com "/")."""
    out = []
    with open(path, encoding="utf-8") as f:
        for n, raw in enumerate(f, 1):
            s = raw.strip()
            if not s:
                continue
            parts = s.split(",")
            if len(parts) != 7:
                raise ValueError(f"linha {n} inválida no CSV do Jaguar 2: esperava 7 campos, veio {len(parts)}")
            try:
                cls, line, cef, cnf, cep, cnp, susp = parts[0], int(parts[1]), int(parts[2]), \
                    int(parts[3]), int(parts[4]), int(parts[5]), float(parts[6])
            except ValueError as e:
                raise ValueError(f"linha {n} inválida no CSV do Jaguar 2: {e}") from None
            out.append(LineScore(cls.replace("/", "."), line, cef, cep, cnf, cnp, susp))
    return out


def parse_jaguar2_xml(path: Path) -> tuple[dict, list[LineScore]]:
    """XML do Jaguar 2: totais de testes em <tests fail pass> e linhas em package/sourcefile/line."""
    root = ET.parse(path).getroot()
    t = root.find("tests")
    tests = {"fail": int(t.get("fail", 0)) if t is not None else 0,
             "pass": int(t.get("pass", 0)) if t is not None else 0}
    scores = []
    for pkg in root.iter("package"):
        pkg_name = pkg.get("name", "").replace("/", ".")
        for sf in pkg.iter("sourcefile"):
            base = sf.get("name", "")
            base = base[:-5] if base.endswith(".java") else base
            cls = ".".join(p for p in (pkg_name, base) if p)
            for ln in sf.iter("line"):
                scores.append(LineScore(cls, int(ln.get("nr", 0)), int(ln.get("cef", 0)),
                                        int(ln.get("cep", 0)), None, None,
                                        float(ln.get("susp", 0.0))))
    return tests, scores


def parse_surefire(reports: Path) -> dict[str, str]:
    """{classe#teste: passed|failed|error|skipped} a partir de target/surefire-reports/TEST-*.xml."""
    out = {}
    for f in sorted(Path(reports).glob("TEST-*.xml")):
        try:
            root = ET.parse(f).getroot()
        except ET.ParseError:
            continue
        for tc in root.iter("testcase"):
            st = "passed"
            if tc.find("failure") is not None:
                st = "failed"
            elif tc.find("error") is not None:
                st = "error"
            elif tc.find("skipped") is not None:
                st = "skipped"
            out[f"{tc.get('classname', '')}#{tc.get('name', '')}"] = st
    return out


def parse_jaguar1_log(path: Path) -> dict[str, str]:
    """{classe#teste: passed|failed} dos testes da ÚLTIMA tentativa do Jaguar."""
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    last = max((i for i, l in enumerate(lines) if l.startswith(MARK)), default=-1)
    out = {}
    for line in lines[last + 1:]:
        m = TEST_RE.search(line)
        if m:
            out[f"{m.group(2)}#{m.group(1)}"] = "passed" if m.group(3) == "Passed" else "failed"
    return out


AVISO_LOG_INTERROMPIDO = ("o log do Jaguar foi interrompido (algum teste redirecionou a saída); "
                          "a contagem veio do XML")
SEM_NOME = "(falha sem nome {n})"


def jaguar1_counts(path) -> dict[str, int] | None:
    """Totais de testes pelo XML do Jaguar 1: em toda linha, cef+cnf = testes que falharam e
    cep+cnp = testes que passaram. Toma o máximo entre as linhas (None se o XML não tem linhas)."""
    failed = passed = 0
    seen = False
    for r in parse_xml_jaguar1(path):
        seen = True
        failed = max(failed, r.ef + r.nf)
        passed = max(passed, r.ep + r.np)
    return {"failed": failed, "passed": passed, "total": failed + passed} if seen else None


def reconcile_jaguar1(log_tests: dict[str, str], counts: dict | None,
                      known_failing: list[str] | None = None) -> tuple[dict[str, str], dict[str, int], bool]:
    """Junta o log (nomes dos testes) com o XML (contagem confiável).

    O log do Jaguar sai pelo stdout, que um teste pode trocar ou fechar; o XML vem do espectro e não perde nada.
    Devolve (testes, contagem, log_interrompido). Falhas que o log não nomeou ganham o nome de um teste
    relevante conhecido (`classe#teste`) ou um nome genérico."""
    tests = dict(log_tests)
    log_counts = test_counts(tests)
    if not counts:
        return tests, log_counts, False
    log_failed = log_counts["failed"] + log_counts["error"]
    failed = max(counts.get("failed", 0), log_failed)
    passed = max(counts.get("passed", 0), log_counts["passed"])
    truncated = len(tests) < counts.get("total", 0)
    missing = failed - log_failed
    if missing > 0:
        names = [t for t in (known_failing or []) if "#" in t and t not in tests]
        for n in range(missing):
            tests[names[n] if n < len(names) else SEM_NOME.format(n=n + 1 - len(names))] = "failed"
    return tests, {"total": failed + passed, "passed": passed, "failed": failed,
                   "error": 0, "skipped": 0}, truncated


def test_counts(results: dict[str, str]) -> dict[str, int]:
    c = Counter(results.values())
    out = {k: c.get(k, 0) for k in ("passed", "failed", "error", "skipped")}
    out["total"] = len(results)
    return out


# ------------------------------------------------------------ provável erro dos testes que falharam

_AT = re.compile(r"^\s*at\s+(?:[\w.$-]+/)*([\w.$<>]+)\.([\w$<>]+)\(([^():]*?)(?::(\d+))?\)\s*$")
_NOT_PROJECT = ("java.", "javax.", "jdk.", "sun.", "com.sun.", "junit.", "org.junit.", "org.opentest4j.",
                "org.hamcrest.", "org.testng.", "org.apache.maven.surefire.", "org.apache.maven.plugin.surefire.")
MAX_FAILURES = 50
MAX_MESSAGE = 500


def _is_project(cls: str) -> bool:
    return not cls.startswith(_NOT_PROJECT)


def _is_test_class(cls: str, test_cls: str) -> bool:
    base = outer(cls).rsplit(".", 1)[-1]
    return outer(cls) == outer(test_cls) or base.endswith(("Test", "Tests", "TestCase", "IT")) \
        or base.startswith("Test")


def first_project_frame(stack: str, test_cls: str = "") -> dict | None:
    """Primeiro quadro da pilha que é do projeto (não java.*, junit, sun.*, surefire...).

    Prefere o código de produção; se só houver quadros do próprio teste, devolve o primeiro deles."""
    frames = []
    for ln in stack.splitlines():
        m = _AT.match(ln)
        if m and _is_project(m.group(1)):
            frames.append({"cls": m.group(1), "method": m.group(2), "file": m.group(3) or None,
                           "line": int(m.group(4)) if m.group(4) else None})
    for f in frames:
        if not _is_test_class(f["cls"], test_cls):
            return f
    return frames[0] if frames else None


def parse_surefire_failures(reports: Path) -> list[dict]:
    """[{test, message, type, frame:{cls, method, file, line}|None}] dos testes com falha ou erro."""
    out: list[dict] = []
    for f in sorted(Path(reports).glob("TEST-*.xml")):
        try:
            root = ET.parse(f).getroot()
        except ET.ParseError:
            continue
        for tc in root.iter("testcase"):
            el = tc.find("failure")
            if el is None:
                el = tc.find("error")
            if el is None:
                continue
            cname = tc.get("classname", "")
            text = el.text or ""
            msg = el.get("message")
            if msg is None:
                msg = next((ln.strip() for ln in text.splitlines() if ln.strip()
                            and not _AT.match(ln)), "")
            out.append({"test": f"{cname}#{tc.get('name', '')}", "message": (msg or "")[:MAX_MESSAGE] or None,
                        "type": el.get("type"), "frame": first_project_frame(text, cname)})
            if len(out) >= MAX_FAILURES:
                return out
    return out


# ------------------------------------------------------------------- ranking exportável

def ranking_rows(scores: list[LineScore]) -> list[tuple[str, int, float, int]]:
    """(classe, linha, score, posição) por suspeita decrescente.

    Empates: a posição é a MELHOR do grupo (1 + nº de linhas com pontuação maior), igual para todas as
    empatadas. A pior posição do empate não vai ao CSV; ela sai de `compare.fault_positions` (rank_worst)."""
    ordered = sorted(scores, key=lambda s: (-s.susp, s.cls, s.line))
    rows, rank, prev = [], 0, None
    for i, s in enumerate(ordered, 1):
        if s.susp != prev:
            rank, prev = i, s.susp
        rows.append((s.cls, s.line, s.susp, rank))
    return rows


def ranking_csv(scores: list[LineScore]) -> str:
    """CSV do ranking; coluna `rank` = melhor posição do empate (ver `ranking_rows`)."""
    lines = ["class,line,score,rank"]
    lines += [f"{c},{ln},{sc!r},{r}" for c, ln, sc, r in ranking_rows(scores)]
    return "\n".join(lines) + "\n"
