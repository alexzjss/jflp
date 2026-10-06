"""Acompanhamento de uma campanha de coleta: níveis de sucesso, progresso e ETA."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

LEVEL_NAMES = {1: "XML gerado", 2: "+ teste falhando", 3: "+ linha defeituosa coberta"}


def level(meta: dict) -> int:
    """Nível de sucesso (cumulativo) de um bug, a partir do status."""
    return {"ok": 3, "fault_not_covered": 2, "no_failing_tests": 1}.get(meta.get("status"), 0)


def quality_flags(meta: dict) -> list[str]:
    """Alertas de qualidade dos dados de um bug (mesmo com status ok)."""
    flags = []
    nf, npass = meta.get("n_failed_tests") or 0, meta.get("n_passed_tests") or 0
    if nf and nf / (nf + npass) > 0.5:
        flags.append("many_failures")          # mais da metade dos testes "falha": ambiente suspeito
    if (meta.get("analysis_exceptions") or 0) > 0:
        flags.append("instrumentation_errors")  # classes sem instrumentação: espectro incompleto
    ntc, exc = meta.get("n_test_classes") or 0, len(meta.get("excluded_classes") or [])
    if ntc and exc / ntc > 0.1:
        flags.append("tests_excluded")          # >10% das classes de teste excluídas
    if meta.get("truth_crosscheck") == "disagree":
        flags.append("truth_disagrees")
    return flags


def fmt_dur(sec: float | None) -> str:
    if sec is None:
        return "-"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else (f"{m}m{s:02d}s" if m else f"{s}s")


def load_rows(results: Path) -> list[dict]:
    rows = []
    for mp in sorted(Path(results).glob("*/*/*/meta.json")):
        m = json.loads(mp.read_text(encoding="utf-8"))
        rows.append({"benchmark": mp.parts[-4], "project": mp.parts[-3], "bug": mp.parts[-2],
                     "status": m.get("status", "error"), "level": level(m),
                     "seconds": m.get("total_seconds") or 0,
                     "quality": m.get("quality") or []})
    return rows


def summarize(rows: list[dict], totals: dict | None = None) -> list[dict]:
    """Uma linha por (benchmark, projeto). `totals` = {(benchmark, projeto): nº de bugs existentes}."""
    totals = totals or {}
    out = []
    for b, p in sorted({(r["benchmark"], r["project"]) for r in rows} | set(totals)):
        rs = [r for r in rows if (r["benchmark"], r["project"]) == (b, p)]
        total = max(totals.get((b, p), 0), len(rs))
        secs = [r["seconds"] for r in rs if r["seconds"]]
        mean = sum(secs) / len(secs) if secs else None
        remaining = total - len(rs)
        out.append({
            "benchmark": b, "project": p, "total": total, "done": len(rs), "remaining": remaining,
            "l1": sum(r["level"] >= 1 for r in rs), "l2": sum(r["level"] >= 2 for r in rs),
            "l3": sum(r["level"] >= 3 for r in rs),
            "clean": sum(r["level"] >= 3 and not r.get("quality") for r in rs), "mean_s": mean,
            "flags": dict(Counter(f for r in rs for f in r.get("quality", []))),
            "eta_s": mean * remaining if mean is not None else None,
            "statuses": dict(Counter(r["status"] for r in rs)),
        })
    return out


def _pct(a, b):
    return f"{100 * a / b:.0f}%" if b else "-"


def render_markdown(summary: list[dict]) -> str:
    head = ("| Projeto | Total | Feitos | L1 | L2 | L3 | L3 limpo | L3/Feitos | Tempo médio | ETA |\n"
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n")
    lines = []
    tot = {"total": 0, "done": 0, "l1": 0, "l2": 0, "l3": 0, "clean": 0}
    for s in summary:
        for k in tot:
            tot[k] += s[k]
        lines.append(f"| {s['benchmark']}/{s['project']} | {s['total']} | {s['done']} | {s['l1']} | "
                     f"{s['l2']} | {s['l3']} | {s['clean']} | {_pct(s['l3'], s['done'])} | {fmt_dur(s['mean_s'])} | "
                     f"{fmt_dur(s['eta_s'])} |")
    lines.append(f"| **Total** | {tot['total']} | {tot['done']} | {tot['l1']} | {tot['l2']} | {tot['l3']} | "
                 f"{tot['clean']} | {_pct(tot['l3'], tot['done'])} | | |")
    legend = ("\n\nNíveis (cumulativos): " + "; ".join(f"L{k} = {v}" for k, v in LEVEL_NAMES.items())
              + ". **L3 limpo** = L3 sem alertas de qualidade.")
    fl = Counter()
    for s_ in summary:
        fl.update(s_["flags"])
    if fl:
        legend += "\n\nAlertas de qualidade: " + ", ".join(f"`{k}` {v}" for k, v in sorted(fl.items()))
    st = Counter()
    for s in summary:
        st.update(s["statuses"])
    reasons = "\n\nStatus: " + ", ".join(f"`{k}` {v}" for k, v in sorted(st.items(), key=lambda kv: -kv[1]))
    return head + "\n".join(lines) + legend + (reasons if st else "") + "\n"
