"""Leitura do XML do Jaguar, heurísticas SBFL offline e métricas de avaliação."""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from bisect import bisect_left, bisect_right
from dataclasses import dataclass


@dataclass
class Req:
    cls: str
    line: int
    sus: float
    ef: int
    ep: int
    nf: int
    np: int


def parse_xml(path) -> list[Req]:
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


def _d(a, b):
    return a / b if b else 0.0


def _ochiai(r):
    return _d(r.ef, math.sqrt((r.ef + r.nf) * (r.ef + r.ep)))


def _tarantula(r):
    f, p = r.ef + r.nf, r.ep + r.np
    a, b = _d(r.ef, f), _d(r.ep, p)
    return _d(a, a + b)


def _dstar2(r):
    den = r.ep + r.nf
    return r.ef ** 2 / den if den else (1e12 * r.ef ** 2 if r.ef else 0.0)


def _zoltar(r):
    if not r.ef:
        return 0.0
    return r.ef / (r.ef + r.nf + r.ep + 10000 * r.nf * r.ep / r.ef)


HEURISTICS = {
    "jaguar": lambda r: r.sus,  # valor que o próprio Jaguar gravou (já normalizado por ele)
    "ochiai": _ochiai,
    "tarantula": _tarantula,
    "jaccard": lambda r: _d(r.ef, r.ef + r.nf + r.ep),
    "dstar2": _dstar2,
    "op2": lambda r: r.ef - _d(r.ep, r.ep + r.np + 1),
    "zoltar": _zoltar,
    "barinel": lambda r: 1 - _d(r.ep, r.ep + r.ef) if (r.ep + r.ef) else 0.0,
    "kulczynski2": lambda r: 0.5 * (_d(r.ef, r.ef + r.nf) + _d(r.ef, r.ef + r.ep)),
}


def _ranks(sorted_scores: list[float], s: float):
    n = len(sorted_scores)
    hi, lo = bisect_right(sorted_scores, s), bisect_left(sorted_scores, s)
    better, tied = n - hi, hi - lo
    return better + 1, better + (tied + 1) / 2, better + tied  # best, avg, worst


def outer(cls: str) -> str:
    return cls.split("$", 1)[0]


def evaluate(reqs: list[Req], fault_lines, heuristics) -> tuple[str, dict, list[dict]]:
    """Retorna (status, info, linhas_de_métricas). status: ok | empty_report |
    no_failing_tests | fault_not_covered."""
    info = {"n_requirements": len(reqs)}
    if not reqs:
        return "empty_report", info, []
    n_failed = max(r.ef + r.nf for r in reqs)
    n_passed = max(r.ep + r.np for r in reqs)
    info.update(n_failed_tests=n_failed, n_passed_tests=n_passed)
    if n_failed == 0:
        return "no_failing_tests", info, []
    wanted = {(f.cls, f.line) for f in fault_lines}
    hit = [i for i, r in enumerate(reqs) if (outer(r.cls), r.line) in wanted]
    fault_classes = {f.cls for f in fault_lines}
    info["fault_lines_covered"] = len(hit)
    if not hit:
        return "fault_not_covered", info, []
    rows = []
    n = len(reqs)
    for h in heuristics:
        fn = HEURISTICS[h]
        scores = [round(fn(r), 12) for r in reqs]
        srt = sorted(scores)
        best = min((_ranks(srt, scores[i]) for i in hit), key=lambda t: t[1])
        # nível de classe: suspeição da classe = máximo das suas linhas
        cls_max: dict[str, float] = {}
        for r, s in zip(reqs, scores):
            c = outer(r.cls)
            cls_max[c] = max(cls_max.get(c, -1e18), s)
        csrt = sorted(cls_max.values())
        cbest = min((_ranks(csrt, cls_max[c]) for c in fault_classes if c in cls_max),
                    key=lambda t: t[1], default=(None, None, None))
        row = {
            "heuristic": h, "n_requirements": n, "n_classes": len(cls_max),
            "rank_best": best[0], "rank_avg": best[1], "rank_worst": best[2],
            "exam_avg": best[1] / n, "exam_worst": best[2] / n,
            "class_rank_avg": cbest[1], "class_rank_worst": cbest[2],
        }
        for k in (1, 3, 5, 10):
            row[f"top{k}"] = int(best[2] <= k)  # pior caso nos empates (conservador)
        rows.append(row)
    return "ok", info, rows
