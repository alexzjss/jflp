"""Importa os resultados do pipeline PowerShell (relatorios_cobertura/<projeto>/<bug>/) para a cobertura.

Cada pasta de bug vira uma execução do Maven e, se há XML do Jaguar 1, uma do Jaguar 1, gravadas como as
execuções normais (só `origem: "importado"` as distingue). A origem é somente leitura."""
from __future__ import annotations

import contextlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from . import cobertura
from . import queue as jq
from . import status as st
from . import store
from .adapters.base import Bug
from .home import Home, Settings

ORIGEM = "importado"
_TESTES = re.compile(r"run=(\d+) fail=(\d+) err=(\d+) skip=(\d+)")
_TESTES_MVN = re.compile(r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)")
_DEPENDENCIA = re.compile(r"Could not resolve dependencies|Failed to read artifact descriptor|"
                          r"Could not transfer artifact|Non-resolvable parent POM", re.I)
_CAUDA_LOG = 400


def _ler_summary(pasta: Path) -> dict | None:
    try:
        dados = json.loads((pasta / "summary.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return dados if isinstance(dados, dict) else None


def _contagem(texto) -> tuple[int, int, int, int] | None:
    """(rodados, falhas, erros, ignorados) das duas grafias do resumo; None se não há contagem."""
    m = _TESTES.search(texto or "") or _TESTES_MVN.search(texto or "")
    return tuple(int(x) for x in m.groups()) if m else None


def status_maven(resumo: dict, log: str) -> str:
    """Status da execução do Maven a partir do `summary.json` (e do maven.log, para separar dependências)."""
    mvn = str(resumo.get("maven") or "").upper()
    if "CHECKOUT" in mvn:
        return st.CHECKOUT_FALHOU
    if mvn == "TIMEOUT":
        return st.TIMEOUT
    c = _contagem(resumo.get("maven_tests"))
    if not c or c[0] == 0:
        return st.DEPENDENCIA_NAO_RESOLVIDA if _DEPENDENCIA.search(log or "") else st.COMPILACAO_FALHOU
    return st.OK_TEST_FAIL if c[1] + c[2] > 0 else st.SEM_TESTE_FALHANDO


def _tests(resumo: dict) -> dict:
    c = _contagem(resumo.get("maven_tests"))
    if not c:
        return {"total": 0, "passed": 0, "failed": 0, "error": 0, "skipped": 0}
    total, falhas, erros, ign = c
    return {"total": total, "passed": max(total - falhas - erros - ign, 0), "failed": falhas, "error": erros,
            "skipped": ign}


def _falhas(pasta: Path) -> list[dict]:
    try:
        linhas = (pasta / "maven_failed_tests.txt").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [{"test": t.strip(), "message": None, "type": None, "frame": None} for t in linhas if t.strip()]


def _cauda_log(pasta: Path) -> str:
    """As últimas linhas do maven.log (vazio se não há log)."""
    try:
        linhas = (pasta / "maven.log").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(linhas[-_CAUDA_LOG:]) + "\n" if linhas else ""


def _n_requisitos(xml: Path) -> int:
    n = 0
    try:
        for _, el in ET.iterparse(xml, events=("end",)):
            if el.tag == "requirements":
                n += 1
                el.clear()
    except (ET.ParseError, OSError):
        return -1
    return n


def _escolher_xml(pasta: Path) -> tuple[Path | None, list[str]]:
    """O XML do Jaguar 1 com mais linhas; os demais viram avisos. XML ilegível não conta."""
    cand = []
    for x in sorted(pasta.glob("control-flow*.xml")):
        n = _n_requisitos(x)
        if n >= 0:
            cand.append((n, x))
    if not cand:
        return None, []
    cand.sort(key=lambda t: (-t[0], t[1].name))
    avisos = [f"Havia mais de um XML do Jaguar 1 (módulos); importado {cand[0][1].name}. "
              f"Não importados: {x.name} ({n} linhas)." for n, x in cand[1:]]
    return cand[0][1], avisos


def _bug(proj: str, nome: str, job: dict) -> Bug:
    return Bug("bugsjar", proj, nome, job.get("extra") or {})


def _gravar(home: Home, bug: Bug, kind: str, run: dict, anexos, textos=()) -> str:
    w = store.RunWriter(home, bug, kind)
    try:
        for src, rel in anexos:
            w.copy_into(src, rel)
        for rel, texto in textos:
            w.write_text(rel, texto)
        w.finish(run)
    except BaseException:
        if not w._done:
            w.finish({"status": st.DESCONHECIDO, "error": "falha ao importar"})
        raise
    return w.rel


def _marcar(home: Home, job: dict, rel: str, segundos: float | None) -> None:
    with contextlib.closing(jq._connect(home)) as con, con:
        con.execute("UPDATE jobs SET state='done', run_rel=?, error=NULL, seconds=?, origem=? WHERE id=?",
                    (rel, segundos, ORIGEM, job["id"]))
        jq._close_if_finished(con, job["batch_id"])


def _segundos(resumo: dict) -> float | None:
    try:
        return round(float(resumo["minutes"]) * 60, 1)
    except (KeyError, TypeError, ValueError):
        return None


def _base(resumo: dict) -> dict:
    return {"origem": ORIGEM, "test_scope": "all", "truth": [], "relevant_tests": [], "attempt_configs": [],
            "importado_de": {"branch": resumo.get("branch"), "profile": resumo.get("profile")}}


def _livre(job: dict) -> bool:
    """O job ainda espera resultado: pendente ou cancelado, e nunca importado."""
    return job["state"] in ("pending", "cancelled") and not job.get("origem") and not job.get("run_rel")


def importar_pipeline(home: Home, settings: Settings, raiz: Path) -> dict:
    """Importa os resultados de `raiz` (<projeto>/<bug>/summary.json) para os jobs do lote de cobertura."""
    raiz = Path(raiz)
    res = {"maven": 0, "jaguar1": 0, "ignorados": 0, "erros": []}
    bid = cobertura.batch_id(home)
    if bid is None:
        res["erros"].append("O lote de cobertura ainda não existe; sincronize a cobertura antes de importar.")
        return res
    jobs = {(j["bug_key"], j["kind"]): j for j in jq.batch_jobs(home, bid)}
    try:
        projetos = sorted(p for p in raiz.iterdir() if p.is_dir() and not p.name.startswith((".", "_")))
    except OSError as e:
        res["erros"].append(f"Não foi possível ler {raiz}: {e}")
        return res
    for proj in projetos:
        try:
            bugs = sorted(b for b in proj.iterdir() if b.is_dir() and b.name != "origin")
        except OSError as e:
            res["erros"].append(f"{proj.name}: {e}")
            continue
        for pasta in bugs:
            chave = f"bugsjar/{proj.name}/{pasta.name}"
            try:
                _importar_bug(home, chave, proj.name, pasta, jobs, res)
            except Exception as e:      # noqa: BLE001 - um bug quebrado não impede os outros
                res["erros"].append(f"{chave}: {e}")
    return res


def _importar_bug(home: Home, chave: str, proj: str, pasta: Path, jobs: dict, res: dict) -> None:
    jm, j1 = jobs.get((chave, "maven")), jobs.get((chave, "jaguar1"))
    if jm is None and j1 is None:
        res["ignorados"] += 1
        return
    resumo = _ler_summary(pasta)
    if resumo is None:
        res["ignorados"] += 1
        return
    cauda = _cauda_log(pasta)
    seg = _segundos(resumo)
    if jm is not None:
        if not _livre(jm):
            res["ignorados"] += 1
        else:
            status = status_maven(resumo, cauda)
            run = {"status": status, "tests": _tests(resumo), "failures": _falhas(pasta), "warnings": [],
                   "diagnosis": {"status": status, "cause": "", "evidence": [], "suggestions": [], "warnings": []},
                   **_base(resumo)}
            rel = _gravar(home, _bug(proj, pasta.name, jm), "maven", run, [],
                          [("outputs/maven-cauda.log", cauda)] if cauda else [])
            _marcar(home, jm, rel, seg)
            res["maven"] += 1
    if j1 is not None:
        if not _livre(j1):
            res["ignorados"] += 1
            return
        xml, avisos = _escolher_xml(pasta)
        if xml is None:
            return
        tests = _tests(resumo)
        status = st.OK_TEST_FAIL if tests["failed"] + tests["error"] > 0 else st.OK
        run = {"status": status, "tests": tests, "failures": _falhas(pasta), "warnings": avisos,
               "diagnosis": {"status": status, "cause": "", "evidence": [], "suggestions": [], "warnings": avisos},
               **_base(resumo)}
        rel = _gravar(home, _bug(proj, pasta.name, j1), "jaguar1", run, [(xml, "outputs/jaguar1.xml")])
        _marcar(home, j1, rel, seg)
        res["jaguar1"] += 1

