"""Leitura das saídas brutas: surefire do Maven, Jaguar 1 (XML e log) e Jaguar 2 (CSV e XML).

Tudo vira `LineScore` (Jaguar 2 e 1) ou um mapa {classe#teste: status}. Nada aqui calcula fórmulas."""
from __future__ import annotations

import gzip
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


def test_counts(results: dict[str, str]) -> dict[str, int]:
    c = Counter(results.values())
    out = {k: c.get(k, 0) for k in ("passed", "failed", "error", "skipped")}
    out["total"] = len(results)
    return out
