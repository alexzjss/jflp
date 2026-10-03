from __future__ import annotations

import json
from pathlib import Path

ORDER = ["ok", "fault_not_covered", "no_failing_tests", "empty_report", "jaguar_crash",
         "jaguar_timeout", "no_tests", "no_truth", "build_failed", "d4j_failed",
         "git_failed", "error"]


def load_all(results: Path):
    import pandas as pd
    metas, rows = [], []
    for mp in results.glob("*/*/*/meta.json"):
        bench, proj, bug = mp.parts[-4], mp.parts[-3], mp.parts[-2]
        m = json.loads(mp.read_text(encoding="utf-8"))
        metas.append({"benchmark": bench, "project": proj, "bug": bug, **{
            k: v for k, v in m.items() if not isinstance(v, (list, dict))}})
        mt = mp.parent / "metrics.json"
        if mt.exists():
            for r in json.loads(mt.read_text(encoding="utf-8")):
                rows.append({"benchmark": bench, "project": proj, "bug": bug, **r})
    return pd.DataFrame(metas), pd.DataFrame(rows)


def aggregate(results: Path, out: Path) -> list[Path]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    out.mkdir(parents=True, exist_ok=True)
    metas, df = load_all(results)
    made = []
    if metas.empty:
        raise SystemExit("Nenhum resultado em " + str(results))
    metas.to_csv(out / "status_por_bug.csv", index=False)
    funnel = metas.groupby(["benchmark", "project", "status"]).size().unstack(fill_value=0)
    funnel = funnel[[c for c in ORDER if c in funnel.columns] +
                    [c for c in funnel.columns if c not in ORDER]]
    funnel.to_csv(out / "funil_status.csv")
    made.append(out / "funil_status.csv")
    ax = funnel.plot(kind="barh", stacked=True, figsize=(9, max(3, 0.5 * len(funnel) + 2)))
    ax.set_xlabel("nº de bugs"); ax.set_title("Resultado da coleta por projeto")
    plt.tight_layout(); plt.savefig(out / "funil_status.png", dpi=200); plt.close()
    made.append(out / "funil_status.png")
    if df.empty:
        return made
    df.to_csv(out / "metricas_por_bug.csv", index=False)
    g = df.groupby("heuristic")
    summ = g.agg(n_bugs=("bug", "nunique"), exam_media=("exam_avg", "mean"),
                 exam_mediana=("exam_avg", "median"), rank_medio=("rank_avg", "mean"),
                 rank_mediano=("rank_avg", "median"), top1=("top1", "sum"), top3=("top3", "sum"),
                 top5=("top5", "sum"), top10=("top10", "sum")).sort_values("exam_media")
    summ.to_csv(out / "resumo_por_heuristica.csv")
    made.append(out / "resumo_por_heuristica.csv")
    # acc@N
    fig, ax = plt.subplots(figsize=(9, 4.5))
    hs, w = list(summ.index), 0.2
    for i, k in enumerate(["top1", "top3", "top5", "top10"]):
        ax.bar(np.arange(len(hs)) + i * w, 100 * summ[k] / summ["n_bugs"], w, label=k.replace("top", "Top-"))
    ax.set_xticks(np.arange(len(hs)) + 1.5 * w); ax.set_xticklabels(hs, rotation=30, ha="right")
    ax.set_ylabel("% dos bugs localizados (pior caso nos empates)"); ax.legend()
    plt.tight_layout(); plt.savefig(out / "topN_por_heuristica.png", dpi=200); plt.close()
    made.append(out / "topN_por_heuristica.png")
    # CDF do EXAM
    fig, ax = plt.subplots(figsize=(7, 5))
    for h, sub in df.groupby("heuristic"):
        x = np.sort(sub["exam_avg"].values * 100)
        ax.step(x, np.arange(1, len(x) + 1) / len(x) * 100, where="post", label=h)
    ax.set_xscale("symlog", linthresh=0.1)
    ax.set_xlabel("% do código inspecionado (EXAM, rank médio)"); ax.set_ylabel("% de bugs localizados")
    ax.legend(); ax.grid(alpha=.3)
    plt.tight_layout(); plt.savefig(out / "exam_cdf.png", dpi=200); plt.close()
    made.append(out / "exam_cdf.png")
    # boxplot
    fig, ax = plt.subplots(figsize=(9, 4.5))
    labels = list(summ.index)
    ax.boxplot([df[df.heuristic == h]["exam_avg"].values * 100 for h in labels], tick_labels=labels)
    ax.set_yscale("symlog", linthresh=0.1); ax.set_ylabel("EXAM (%)")
    plt.setp(ax.get_xticklabels(), rotation=30, ha="right")
    plt.tight_layout(); plt.savefig(out / "exam_boxplot.png", dpi=200); plt.close()
    made.append(out / "exam_boxplot.png")
    return made
