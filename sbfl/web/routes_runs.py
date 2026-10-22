"""Telas de execução, comparação, lotes, receitas e exportação de dataset."""
from __future__ import annotations

import gzip
import json
import re
import os
import subprocess
import sys
import tempfile
import tomllib
import xml.etree.ElementTree as ET
import zipfile
from html import escape
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse

from .. import bugview, compare, dataset, recipes, source, store
from .. import queue as jobs_queue
from .. import status as S
from ..adapters.base import Bug
from ..autosearch import effective_jdk_maven
from ..diagnose import SEM_INSTRUMENTACAO, applicable
from ..diffgt import FaultLine, select_lines
from ..outputs import (AVISO_LOG_INTERROMPIDO, jaguar1_counts, outer, parse_jaguar1_log,
                       ranking_csv, ranking_rows, reconcile_jaguar1, test_counts)
from ..winpath import inside
from ..run_maven import AttemptConfig
from .routes_bugs import KIND_LABEL, KINDS, _find_bug

router = APIRouter()

PAGE_SIZE = 200
MAX_MATCHES = 500
LOG_CONTEXT = 2
ORDER_COLS = ("cls", "line", "cef", "cep", "cnf", "cnp", "susp")
LIST_CAP = 200
MAX_FILES = 500
MAX_SELECT = 200


def _render(request: Request, name: str, ctx: dict, status: int = 200):
    return request.app.state.templates.TemplateResponse(request, name, ctx, status_code=status)


def _msg(request: Request, page: str, title: str, text: str, status: int):
    return _render(request, "mensagem.html", {"page": page, "titulo": title, "texto": text}, status)


def _open_reply(request: Request, page: str, title: str, text: str, status: int):
    """Resposta de "abrir pasta": só um trecho quando vem do htmx, página inteira caso contrário."""
    if request.headers.get("hx-request"):
        cls = "erro" if status >= 400 else "vazio"
        return HTMLResponse(f'<span class="{cls}">{escape(text)}</span>', status_code=status)
    return _msg(request, page, title, text, status)


# ------------------------------------------------------------------ helpers

def _valid_rel(rel: str) -> bool:
    parts = rel.split("/")
    return (len(parts) == 4 and all(p and p not in (".", "..") and not p.startswith(".")
                                    and "\\" not in p and ":" not in p for p in parts))


def _load(request: Request, rel: str) -> tuple[dict, Path] | None:
    """(run.json, pasta) da execução, ou None se o caminho é inválido ou não existe."""
    home = request.app.state.home
    if not _valid_rel(rel):
        return None
    d = store.run_dir(home, rel)
    try:
        if not inside(d, home.runs) or not (d / "run.json").is_file():
            return None
        return store.load_run(home, rel), d
    except (OSError, ValueError):
        return None


def _no_run(request: Request, rel: str):
    return _msg(request, "bugs", "Execução não encontrada", f"Não existe a execução: {rel}", 404)


def _inside(base: Path, p: str) -> Path | None:
    """`p` relativo resolvido dentro de `base`; None se tenta sair (.., absoluto, unidade)."""
    if not p or p.startswith(("/", "\\")) or ":" in p or ".." in p.replace("\\", "/").split("/"):
        return None
    target = Path(os.path.abspath(base / p))
    return target if inside(target, base) else None


def _last_cfg(run: dict) -> dict:
    cfgs = run.get("attempt_configs") or []
    cfg = dict(cfgs[-1]) if cfgs else {}
    cfg.setdefault("fixes_on", [])
    cfg.setdefault("fixes_off", [])
    cfg.setdefault("params", {})
    cfg.setdefault("jdk", None)
    cfg.setdefault("maven", None)
    cfg.setdefault("test_scope", run.get("test_scope") or "relevant")
    return cfg


def _can_save_recipe(run: dict) -> bool:
    cfg = _last_cfg(run)
    return S.is_success(run.get("status") or "") and ((run.get("attempts") or 0) > 1 or bool(cfg["fixes_on"]))


def _open_path(path: Path) -> None:
    if sys.platform.startswith("win"):
        os.startfile(str(path))                       # noqa: S606
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])   # noqa: S603,S607


def _files(d: Path) -> list[dict]:
    out = []
    for f in sorted(d.rglob("*")):
        if f.is_file():
            out.append({"rel": f.relative_to(d).as_posix(), "size": f.stat().st_size})
    return out


