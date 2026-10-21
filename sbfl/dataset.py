"""Exportação de um dataset de saídas do Jaguar 2 com layout fixo e manifest (dados para publicar)."""
from __future__ import annotations

import contextlib
import csv
import json
import re
import shutil
from pathlib import Path

from . import status as st
from . import store
from .adapters.base import Bug
from .home import Home, load_settings
from .prepare import _rmtree
from .winpath import long_path

SCHEMA_VERSION = 1
COLUMNS = ["benchmark", "project", "bug", "status", "test_scope", "n_tests", "n_failed", "jdk", "maven",
           "jaguar2_version", "recipe", "path"]

README = """# Dataset de saídas do Jaguar 2

Gerado pelo JFLP Studio. Cada bug tem a saída pura da última execução do Jaguar 2 bem-sucedida
(status `ok` ou `ok_testes_falhando`). Bugs sem execução bem-sucedida aparecem no manifest com o
status da última tentativa e o campo `path` vazio, para que a taxa de sucesso fique explícita.

## Layout

```
README.md
manifest.csv
manifest.json
<benchmark>/<projeto>/<bug>/
  jaguar2.csv            # SEM linha de cabeçalho; colunas classe,linha,cef,cnf,cep,cnp,susp (só linhas com susp > 0)
  jaguar2.xml
  surefire-reports/
  env.json               # ambiente da execução (ferramentas, SO, commit)
```

## Colunas do manifest

- `benchmark`, `project`, `bug`: identificação do defeito.
- `status`: status da execução usada (ou da última tentativa, se nenhuma foi bem-sucedida).
  Além dos status do JFLP, o manifest usa: `sem_execucao` (selecionado, mas nunca executado),
  `cancelado` (job cancelado antes de rodar) e `desconhecido` (o job terminou com erro sem execução gravada).
- `test_scope`: escopo de testes (`relevant` ou `all`).
- `n_tests`, `n_failed`: total de testes e quantos falharam ou deram erro.
- `jdk`, `maven`: versões usadas na execução.
- `jaguar2_version`: versão do Jaguar 2 usada na execução (do plano do POM; se não houver registro, a da configuração no momento da exportação).
- `recipe`: receita aplicada, se houver.
- `path`: pasta do bug dentro do dataset, relativa à raiz.

O `manifest.json` traz o mesmo conteúdo, com `schema_version` (__VERSION__).
"""


def _safe_name(name: str) -> str:
    s = re.sub(r"[^\w.-]", "_", name or "").strip(".")
    if not s:
        raise ValueError("Informe um nome para o dataset.")
    return s


def _batch_selection(home: Home, batch_id: int) -> tuple[list[Bug], dict[str, str]]:
    """Bugs do lote (com extra) e, para cada chave, o status pelo estado dos jobs."""
    from .queue import batch_jobs
    bugs: dict[str, Bug] = {}
    status: dict[str, str] = {}
    for j in batch_jobs(home, batch_id):
        b, p, i = j["bug_key"].split("/", 2)
        bugs.setdefault(j["bug_key"], Bug(b, p, i, j["extra"]))
        if j["error"]:
            status[j["bug_key"]] = "desconhecido"
        elif j["state"] == "cancelled":
            status.setdefault(j["bug_key"], "cancelado")
    return list(bugs.values()), status


def _all_bugs(home: Home) -> list[Bug]:
    seen: dict[tuple, Bug] = {}
    for r in store.list_runs(home, kind="jaguar2"):
        seen.setdefault((r["benchmark"], r["project"], r["bug"]), Bug(r["benchmark"], r["project"], r["bug"]))
    return sorted(seen.values(), key=lambda b: b.key)


def _recipe_name(run: dict) -> str:
    rec = run.get("recipe")
    if not rec:
        return ""
    match = rec.get("match")
    return match if isinstance(match, str) else json.dumps(match, ensure_ascii=False)


def _toolenv(home: Home, rel: str) -> dict:
    try:
        env = json.loads((store.run_dir(home, rel) / "env.json").read_text(encoding="utf-8"))
        return env.get("toolenv") or {}
    except (OSError, ValueError):
        return {}


