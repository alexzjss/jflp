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
  jaguar2.csv            # classe,linha,cef,cnf,cep,cnp,susp (só linhas com susp > 0)
  jaguar2.xml
  surefire-reports/
  env.json               # ambiente da execução (ferramentas, SO, commit)
```

## Colunas do manifest

- `benchmark`, `project`, `bug`: identificação do defeito.
- `status`: status da execução usada (ou da última tentativa, se nenhuma foi bem-sucedida).
- `test_scope`: escopo de testes (`relevant` ou `all`).
- `n_tests`, `n_failed`: total de testes e quantos falharam ou deram erro.
- `jdk`, `maven`: versões usadas na execução.
- `jaguar2_version`: versão do Jaguar 2 (da configuração no momento da exportação).
- `recipe`: receita aplicada, se houver.
- `path`: pasta do bug dentro do dataset, relativa à raiz.

O `manifest.json` traz o mesmo conteúdo, com `schema_version` (__VERSION__).
"""


def _safe_name(name: str) -> str:
    s = re.sub(r"[^\w.-]", "_", name or "").strip(".")
    if not s:
        raise ValueError("Informe um nome para o dataset.")
    return s


def _batch_bugs(home: Home, batch_id: int) -> list[Bug]:
    from .queue import _connect
    with contextlib.closing(_connect(home)) as con:
        rows = con.execute("SELECT DISTINCT bug_key FROM jobs WHERE batch_id=? ORDER BY idx", (batch_id,)).fetchall()
    return [Bug(*r[0].split("/", 2)) for r in rows]


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


def export(home: Home, name: str, bugs: list[Bug] | None = None, batch_id: int | None = None) -> Path:
    name = _safe_name(name)
    dest = home.datasets / name
    if dest.exists():
        raise ValueError(f"Já existe um dataset chamado '{name}'. Escolha outro nome.")
    if bugs is None:
        bugs = _batch_bugs(home, batch_id) if batch_id is not None else _all_bugs(home)
    version = str(load_settings(home).get("jaguar2", "version", ""))
    rows: list[dict] = []
    tmp = home.datasets / f".{name}.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        for bug in bugs:
            runs = store.list_runs(home, bug.benchmark, bug.project, bug.bug_id, "jaguar2")
            if not runs:
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
                        shutil.copyfile(src / "outputs" / f, out / f)
                if (src / "outputs" / "surefire-reports").is_dir():
                    shutil.copytree(src / "outputs" / "surefire-reports", out / "surefire-reports")
                if (src / "env.json").is_file():
                    shutil.copyfile(src / "env.json", out / "env.json")
            rows.append({
                "benchmark": bug.benchmark, "project": bug.project, "bug": bug.bug_id,
                "status": run.get("status") or use["status"], "test_scope": run.get("test_scope") or "",
                "n_tests": tests.get("total", ""),
                "n_failed": ((tests.get("failed") or 0) + (tests.get("error") or 0)) if tests else "",
                "jdk": (te.get("jdk") or {}).get("major", ""), "maven": (te.get("maven") or {}).get("version", ""),
                "jaguar2_version": version, "recipe": _recipe_name(run), "path": rel_dir})
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
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return dest
