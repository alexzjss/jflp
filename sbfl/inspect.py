"""`sbfl inspect <bug>`: resumo de um resultado para depurar por que um bug falhou (ou deu certo)."""
from __future__ import annotations

import gzip
import json
from pathlib import Path

from .report import HEURISTICS, outer, parse_xml


def find_dir(results: Path, token: str) -> Path | None:
    for mp in sorted(Path(results).glob("*/*/*/meta.json")):
        d = mp.parent
        if token in (d.name, "/".join(d.parts[-3:])):
            return d
    return None


def render(d: Path, top: int = 10) -> str:
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    out = [f"# {meta.get('bug', d.name)}", f"status: {meta.get('status')}"]
    for k in ("detail", "java", "total_seconds", "attempts", "excluded_classes", "analysis_exceptions",
              "n_test_classes", "junit5_skipped", "abstract_skipped", "n_failed_tests",
              "n_passed_tests", "fault_lines_covered", "truth_crosscheck", "quality", "java_level",
              "module"):
        if meta.get(k) not in (None, [], ""):
            out.append(f"{k}: {meta[k]}")
    if meta.get("failed_tests"):
        out.append("failed_tests (primeiros 5): " + ", ".join(meta["failed_tests"][:5]))
    tp = d / "truth.json"
    truth = json.loads(tp.read_text(encoding="utf-8")) if tp.exists() else []
    if truth:
        out.append("ground truth: " + ", ".join(f"{t['cls']}:{t['line']}({t['kind']})" for t in truth[:8])
                   + (" ..." if len(truth) > 8 else ""))
    xmls = sorted(d.glob("jaguar_*.xml"))
    if xmls:
        reqs = parse_xml(xmls[0])
        fault = {(t["cls"], t["line"]) for t in truth}
        fn = HEURISTICS["ochiai"]
        ranked = sorted(reqs, key=lambda r: -fn(r))[:top]
        out.append(f"\ntop {top} por Ochiai ('*' = linha do ground truth):")
        for r in ranked:
            mark = "*" if (outer(r.cls), r.line) in fault else " "
            out.append(f" {mark} {fn(r):.4f}  {r.cls}:{r.line}  (cef={r.ef} cep={r.ep} cnf={r.nf} cnp={r.np})")
    lg = d / "jaguar.log.gz"
    if lg.exists() and meta.get("status") not in ("ok",):
        with gzip.open(lg, "rt", encoding="utf-8", errors="replace") as f:
            tail = f.read().splitlines()[-15:]
        out.append("\núltimas linhas do log:\n" + "\n".join("  " + x[:200] for x in tail))
    return "\n".join(out) + "\n"
