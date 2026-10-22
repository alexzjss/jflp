"""Comparações entre execuções gravadas: Maven × Jaguar (testes), Jaguar 1 × Jaguar 2 (espectros),
posição das linhas do defeito e resumo de lote com a taxa de sucesso.
Nada aqui calcula fórmulas próprias; só lê o que o Maven e o Jaguar produziram."""
from __future__ import annotations

import csv
import io
from collections import Counter
from pathlib import Path

from . import status as S
from .diffgt import FaultLine, select_lines
from .outputs import (LineScore, jaguar1_scores, outer, parse_jaguar1_log, parse_jaguar2_csv,
                      parse_jaguar2_xml, parse_surefire)
from .store import load_run, run_dir

_TOL = 1e-6
NOTE_J2 = "Jaguar 2 só grava linhas com susp > 0"


def compare_tests(a: dict, b: dict) -> dict:
    ok = lambda st: st == "passed"  # noqa: E731
    a_run = {t: s for t, s in a.items() if s != "skipped"}
    return {
        "a_counts": dict(Counter(a.values())),
        "b_counts": dict(Counter(b.values())),
        "only_a": sorted(set(a_run) - set(b)),
        "only_b": sorted(set(b) - set(a_run)),
        "outcome_differs": sorted((t, a_run[t], b[t]) for t in set(a_run) & set(b)
                                  if ok(a_run[t]) != ok(b[t])),
        "a_failing": sorted(t for t, s in a_run.items() if not ok(s)),
        "b_failing": sorted(t for t, s in b.items() if not ok(s)),
    }


def compare_spectra(a: list[LineScore], b: list[LineScore]) -> dict:
    ka = {(outer(s.cls), s.line): s for s in a}
    kb = {(outer(s.cls), s.line): s for s in b}
    pick = lambda s: {"cef": s.cef, "cep": s.cep, "susp": s.susp}  # noqa: E731
    diffs = []
    for key in sorted(set(ka) & set(kb)):
        x, y = ka[key], kb[key]
        if x.cef != y.cef or x.cep != y.cep or abs(x.susp - y.susp) > _TOL:
            diffs.append({"cls": key[0], "line": key[1], "a": pick(x), "b": pick(y)})
    return {"only_a": sorted(set(ka) - set(kb)), "only_b": sorted(set(kb) - set(ka)),
            "both": len(set(ka) & set(kb)), "diffs": diffs, "note": NOTE_J2}


def fault_positions(scores: list[LineScore], faults: list[FaultLine]) -> dict:
    """Posição das linhas do defeito, ordenando por `susp` decrescente (empates entre best e worst)."""
    vals = [round(s.susp, 12) for s in scores]
    wanted = {(f.cls, f.line) for f in select_lines(faults)}
    rows = []
    for s, v in zip(scores, vals):
        if (outer(s.cls), s.line) in wanted:
            better = sum(1 for x in vals if x > v)
            rows.append({"cls": s.cls, "line": s.line, "susp": s.susp, "rank_best": better + 1,
                         "rank_worst": sum(1 for x in vals if x >= v),
                         "tied": sum(1 for x in vals if x == v),
                         "cef": s.cef, "cep": s.cep, "cnf": s.cnf, "cnp": s.cnp})
    return {"total": len(scores), "lines": rows, "not_covered": len(wanted) - len(rows)}


def run_tests(home, rel: str) -> dict[str, str]:
    """Resultado dos testes de uma execução gravada ({} se os arquivos não existem)."""
    d = run_dir(home, rel)
    if load_run(home, rel).get("kind") == "jaguar1":
        log = d / "outputs" / "jaguar1.log.gz"
        return parse_jaguar1_log(log) if log.exists() else {}
    return parse_surefire(d / "outputs" / "surefire-reports")


def run_scores(home, rel: str) -> list[LineScore] | None:
    kind = load_run(home, rel).get("kind")
    out = run_dir(home, rel) / "outputs"
    if kind == "jaguar2":
        if (out / "jaguar2.csv").exists():
            return parse_jaguar2_csv(out / "jaguar2.csv")
        if (out / "jaguar2.xml").exists():
            return parse_jaguar2_xml(out / "jaguar2.xml")[1]
    elif kind == "jaguar1" and (out / "jaguar1.xml").exists():
        return jaguar1_scores(out / "jaguar1.xml")
    return None


def compare_runs(home, rel_a: str, rel_b: str) -> dict:
    ra, rb = load_run(home, rel_a), load_run(home, rel_b)
    sa, sb = run_scores(home, rel_a), run_scores(home, rel_b)
    truth = [FaultLine(**t) for t in (ra.get("truth") or rb.get("truth") or [])]
    return {"tests": compare_tests(run_tests(home, rel_a), run_tests(home, rel_b)),
            "spectra": compare_spectra(sa, sb) if sa is not None and sb is not None else None,
            "faults_a": fault_positions(sa, truth) if sa is not None else None,
            "faults_b": fault_positions(sb, truth) if sb is not None else None}


def batch_summary(home, runs: list[dict]) -> list[dict]:
    """Por (benchmark, project, kind): usa a última execução de cada bug."""
    last: dict[tuple, dict] = {}
    for r in runs:
        key = (r["benchmark"], r["project"], r["kind"], r["bug"])
        if key not in last or (r.get("started") or "") > (last[key].get("started") or ""):
            last[key] = r
    groups: dict[tuple, list[dict]] = {}
    for key, r in last.items():
        groups.setdefault(key[:3], []).append(r)
    rows = []
    for (bench, proj, kind), rs in sorted(groups.items(), key=lambda kv: tuple(str(x) for x in kv[0])):
        st = Counter(r.get("status") for r in rs)
        ok_n = sum(n for s, n in st.items() if s == S.OK)
        okf = st.get(S.OK_TEST_FAIL, 0)
        succ = sum(n for s, n in st.items() if S.is_success(s))
        rows.append({"benchmark": bench, "project": proj, "kind": kind, "bugs": len(rs),
                     "ok": ok_n, "ok_testes_falhando": okf,
                     "falhas": {s: n for s, n in sorted(st.items(), key=lambda x: str(x[0]))
                                if not S.is_success(s)},
                     "taxa_sucesso": round(succ / len(rs), 4) if rs else 0})
    return rows


def summary_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["benchmark", "project", "kind", "bugs", "ok", "ok_testes_falhando", "falhas",
                "taxa_sucesso"])
    for r in rows:
        w.writerow([r["benchmark"], r["project"], r["kind"], r["bugs"], r["ok"],
                    r["ok_testes_falhando"], sum(r["falhas"].values()), r["taxa_sucesso"]])
    return buf.getvalue()