def _run_jaguar2_version(home: Home, rel: str, fallback: str) -> str:
    """Versão do Jaguar 2 gravada no plano do POM da última tentativa real; senão a da configuração atual."""
    attempts = store.run_dir(home, rel) / "attempts"
    nums = sorted((int(d.name) for d in attempts.iterdir() if d.name.isdigit()), reverse=True)         if attempts.is_dir() else []
    for n in nums:
        try:
            data = json.loads((attempts / str(n) / "attempt.json").read_text(encoding="utf-8"))
            v = ((data.get("extra") or {}).get("pom_plan") or {}).get("jaguar2_version")
        except (OSError, ValueError, AttributeError):
            continue
        if v:
            return str(v)
    return fallback


def export(home: Home, name: str, bugs: list[Bug] | None = None, batch_id: int | None = None) -> Path:
    name = _safe_name(name)
    dest = home.datasets / name
    if dest.exists():
        raise ValueError(f"Já existe um dataset chamado '{name}'. Escolha outro nome.")
    job_status: dict[str, str] = {}
    if bugs is None:
        if batch_id is not None:
            bugs, job_status = _batch_selection(home, batch_id)
        else:
            bugs = _all_bugs(home)
    version = str(load_settings(home).get("jaguar2", "version", ""))
    rows: list[dict] = []
    tmp = home.datasets / f".{name}.tmp"
    _rmtree(tmp, strict=False)          # sobra de uma exportação interrompida (apaga até somente-leitura)
    tmp.mkdir(parents=True)
    try:
        for bug in bugs:
            runs = store.list_runs(home, bug.benchmark, bug.project, bug.bug_id, "jaguar2")
            if not runs:   # selecionado mas sem execução: entra no manifest (taxa de sucesso explícita)
                rows.append({c: "" for c in COLUMNS} | {
                    "benchmark": bug.benchmark, "project": bug.project, "bug": bug.bug_id,
                    "status": job_status.get(bug.key, "sem_execucao"), "jaguar2_version": version})
                continue
            good = next((r for r in runs if st.is_success(r["status"])), None)
            use = good or runs[0]
            run = store.load_run(home, use["path"])
            tests = run.get("tests") or {}
            te = _toolenv(home, use["path"])
            rel_dir = ""
            if good:
                rel_dir = "/".join((bug.benchmark, bug.project, bug.bug_id))
                out = tmp.joinpath(*rel_dir.split("/"))
                out.mkdir(parents=True, exist_ok=True)
                src = store.run_dir(home, use["path"])
                for f in ("jaguar2.csv", "jaguar2.xml"):
                    if (src / "outputs" / f).is_file():
                        shutil.copyfile(long_path(src / "outputs" / f), long_path(out / f))
                if (src / "outputs" / "surefire-reports").is_dir():
                    shutil.copytree(long_path(src / "outputs" / "surefire-reports"), long_path(out / "surefire-reports"))
                if (src / "env.json").is_file():
                    shutil.copyfile(long_path(src / "env.json"), long_path(out / "env.json"))
                store.make_writable(out)    # as execuções são somente-leitura; o dataset exportado não
            rows.append({
                "benchmark": bug.benchmark, "project": bug.project, "bug": bug.bug_id,
                "status": run.get("status") or use["status"], "test_scope": run.get("test_scope") or "",
                "n_tests": tests.get("total", ""),
                "n_failed": ((tests.get("failed") or 0) + (tests.get("error") or 0)) if tests else "",
                "jdk": (te.get("jdk") or {}).get("major", ""), "maven": (te.get("maven") or {}).get("version", ""),
                "jaguar2_version": _run_jaguar2_version(home, use["path"], version),
                "recipe": _recipe_name(run), "path": rel_dir})
        with open(tmp / "manifest.csv", "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
        (tmp / "manifest.json").write_text(json.dumps(
            {"schema_version": SCHEMA_VERSION, "name": name, "columns": COLUMNS, "bugs": rows},
            ensure_ascii=False, indent=2), encoding="utf-8")
        (tmp / "README.md").write_text(README.replace("__VERSION__", str(SCHEMA_VERSION)), encoding="utf-8")
        tmp.rename(dest)
    except BaseException:
        _rmtree(tmp, strict=False)
        raise
    return dest
