"""`sbfl compare`: o mesmo módulo com e sem o Jaguar, e o rank que o próprio Jaguar fornece.

 - sem Jaguar: `mvn test` no módulo (resultado dos testes nos relatórios do surefire);
 - com Jaguar: testes executados na tentativa final do Jaguar (log) e o relatório XML dele;
 - rank do Jaguar: posição das linhas do defeito segundo o `suspicious-value` gravado pelo Jaguar.
Nada aqui calcula fórmulas próprias; só lê o que o Maven e o Jaguar produziram."""
from __future__ import annotations

import gzip
import json
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from .build import resolve_tool
from .diffgt import FaultLine, select_lines
from .errors import StageError
from .jaguar import TEST_RE
from .proc import make_env, run
from .report import outer, parse_xml

MARK = "# sbfl attempt"


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


def parse_jaguar_log(path: Path) -> dict[str, str]:
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


def compare_tests(maven: dict, jag: dict) -> dict:
    ok = lambda st: st == "passed"
    m_run = {t: s for t, s in maven.items() if s != "skipped"}
    return {
        "maven": dict(Counter(maven.values())),
        "jaguar": dict(Counter(jag.values())),
        "only_maven": sorted(set(m_run) - set(jag)),
        "only_jaguar": sorted(set(jag) - set(m_run)),
        "outcome_differs": sorted((t, m_run[t], jag[t]) for t in set(m_run) & set(jag)
                                  if ok(m_run[t]) != ok(jag[t])),
        "maven_failing": sorted(t for t, s in m_run.items() if not ok(s)),
        "jaguar_failing": sorted(t for t, s in jag.items() if not ok(s)),
    }


def jaguar_rank(xml_path: Path, faults: list[FaultLine]) -> dict:
    """Posição das linhas do defeito no relatório do Jaguar. `position_in_file` é a ordem em que o
    Jaguar gravou; rank_best/rank_worst vêm do `suspicious-value` dele (empates entre os dois)."""
    reqs = parse_xml(xml_path)
    vals = [round(r.sus, 12) for r in reqs]
    wanted = {(f.cls, f.line) for f in select_lines(faults)}
    rows = []
    for pos, (r, v) in enumerate(zip(reqs, vals), 1):
        if (outer(r.cls), r.line) in wanted:
            better = sum(1 for x in vals if x > v)
            tied = sum(1 for x in vals if x == v)
            rows.append({"cls": r.cls, "line": r.line, "position_in_file": pos,
                         "suspicious_value": r.sus, "rank_best": better + 1,
                         "rank_worst": better + tied, "tied": tied,
                         "cef": r.ef, "cep": r.ep, "cnf": r.nf, "cnp": r.np})
    return {"total": len(reqs), "sorted_desc": all(a >= b for a, b in zip(vals, vals[1:])),
            "lines": rows, "not_covered": len(wanted) - len(rows)}


def render(bug_key: str, meta: dict, cmp: dict, rank: dict) -> str:
    c = lambda d: ", ".join(f"{k} {v}" for k, v in sorted(d.items())) or "nenhum"
    out = [f"# Comparação com e sem Jaguar — {bug_key}", "",
           "## Testes", f"- Sem Jaguar (mvn test): {c(cmp['maven'])}",
           f"- Com Jaguar (tentativa final): {c(cmp['jaguar'])}",
           f"- Classes excluídas nas tentativas do Jaguar: {', '.join(meta.get('excluded_classes') or []) or 'nenhuma'}",
           f"- Falham no Maven: {len(cmp['maven_failing'])}; falham no Jaguar: {len(cmp['jaguar_failing'])}"]
    for title, key in (("Só no Maven (o Jaguar não executou)", "only_maven"),
                       ("Só no Jaguar (o Maven não executou)", "only_jaguar")):
        out.append(f"- {title}: {len(cmp[key])}" + (f" — ex.: {', '.join(cmp[key][:3])}" if cmp[key] else ""))
    d = cmp["outcome_differs"]
    out.append(f"- Resultado diferente entre os dois: {len(d)}" +
               ("".join(f"\n  - {t}: Maven {m}, Jaguar {j}" for t, m, j in d[:10])))
    out += ["", "## Rank fornecido pelo Jaguar (linhas do defeito)",
            f"Relatório com {rank['total']} linhas. O arquivo está "
            f"{'ordenado' if rank['sorted_desc'] else 'NÃO ordenado'} por suspicious-value decrescente."]
    if not rank["lines"]:
        out.append("Nenhuma linha do defeito aparece no relatório do Jaguar (não foi coberta).")
    else:
        out += ["", "| Linha | Posição no arquivo | suspicious-value | Rank (melhor–pior) | Empatadas | cef/cep/cnf/cnp |",
                "|---|---:|---:|---:|---:|---|"]
        for r in rank["lines"]:
            out.append(f"| {r['cls']}:{r['line']} | {r['position_in_file']} | {r['suspicious_value']:.4f} | "
                       f"{r['rank_best']}–{r['rank_worst']} de {rank['total']} | {r['tied']} | "
                       f"{r['cef']}/{r['cep']}/{r['cnf']}/{r['cnp']} |")
    if rank["not_covered"]:
        out.append(f"\n{rank['not_covered']} linha(s) do defeito não aparecem no relatório.")
    return "\n".join(out) + "\n"


def run_compare(cfg, adapter, bug) -> str:
    d = cfg.results / bug.benchmark / bug.project / bug.bug_id
    xmls = sorted(d.glob("jaguar_*.xml"))
    if not (d / "meta.json").exists() or not xmls:
        raise StageError("no_results", f"sem resultado do Jaguar para {bug.bug_id}: rode `sbfl run` antes")
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    faults = [FaultLine(**t) for t in json.loads((d / "truth.json").read_text(encoding="utf-8"))]
    jag = parse_jaguar_log(d / "jaguar.log.gz")

    mlog = d / "maven_test.log"
    mlog.write_text("", encoding="utf-8")
    ctx = adapter.prepare(bug, mlog)          # mesmo checkout e build do `run`
    try:
        if not (ctx.project_dir / "pom.xml").exists():
            raise StageError("not_maven", "a execução sem Jaguar só é suportada para módulos Maven")
        cmd = [resolve_tool(cfg.get("java", "mvn")), "-B", "test", *cfg.get("maven", "skip_flags"),
               *cfg.get("maven", "extra_args"), "-Dmaven.test.failure.ignore=true", "-DfailIfNoTests=false"]
        r = run(cmd, cwd=ctx.project_dir, env=make_env(cfg, ctx.java_home),
                timeout=cfg.get("maven", "build_timeout_s"), log_path=mlog)
        maven = parse_surefire(ctx.project_dir / "target" / "surefire-reports")
        if not maven:
            raise StageError("no_surefire", "o `mvn test` não gerou relatórios do surefire "
                             f"(rc={r['rc']}); veja {mlog}")
    finally:
        ctx.cleanup()
    cmp, rank = compare_tests(maven, jag), jaguar_rank(xmls[0], faults)
    text = render(bug.key, meta, cmp, rank)
    (d / "compare.md").write_text(text, encoding="utf-8")
    (d / "compare.json").write_text(json.dumps({"tests": cmp, "jaguar_rank": rank}, indent=2,
                                               ensure_ascii=False), encoding="utf-8")
    return text