def _attempts(d: Path) -> list[dict]:
    out = []
    for a in sorted((d / "attempts").glob("*/attempt.json"), key=lambda x: int(x.parent.name)
                    if x.parent.name.isdigit() else 0):
        try:
            data = json.loads(a.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        cfg = data.get("config") if isinstance(data.get("config"), dict) else {}
        toolenv = data.get("toolenv") if isinstance(data.get("toolenv"), dict) else None
        # JDK/Maven efetivos (toolenv); sem eles (tentativas antigas ou ignoradas), usa a configuração
        ejdk, emvn = effective_jdk_maven(toolenv)
        out.append({"n": a.parent.name, "status": data.get("status"), "skipped": data.get("skipped"),
                    "fixes": cfg.get("fixes_on") or [],
                    "jdk": ejdk if ejdk is not None else cfg.get("jdk"),
                    "maven": emvn or cfg.get("maven")})
    return out


# ------------------------------------------------------------- tabela de linhas

def _line_rows(request: Request, rel: str, run: dict, ord_: str, dir_: str, filtro: str, pag: int) -> dict:
    home = request.app.state.home
    try:
        scores = compare.run_scores(home, rel)
    except (OSError, ValueError) as e:
        return {"erro": f"Não foi possível ler as pontuações: {e}", "rows": [], "total": 0}
    if scores is None:
        return {"erro": None, "rows": [], "total": 0, "sem": True}
    wanted = {(f.cls, f.line) for f in select_lines([FaultLine(**t) for t in (run.get("truth") or [])])}
    ord_ = ord_ if ord_ in ORDER_COLS else "susp"
    desc = dir_ != "asc"
    f = filtro.strip().lower()
    items = [s for s in scores if f in s.cls.lower()] if f else list(scores)
    nulls = [s for s in items if getattr(s, ord_) is None]       # contadores ausentes ficam por último
    items = sorted((s for s in items if getattr(s, ord_) is not None),
                   key=lambda s: getattr(s, ord_), reverse=desc) + nulls
    total = len(items)
    pages = max(1, -(-total // PAGE_SIZE))
    pag = min(max(pag, 1), pages)
    chunk = items[(pag - 1) * PAGE_SIZE: pag * PAGE_SIZE]
    rows = [{"s": s, "defeito": (outer(s.cls), s.line) in wanted} for s in chunk]
    return {"erro": None, "rows": rows, "total": total, "pag": pag, "pages": pages}


def _lines_ctx(request: Request, rel: str, run: dict, ord_: str, dir_: str, filtro: str, pag: int) -> dict:
    data = _line_rows(request, rel, run, ord_, dir_, filtro, pag)
    ord_ = ord_ if ord_ in ORDER_COLS else "susp"
    dir_ = "asc" if dir_ == "asc" else "desc"
    return {"rel": rel, "ord": ord_, "dir": dir_, "filtro": filtro, "cols": ORDER_COLS, **data}


@router.get("/run/{rel:path}/linhas", response_class=HTMLResponse)
def run_lines(request: Request, rel: str, ord: str = "susp", dir: str = "desc", filtro: str = "",
              pag: int = 1):
    got = _load(request, rel)
    if got is None:
        return _no_run(request, rel)
    return _render(request, "_linhas.html", _lines_ctx(request, rel, got[0], ord, dir, filtro, pag))


# ------------------------------------------------------------ resultado no código

def _code_classes(scores) -> dict[str, dict]:
    """Por classe externa: linha -> LineScore de maior susp (internas caem na externa)."""
    by_cls: dict[str, dict[int, object]] = {}
    for s in scores:
        cur = by_cls.setdefault(outer(s.cls), {})
        if s.line not in cur or s.susp > cur[s.line].susp:
            cur[s.line] = s
    return {c: {"lines": ls, "max": max(x.susp for x in ls.values()), "n": len(ls)}
            for c, ls in by_cls.items()}


@router.get("/run/{rel:path}/codigo", response_class=HTMLResponse)
def run_code(request: Request, rel: str, classe: str = ""):
    got = _load(request, rel)
    if got is None:
        return _no_run(request, rel)
    run, d = got
    ctx: dict = {"page": "bugs", "rel": rel, "run": run, "aviso": None, "classes": [], "atual": None,
                 "rows": [], "tabela": [], "truncado": False, "membro": None, "top": None}
    scores = None
    if run.get("kind") in ("jaguar1", "jaguar2"):
        try:
            scores = compare.run_scores(request.app.state.home, rel)
        except (OSError, ValueError) as e:
            ctx["aviso"] = f"Não foi possível ler as pontuações: {e}"
    if not scores:
        ctx["aviso"] = ctx["aviso"] or "Esta execução não tem pontuações por linha para mostrar no código."
        return _render(request, "codigo.html", ctx)
    classes = _code_classes(scores)
    order = sorted(classes, key=lambda c: (-classes[c]["max"], c))
    ctx["classes"] = [{"cls": c, "max": classes[c]["max"], "n": classes[c]["n"]} for c in order]
    atual = outer(classe) if classe else order[0]
    ctx["atual"] = atual
    lines = classes.get(atual, {"lines": {}})["lines"]
    ctx["tabela"] = sorted(lines.values(), key=lambda s: (-s.susp, s.line))
    gmax = max(c["max"] for c in classes.values())
    try:
        membro, src, trunc = source.load_source(d, atual, run.get("module"))
    except source.SourceError as e:
        ctx["aviso"] = str(e)
        return _render(request, "codigo.html", ctx)
    top = ctx["tabela"][0].line if ctx["tabela"] else None
    ctx["rows"] = [{"n": i, "text": t, "s": lines.get(i),
                    "a": round(lines[i].susp / gmax, 3) if i in lines and gmax > 0 else 0}
                   for i, t in enumerate(src, 1)]
    ctx.update(membro=membro, truncado=trunc, top=top)
    return _render(request, "codigo.html", ctx)


# ------------------------------------------------------- arquivos, log, zip

@router.get("/run/{rel:path}/arquivo")
def run_file(request: Request, rel: str, p: str = ""):
    got = _load(request, rel)
    if got is None:
        return _no_run(request, rel)
    target = _inside(got[1], p)
    if target is None:
        return _msg(request, "bugs", "Caminho inválido", "O arquivo precisa estar dentro da pasta da execução.", 400)
    if not target.is_file():
        return _msg(request, "bugs", "Arquivo não encontrado", p, 404)
    return FileResponse(target, filename=target.name)


def _search_logs(d: Path, q: str) -> tuple[list[dict], bool, int]:
    """Ocorrências de `q` nos logs das tentativas, com contexto; no máximo MAX_MATCHES."""
    needle = q.lower()
    blocks, total, capped = [], 0, False
    for gz in sorted((d / "attempts").glob("*/log.txt.gz"), key=lambda x: x.parent.name):
        try:
            with gzip.open(gz, "rt", encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except (OSError, EOFError):
            continue
        for i, ln in enumerate(lines):
            if needle in ln.lower():
                if total >= MAX_MATCHES:
                    capped = True
                    break
                total += 1
                lo, hi = max(0, i - LOG_CONTEXT), min(len(lines), i + LOG_CONTEXT + 1)
                blocks.append({"attempt": gz.parent.name, "line": i + 1,
                               "ctx": [(lo + k + 1, t, lo + k == i) for k, t in enumerate(lines[lo:hi])]})
        if capped:
            break
    return blocks, capped, total


@router.get("/run/{rel:path}/log", response_class=HTMLResponse)
def run_log(request: Request, rel: str, q: str = ""):
    got = _load(request, rel)
    if got is None:
        return _no_run(request, rel)
    blocks, capped, total = _search_logs(got[1], q) if q.strip() else ([], False, 0)
    return _render(request, "_log.html", {"rel": rel, "q": q, "blocks": blocks, "capped": capped,
                                          "total": total, "max": MAX_MATCHES})


@router.get("/run/{rel:path}/zip")
def run_zip(request: Request, rel: str):
    got = _load(request, rel)
    if got is None:
        return _no_run(request, rel)
    d = got[1]
    tmp = tempfile.TemporaryFile()
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as zf:
        for f in sorted(d.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(d).as_posix())
    tmp.seek(0)

    def chunks():
        try:
            while True:
                b = tmp.read(1 << 16)
                if not b:
                    return
                yield b
        finally:
            tmp.close()

    name = rel.replace("/", "_") + ".zip"
    return StreamingResponse(chunks(), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/run/{rel:path}/abrir", response_class=HTMLResponse)
def run_open(request: Request, rel: str):
    got = _load(request, rel)
    if got is None:
        return _open_reply(request, "bugs", "Execução não encontrada", f"Não existe a execução: {rel}", 404)
    try:
        _open_path(got[1])
    except OSError as e:
        return _open_reply(request, "bugs", "Não foi possível abrir a pasta",
                           f"Não foi possível abrir a pasta: {e}", 500)
    return _open_reply(request, "bugs", "Pasta aberta", "Pasta aberta no explorador de arquivos.", 200)


# ------------------------------------------------- resultado direto na página

TOP_N = 10
_RAW_OUTPUTS = ("jaguar2.csv", "jaguar2.xml", "jaguar1.xml")


def _failures_of(run: dict, d: Path) -> list[dict]:
    """Falhas gravadas na execução; nas antigas (sem `failures`), refaz a partir dos relatórios guardados."""
    return bugview.falhas_da_execucao(run, d)


def _raw_output(d: Path) -> Path | None:
    for name in _RAW_OUTPUTS:
        if (d / "outputs" / name).is_file():
            return d / "outputs" / name
    return None


def _log_cache_vazio(d: Path) -> bool:
    for gz in (d / "outputs" / "jaguar1.log.gz", d / "attempts" / "1" / "log.txt.gz"):
        try:
            with gzip.open(gz, "rt", encoding="utf-8", errors="replace") as f:
                if re.search(r"ClassFilesCache size = 0", f.read()):
                    return True
        except (OSError, EOFError):
            continue
    return False


def _jaguar1_view(run: dict, d: Path) -> dict | None:
    """Testes e contagem refeitos a partir do XML do Jaguar 1 (o log pode ter sido cortado por um teste).

    None quando não é Jaguar 1 ou o XML não está guardado; nas execuções antigas reavalia o que foi gravado."""
    xml = d / "outputs" / "jaguar1.xml"
    if run.get("kind") != "jaguar1" or not xml.is_file():
        return None
    try:
        counts = jaguar1_counts(xml)
    except (ValueError, OSError, ET.ParseError):
        return None
    if counts is None:     # XML sem linhas: nada a reconciliar
        return None
    log = d / "outputs" / "jaguar1.log.gz"
    try:
        logged = parse_jaguar1_log(log) if log.is_file() else {}
    except (OSError, EOFError):
        logged = {}
    known = [t for t in (run.get("relevant_tests") or []) if "#" in t]
    tests, tcounts, truncated = reconcile_jaguar1(logged, counts, known)
    return {"tests": tests, "counts": tcounts, "truncated": truncated}


def _pos_texto(best: int, worst: int, tied: int, total: int) -> str:
    """"posição 5 de 4984" sem empate; "posições 1–137 de 4984 (137 empatadas)" com empate."""
    if tied <= 1:
        return f"posição {best} de {total}"
    return f"posições {best}–{worst} de {total} ({tied} empatadas)"


def _truth_view(run: dict, scores) -> dict | None:
    """Linhas do defeito (pelo patch) e, havendo ranking, a faixa de posições de cada uma (empates incluídos)."""
    faults = select_lines([FaultLine(**t) for t in (run.get("truth") or [])])
    if not faults:
        return None
    total = len(scores) if scores else None
    pos: dict[tuple[str, int], dict] = {}
    if scores:
        for r in compare.fault_positions(scores, faults)["lines"]:
            key = (outer(r["cls"]), r["line"])
            if key not in pos or r["rank_best"] < pos[key]["rank_best"]:   # classe interna: vale a mais suspeita
                pos[key] = r
    rows = []
    for f in dict.fromkeys(faults):
        r = pos.get((f.cls, f.line))
        row = {"cls": f.cls, "short": f.cls.rsplit(".", 1)[-1], "line": f.line, "best": None}
        if r:
            row.update(best=r["rank_best"], worst=r["rank_worst"], tied=r["tied"],
                       expected=f"{r['expected']:g}", pos_text=_pos_texto(r["rank_best"], r["rank_worst"],
                                                                         r["tied"], total))
        rows.append(row)
    rows.sort(key=lambda r: (r["best"] is None, r["best"] or 0, r["cls"], r["line"]))
    best_text = None
    ranked = [r for r in rows if r["best"] is not None]
    if ranked:
        b = min(ranked, key=lambda r: r["best"])
        if b["tied"] > 1:
            best_text = (f"melhor linha do defeito: posições {b['best']}–{b['worst']} (empate com {b['tied']} linhas)"
                         f"; posição esperada {b['expected']}")
        else:
            best_text = f"melhor linha do defeito: posição {b['best']} de {total}"
    return {"rows": rows, "total": total, "best_text": best_text, "ranked": scores is not None, "n": len(rows)}


def _result_ctx(request: Request, rel: str, run: dict, d: Path, counts, view: dict | None = None) -> dict:
    """Seção "Resultado": status em palavras, falhas com o provável erro, 10 linhas mais suspeitas e avisos."""
    kind = run.get("kind") or ""
    jaguar = kind in ("jaguar1", "jaguar2")
    status = S.effective(run.get("status") or "", counts)
    diag = run.get("diagnosis") or {}
    if status == S.SEM_TESTE_FALHANDO:
        cause = diag.get("cause") or S.CAUSA_SEM_TESTE_FALHANDO
    else:     # reavaliado: a causa gravada ("nenhum teste falhou") não vale mais
        cause = diag.get("cause") or "" if status == run.get("status") else ""
    warnings = list(dict.fromkeys([*(run.get("warnings") or []), *(diag.get("warnings") or [])]))
    if view and view["truncated"] and AVISO_LOG_INTERROMPIDO not in warnings:
        warnings.append(AVISO_LOG_INTERROMPIDO)
    top, scores, top_nota = [], None, None
    if jaguar:
        try:
            scores = compare.run_scores(request.app.state.home, rel)
        except (OSError, ValueError):
            scores = None
        if scores is not None:
            wanted = {(f.cls, f.line) for f in select_lines([FaultLine(**t) for t in (run.get("truth") or [])])}
            for c, ln, sc, rank in ranking_rows(scores)[:TOP_N]:
                top.append({"cls": c, "line": ln, "susp": sc, "rank": rank, "defeito": (outer(c), ln) in wanted})
            if top:     # a última do top 10 empatada com mais linhas do que as mostradas: a ordem é arbitrária
                last = top[-1]["susp"]
                empatadas = sum(1 for s in scores if s.susp == last)
                if empatadas > sum(1 for t in top if t["susp"] == last):
                    top_nota = (f"{empatadas} linhas empatadas com pontuação {last:.4f}; "
                                "a ordem entre elas é arbitrária")
            if not scores and SEM_INSTRUMENTACAO not in warnings:
                warnings.append(SEM_INSTRUMENTACAO)
        if kind == "jaguar1" and SEM_INSTRUMENTACAO not in warnings and _log_cache_vazio(d):
            warnings.append(SEM_INSTRUMENTACAO)
    raw = _raw_output(d)
    failures = _failures_of(run, d)
    if view and not failures:     # execução antiga: as falhas só existem no XML/log
        failures = [{"test": t, "message": None, "frame": None, "frame_text": ""}
                    for t, v in sorted(view["tests"].items()) if v in ("failed", "error")]
    truth = _truth_view(run, scores) if (scores is not None or not jaguar) else None
    return {"res_status": status, "res_label": S.label(status), "res_tom": S.tom(status), "res_cause": cause,
            "res_ok": S.is_success(status), "res_failures": failures, "res_truth": truth,
            "res_top": top, "res_top_nota": top_nota, "res_warnings": warnings, "res_scores": scores is not None,
            "res_ranking": jaguar and bool(scores), "res_raw": raw.name if raw else None}


@router.get("/run/{rel:path}/ranking.csv")
def run_ranking_csv(request: Request, rel: str):
    got = _load(request, rel)
    if got is None:
        return _no_run(request, rel)
    try:
        scores = compare.run_scores(request.app.state.home, rel)
    except (OSError, ValueError) as e:
        return _msg(request, "bugs", "Ranking indisponível", f"Não foi possível ler as pontuações: {e}", 400)
    if not scores:
        return _msg(request, "bugs", "Ranking indisponível", "Esta execução não tem ranking de linhas.", 404)
    name = rel.replace("/", "_") + "-ranking.csv"
    return Response(ranking_csv(scores), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/run/{rel:path}/espectro")
def run_spectrum(request: Request, rel: str):
    got = _load(request, rel)
    if got is None:
        return _no_run(request, rel)
    raw = _raw_output(got[1])
    if raw is None:
        return _msg(request, "bugs", "Saída indisponível", "Esta execução não tem saída do Jaguar para baixar.", 404)
    return FileResponse(raw, filename=raw.name)


# --------------------------------------------------- tentar de novo / receita

def _detected_fixes(d: Path | None, run: dict) -> list[str] | None:
    """Correções detectadas do POM na última tentativa real (attempt.json → extra.pom_plan.detected)."""
    if d is None:
        return None
    n = len(run.get("attempt_configs") or []) or int(run.get("attempts") or 0)
    for k in range(n, 0, -1):
        try:
            data = json.loads((d / "attempts" / str(k) / "attempt.json").read_text(encoding="utf-8"))
            plan = (data.get("extra") or {}).get("pom_plan")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(plan, dict) and isinstance(plan.get("detected"), list):
            return [str(x) for x in plan["detected"]]
    return None


def _retry_opts(run: dict, rel: str, sug: dict, d: Path | None = None) -> dict:
    cfg = _last_cfg(run)
    fixes_on = list(cfg["fixes_on"])
    add: list[str] = []
    fix = sug.get("fix")
    if not fixes_on and fix and run.get("kind") == "jaguar2":
        # fixes_on vazio significa "detectadas": parte delas, ou os acrescentaria só a sugestão
        detected = _detected_fixes(d, run)
        if detected is None:
            add = [fix]               # sem registro: o execute soma a sugestão às detectadas
        else:
            fixes_on = [f for f in detected if f not in cfg["fixes_off"]]
    params = {k: v for k, v in (cfg["params"] or {}).items()}
    jdk, maven, scope = cfg["jdk"], cfg["maven"], cfg["test_scope"]
    sparams = dict(sug.get("params") or {})
    if fix:
        if fix not in fixes_on and not add:
            fixes_on.append(fix)
        if sparams:
            params[fix] = sparams
    elif sparams.get("test_scope") in ("relevant", "all"):
        scope = sparams["test_scope"]
    if sug.get("jdk") is not None:
        jdk = sug["jdk"]
    if sug.get("maven"):
        maven = sug["maven"]
    opts = {"test_scope": scope, "auto_search": False, "fixes_on": fixes_on,
            "fixes_off": [f for f in cfg["fixes_off"] if f != fix], "params": params,
            "jdk": jdk, "maven": maven, "parent": rel}
    if add:
        opts["fixes_add"] = add
    return opts


@router.post("/run/{rel:path}/tentar")
def run_retry(request: Request, rel: str, idx: int = Form(...), confirmar: str = Form("")):
    got = _load(request, rel)
    if got is None:
        return _no_run(request, rel)
    run = got[0]
    sugs = applicable(run.get("kind") or "", (run.get("diagnosis") or {}).get("suggestions") or [])
    if not 0 <= idx < len(sugs):
        return _msg(request, "bugs", "Sugestão inválida", "Essa sugestão não existe nesta execução.", 400)
    sug = sugs[idx]
    if sug.get("fix") == "exclude-duplicates" and not confirmar:
        return _render(request, "confirmar_dup.html", {
            "page": "bugs", "rel": rel, "idx": idx, "files": (sug.get("params") or {}).get("files") or []})
    bug = _find_bug(request, run.get("bug") or "")
    if bug is None:
        return _msg(request, "bugs", "Bug não encontrado", f"Não foi possível reconstruir o bug {run.get('bug')}.", 404)
    st = request.app.state
    bid = jobs_queue.create_batch(st.home, f"nova tentativa de {rel}", [bug], [run["kind"]],
                                  _retry_opts(run, rel, sug, got[1]))
    job = jobs_queue.batch_jobs(st.home, bid)[0]
    return RedirectResponse(f"/job/{job['id']}/ao-vivo", status_code=303)


@router.post("/run/{rel:path}/receita")
def run_recipe(request: Request, rel: str, scope: str = Form("bug"), glob: str = Form(""),
               note: str = Form("")):
    got = _load(request, rel)
    if got is None:
        return _no_run(request, rel)
    run = got[0]
    if not _can_save_recipe(run):
        return _msg(request, "bugs", "Receita indisponível",
                    "Só dá para salvar uma receita de uma execução bem-sucedida que precisou de correções.", 400)
    cfg = _last_cfg(run)
    acfg = AttemptConfig(frozenset(cfg["fixes_on"]), frozenset(cfg["fixes_off"]), cfg["params"],
                         cfg["jdk"], cfg["maven"], cfg["test_scope"])
    bug = Bug(run["benchmark"], run["project"], run["bug_id"])
    try:
        recipes.save(request.app.state.home, bug, run["kind"], "project" if scope == "projeto" else scope,
                     acfg, glob.strip() or None, note.strip())
    except ValueError as e:
        return _msg(request, "bugs", "Não foi possível salvar a receita", str(e), 400)
    return RedirectResponse("/receitas", status_code=303)


# --------------------------------------------------------------- página da run

@router.get("/run/{rel:path}", response_class=HTMLResponse)
def run_page(request: Request, rel: str):
    got = _load(request, rel)
    if got is None:
        return _no_run(request, rel)
    run, d = got
    home = request.app.state.home
    try:
        results = compare.run_tests(home, rel)
    except (OSError, ValueError):
        results = {}
    counts = run.get("tests") or (test_counts(results) if results else None)
    view = _jaguar1_view(run, d)
    if view:     # o XML é a fonte confiável da contagem; o log pode ter parado no meio
        results, counts = view["tests"], view["counts"]
    failing = sorted(t for t, s in results.items() if s in ("failed", "error"))
    diag = dict(run.get("diagnosis") or {})
    if diag.get("suggestions"):
        diag["suggestions"] = applicable(run.get("kind") or "", diag["suggestions"])
    files = _files(d)
    result = _result_ctx(request, rel, run, d, counts, view)
    if result["res_status"] != run.get("status"):     # status reavaliado: a causa gravada não vale mais
        diag["cause"] = result["res_cause"]
    ctx = {"page": "bugs", "rel": rel, "run": run, "diag": diag, "counts": counts,
           "failing": failing, "files": files[:MAX_FILES], "more_files": max(0, len(files) - MAX_FILES),
           "attempts": _attempts(d), "kind_label": KIND_LABEL,
           "can_recipe": _can_save_recipe(run), "cfg": _last_cfg(run),
           "has_scores": run.get("kind") in ("jaguar1", "jaguar2"),
           **result,
           **_lines_ctx(request, rel, run, "susp", "desc", "", 1)}
    return _render(request, "execucao.html", ctx)


# ----------------------------------------------------------------- comparar

def _cap(items: list, n: int = LIST_CAP) -> tuple[list, int]:
    return items[:n], max(0, len(items) - n)


@router.get("/comparar", response_class=HTMLResponse)
def comparar(request: Request, a: str = "", b: str = ""):
    home = request.app.state.home
    ctx: dict = {"page": "comparar", "runs": store.list_runs(home)[:MAX_SELECT], "a": a, "b": b, "res": None,
                 "erro": None}
    status = 200
    if a and b:
        ra, rb = _load(request, a), _load(request, b)
        if ra is None or rb is None:
            ctx["erro"] = "Uma das execuções escolhidas não existe."
            status = 404
        else:
            try:
                res = compare.compare_runs(home, a, b)
                t = res["tests"]
                ctx["res"] = res
                ctx["limit"] = LIST_CAP
                ctx["kinds"] = (ra[0]["kind"], rb[0]["kind"])
                ctx["t_only_a"] = _cap(t["only_a"])
                ctx["t_only_b"] = _cap(t["only_b"])
                ctx["t_diff"] = _cap(t["outcome_differs"])
                if res["spectra"]:
                    s = res["spectra"]
                    ctx["s_only_a"] = _cap(s["only_a"])
                    ctx["s_only_b"] = _cap(s["only_b"])
                    ctx["s_diffs"] = _cap(s["diffs"])
            except (OSError, ValueError) as e:
                ctx["erro"] = f"Não foi possível comparar: {e}"
                status = 400
    return _render(request, "comparar.html", ctx, status)


# -------------------------------------------------------------------- lotes

def _parse_keys(text: str) -> list[str]:
    return [k.strip() for k in text.replace("\r", "\n").replace(",", "\n").split("\n") if k.strip()]


def _lotes_ctx(request: Request, **kw) -> dict:
    ctx = {"page": "lotes", "bugs": "", "kinds": KINDS, "erro": None,
           "lotes": jobs_queue.list_batches(request.app.state.home)}
    ctx.update(kw)
    return ctx


@router.get("/lotes", response_class=HTMLResponse)
def lotes(request: Request, bugs: str = ""):
    return _render(request, "lotes.html", _lotes_ctx(request, bugs="\n".join(_parse_keys(bugs))))


@router.post("/lotes")
def lotes_criar(request: Request, bugs: str = Form(""), kinds: list[str] = Form([]), scope: str = Form("relevant"),
                auto_search: str = Form(""), archive_checkout: str = Form(""), name: str = Form("")):
    keys = _parse_keys(bugs)
    ks = [k for k in kinds if k in KIND_LABEL]
    err = None
    if not keys:
        err = "Informe pelo menos um bug."
    elif not ks:
        err = "Escolha pelo menos um tipo de execução."
    found: list[Bug] = []
    if not err:
        for k in keys:
            bug = _find_bug(request, k)
            if bug is None:
                err = f"Bug não encontrado: {k}"
                break
            found.append(bug)
    if err:
        return _render(request, "lotes.html", _lotes_ctx(request, bugs=bugs, erro=err), 400)
    opts = {"test_scope": scope if scope in ("relevant", "all") else "relevant",
            "auto_search": bool(auto_search), "archive_checkout": bool(archive_checkout)}
    bid = jobs_queue.create_batch(request.app.state.home, name.strip() or f"{len(found)} bugs", found, ks, opts)
    return RedirectResponse(f"/lote/{bid}", status_code=303)


def _batch_ctx(request: Request, bid: int) -> dict | None:
    try:
        st = jobs_queue.batch_status(request.app.state.home, bid)
    except ValueError:
        return None
    return {"b": st, "kind_label": KIND_LABEL}


@router.get("/lote/{bid}/progresso", response_class=HTMLResponse)
def lote_progresso(request: Request, bid: int):
    ctx = _batch_ctx(request, bid)
    if ctx is None:
        return _msg(request, "lotes", "Lote não encontrado", f"Não existe o lote {bid}.", 404)
    return _render(request, "_lote_progresso.html", ctx)


@router.get("/lote/{bid}/csv")
def lote_csv(request: Request, bid: int):
    ctx = _batch_ctx(request, bid)
    if ctx is None:
        return _msg(request, "lotes", "Lote não encontrado", f"Não existe o lote {bid}.", 404)
    home = request.app.state.home
    paths = {j["run_rel"] for j in ctx["b"]["jobs"] if j["run_rel"]}
    runs = [r for r in store.list_runs(home) if r["path"] in paths]
    text = compare.summary_csv(compare.batch_summary(home, runs))
    return Response(text, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="lote-{bid}.csv"'})


@router.post("/lote/{bid}/{acao}")
def lote_acao(request: Request, bid: int, acao: str):
    if _batch_ctx(request, bid) is None:
        return _msg(request, "lotes", "Lote não encontrado", f"Não existe o lote {bid}.", 404)
    worker = request.app.state.worker
    fn = {"pausar": worker.pause, "retomar": worker.resume, "cancelar": worker.cancel}.get(acao)
    if fn is None:
        return _msg(request, "lotes", "Ação desconhecida", acao, 400)
    fn(bid)
    return RedirectResponse(f"/lote/{bid}", status_code=303)


@router.get("/lote/{bid}", response_class=HTMLResponse)
def lote_page(request: Request, bid: int):
    ctx = _batch_ctx(request, bid)
    if ctx is None:
        return _msg(request, "lotes", "Lote não encontrado", f"Não existe o lote {bid}.", 404)
    return _render(request, "lote.html", {"page": "lotes", **ctx})


# ----------------------------------------------------------------- receitas

def _recipe_files(home) -> list[str]:
    if not home.recipes.is_dir():
        return []
    return sorted(f.relative_to(home.recipes).as_posix() for f in home.recipes.rglob("*.toml"))


def _recipe_ctx(request: Request, f: str = "", texto: str | None = None, erro: str | None = None,
                ok: bool = False) -> dict:
    home = request.app.state.home
    files = _recipe_files(home)
    if texto is None and f in files:
        texto = (home.recipes / f).read_text(encoding="utf-8")
    return {"page": "receitas", "files": files, "f": f, "texto": texto or "", "erro": erro, "ok": ok}


@router.get("/receitas", response_class=HTMLResponse)
def receitas(request: Request, f: str = ""):
    return _render(request, "receitas.html", _recipe_ctx(request, f))


@router.post("/receitas", response_class=HTMLResponse)
def receitas_salvar(request: Request, f: str = Form(""), texto: str = Form("")):
    home = request.app.state.home
    if f not in _recipe_files(home) or _inside(home.recipes, f) is None:
        return _render(request, "receitas.html",
                       _recipe_ctx(request, "", texto, "Arquivo de receitas inválido."), 400)
    texto = texto.replace("\r\n", "\n").replace("\r", "\n")
    try:
        tomllib.loads(texto)
    except tomllib.TOMLDecodeError as e:
        return _render(request, "receitas.html", _recipe_ctx(request, f, texto, f"TOML inválido: {e}"), 400)
    target = home.recipes / f
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(texto, encoding="utf-8", newline="\n")
    os.replace(tmp, target)
    return _render(request, "receitas.html", _recipe_ctx(request, f, texto, None, True))


# ----------------------------------------------------------------- exportar

def _export_ctx(request: Request, **kw) -> dict:
    ctx = {"page": "exportar", "lotes": jobs_queue.list_batches(request.app.state.home), "erro": None,
           "feito": None, "nome": "", "bugs": "", "lote": ""}
    ctx.update(kw)
    return ctx


@router.get("/exportar", response_class=HTMLResponse)
def exportar(request: Request):
    return _render(request, "exportar.html", _export_ctx(request))


@router.post("/exportar", response_class=HTMLResponse)
def exportar_post(request: Request, nome: str = Form(""), lote: str = Form(""), bugs: str = Form("")):
    home = request.app.state.home
    sel, batch_id, err = None, None, None
    if bugs.strip():
        sel = []
        for k in _parse_keys(bugs):
            bug = _find_bug(request, k)
            if bug is None:
                err = f"Bug não encontrado: {k}"
                break
            sel.append(bug)
    elif lote.strip():
        try:
            batch_id = int(lote)
            jobs_queue.batch_status(home, batch_id)
        except ValueError:
            err = f"Lote inválido: {lote}"
    if not err:
        try:
            dest = dataset.export(home, nome, sel, batch_id)
            return _render(request, "exportar.html", _export_ctx(request, feito=dest.name, caminho=str(dest)))
        except ValueError as e:
            err = str(e)
    return _render(request, "exportar.html",
                   _export_ctx(request, erro=err, nome=nome, bugs=bugs, lote=lote), 400)


@router.post("/exportar/abrir", response_class=HTMLResponse)
def exportar_abrir(request: Request, nome: str = Form("")):
    home = request.app.state.home
    d = _inside(home.datasets, nome) if nome else None
    if d is None or not d.is_dir():
        return _open_reply(request, "exportar", "Dataset não encontrado", f"Dataset não encontrado: {nome}", 404)
    try:
        _open_path(d)
    except OSError as e:
        return _open_reply(request, "exportar", "Não foi possível abrir a pasta",
                           f"Não foi possível abrir a pasta: {e}", 500)
    return _open_reply(request, "exportar", "Pasta aberta", "Pasta aberta no explorador de arquivos.", 200)
