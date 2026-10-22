"""Tela Executar: execução rápida de um projeto (pasta local ou URL@commit) em poucos cliques."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import compare, store
from .. import queue as jobs_queue
from .. import status as S
from ..diagnose import applicable
from ..prepare import local_bug
from .routes_bugs import KIND_LABEL, KINDS

router = APIRouter()

MAX_RECENTES = 10
TOP_LINHAS = 10
POLL_SECONDS = 2


def _render(request: Request, name: str, ctx: dict, status: int = 200):
    return request.app.state.templates.TemplateResponse(request, name, ctx, status_code=status)


# ----------------------------------------------------------------- recentes

def _recentes_path(home) -> Path:
    return Path(home.root) / "recentes.json"


def load_recentes(home) -> list[dict]:
    """Projetos usados por último (mais recente primeiro); arquivo ausente ou inválido = lista vazia."""
    try:
        data = json.loads(_recentes_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for r in data:
        if isinstance(r, dict) and isinstance(r.get("spec"), str) and r["spec"].strip():
            out.append({"spec": r["spec"], "nome": str(r.get("nome") or r["spec"]),
                        "data": str(r.get("data") or ""),
                        "kinds": [k for k in (r.get("kinds") or []) if k in KIND_LABEL]
                        if isinstance(r.get("kinds"), list) else [],
                        "tests": [str(t) for t in r["tests"]] if isinstance(r.get("tests"), list) else [],
                        "auto_search": bool(r.get("auto_search", True)),
                        "module": r["module"].strip() if isinstance(r.get("module"), str) else ""})
    return out[:MAX_RECENTES]


def add_recente(home, spec: str, nome: str, kinds: list[str], tests: list[str], auto_search: bool,
                module: str = "") -> None:
    """Põe o projeto no topo dos recentes (sem duplicar o mesmo spec+módulo) e mantém no máximo 10."""
    entry = {"spec": spec, "nome": nome, "data": datetime.now().isoformat(timespec="seconds"),
             "kinds": list(kinds), "tests": list(tests), "auto_search": bool(auto_search), "module": module}
    lst = [entry] + [r for r in load_recentes(home) if (r["spec"], r["module"]) != (spec, module)]
    path = _recentes_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(lst[:MAX_RECENTES], ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def parse_tests(text: str) -> list[str]:
    """`Classe` / `Classe#metodo` separados por vírgula ou quebra de linha."""
    return [t.strip() for t in text.replace("\r", "\n").replace(",", "\n").split("\n") if t.strip()]


def _folder_missing(spec: str) -> bool:
    """Spec de pasta (sem `@`) cuja pasta não existe mais."""
    return "@" not in spec and not Path(spec).expanduser().is_dir()


# ---------------------------------------------------------------- formulário

def _tk_available() -> bool:
    try:
        import tkinter  # noqa: F401
    except ImportError:
        return False
    return True


def _pick_folder() -> str:
    """Seletor de pastas nativo, numa thread própria e com a janela no topo. "" se cancelar ou falhar."""
    out = [""]

    def work() -> None:
        try:
            import tkinter
            from tkinter import filedialog
            root = tkinter.Tk()
        except Exception:        # sem tkinter ou sem tela
            return
        try:
            root.withdraw()
            root.attributes("-topmost", True)
            out[0] = filedialog.askdirectory(parent=root, title="Escolha a pasta do projeto") or ""
        except Exception:
            out[0] = ""
        finally:
            root.destroy()

    t = threading.Thread(target=work, name="jflp-folder-picker")
    t.start()
    t.join()
    return out[0]


def _form_ctx(request: Request, **kw) -> dict:
    ctx = {"page": "executar", "kinds": KINDS, "projeto": "", "sel": ["jaguar2"], "tests_mode": "all",
           "tests": "", "auto_search": True, "erro": None, "module": "", "tk": _tk_available(),
           "recentes": [dict(r, faltando=_folder_missing(r["spec"]),
                             nomes=[KIND_LABEL[k] for k in r["kinds"]])
                        for r in load_recentes(request.app.state.home)]}
    ctx.update(kw)
    return ctx


def _start(request: Request, spec: str, kinds: list[str], tests: list[str], auto: bool, module: str = ""):
    """Cria o lote do projeto. Devolve (id do lote, None) ou (None, mensagem de erro em português)."""
    try:
        bug = local_bug(spec.strip().strip('"'), module)
    except ValueError as e:
        return None, str(e)
    st = request.app.state
    st.local_bugs[bug.key] = bug
    opts: dict = {"test_scope": "relevant" if tests else "all", "auto_search": auto}
    if tests:
        opts["tests"] = tests
    bid = jobs_queue.create_batch(st.home, f"executar {bug.project}", [bug], kinds, opts)
    add_recente(st.home, spec.strip().strip('"'), bug.project, kinds, tests, auto, bug.extra.get("module", ""))
    return bid, None


@router.get("/executar", response_class=HTMLResponse)
def executar(request: Request, usar: int = -1):
    kw: dict = {}
    recentes = load_recentes(request.app.state.home)
    if 0 <= usar < len(recentes):         # "Usar": preenche o formulário com o recente
        r = recentes[usar]
        kw = {"projeto": r["spec"], "module": r["module"], "sel": r["kinds"] or ["jaguar2"], "tests": "\n".join(r["tests"]),
              "tests_mode": "only" if r["tests"] else "all", "auto_search": r["auto_search"]}
    return _render(request, "executar.html", _form_ctx(request, **kw))


@router.post("/executar")
def executar_post(request: Request, projeto: str = Form(""), kinds: list[str] = Form([]),
                  tests_mode: str = Form("all"), tests: str = Form(""), auto_search: str = Form(""),
                  module: str = Form("")):
    ks = [k for k, _ in KINDS if k in kinds]
    lista = parse_tests(tests) if tests_mode == "only" else []
    err = None
    if not projeto.strip():
        err = "Informe o projeto: uma pasta do computador ou URL@commit."
    elif not ks:
        err = "Escolha pelo menos um tipo de execução."
    elif tests_mode == "only" and not lista:
        err = "Informe pelo menos um teste (Classe ou Classe#metodo) ou escolha rodar todos."
    bid = None
    if not err:
        bid, err = _start(request, projeto, ks, lista, bool(auto_search), module)
    if err:
        return _render(request, "executar.html", _form_ctx(
            request, projeto=projeto, sel=ks, tests_mode=tests_mode, tests=tests,
            auto_search=bool(auto_search), module=module, erro=err), 400)
    return RedirectResponse(f"/executar/{bid}", status_code=303)


@router.post("/executar/escolher", response_class=HTMLResponse)
def executar_escolher(projeto: str = Form("")):
    if not _tk_available():
        return HTMLResponse('<span class="erro">O seletor de pastas não está disponível neste computador.</span>',
                            status_code=404)
    chosen = _pick_folder() or projeto       # cancelar mantém o que estava digitado
    return HTMLResponse(f'<input type="text" id="projeto" name="projeto" value="{escape(chosen, quote=True)}" '
                        'placeholder="C:\\caminho\\do\\projeto  ou  https://.../repo.git@commit" '
                        'autocomplete="off">')


@router.post("/executar/recente/{i}")
def executar_recente(request: Request, i: int):
    recentes = load_recentes(request.app.state.home)
    if not 0 <= i < len(recentes):
        return _render(request, "mensagem.html", {"page": "executar", "titulo": "Recente não encontrado",
                                                  "texto": "Esse projeto não está mais na lista."}, 404)
    r = recentes[i]
    if _folder_missing(r["spec"]):
        return _render(request, "executar.html", _form_ctx(
            request, projeto=r["spec"], module=r["module"], erro=f"Pasta não encontrada: {r['spec']}"), 400)
    bid, err = _start(request, r["spec"], r["kinds"] or ["jaguar2"], r["tests"], r["auto_search"], r["module"])
    if err:
        return _render(request, "executar.html", _form_ctx(request, projeto=r["spec"], module=r["module"],
                                                           erro=err), 400)
    return RedirectResponse(f"/executar/{bid}", status_code=303)


# ------------------------------------------------------------------ cartões

def _top_linhas(home, rel: str) -> list[dict]:
    try:
        scores = compare.run_scores(home, rel)
    except (OSError, ValueError):
        return []
    top = sorted(scores or [], key=lambda s: s.susp, reverse=True)[:TOP_LINHAS]
    return [{"cls": s.cls, "line": s.line, "susp": s.susp,
             "href": f"/run/{rel}/codigo?classe={quote(s.cls, safe='.')}#L{s.line}"} for s in top]


def _cartao(request: Request, job: dict) -> dict:
    home = request.app.state.home
    c = {"job": job, "kind": job["kind"], "nome": KIND_LABEL.get(job["kind"], job["kind"]),
         "state": job["state"], "rel": job.get("run_rel"), "run": None, "erro": job.get("error")}
    if job["state"] != "done" or not c["rel"]:
        return c
    try:
        run = store.load_run(home, c["rel"])
    except (OSError, ValueError, KeyError) as e:
        c["erro"] = c["erro"] or f"Não foi possível ler a execução: {e}"
        return c
    c["run"] = run
    c["ok"] = S.is_success(run.get("status") or "")
    diag = dict(run.get("diagnosis") or {})
    c["suggestions"] = applicable(run.get("kind") or "", diag.get("suggestions") or [])
    c["cause"] = diag.get("cause") or ""
    c["tests"] = run.get("tests")
    if job["kind"] in ("jaguar1", "jaguar2"):
        c["linhas"] = _top_linhas(home, c["rel"])
    return c


def _cartoes_ctx(request: Request, bid: int) -> dict | None:
    home = request.app.state.home
    jobs = jobs_queue.batch_jobs(home, bid)
    if not jobs:
        return None
    cards = [_cartao(request, j) for j in jobs]
    done = {c["kind"]: c["rel"] for c in cards if c["run"] is not None}
    comparar = None
    if "maven" in done and "jaguar2" in done:
        comparar = f"/comparar?a={quote(done['maven'], safe='/')}&b={quote(done['jaguar2'], safe='/')}"
    return {"bid": bid, "cards": cards, "comparar": comparar, "poll": POLL_SECONDS,
            "pendente": any(j["state"] in ("pending", "running") for j in jobs)}


def _sem_lote(request: Request, bid: int):
    return _render(request, "mensagem.html", {"page": "executar", "titulo": "Execução não encontrada",
                                              "texto": f"Não existe a execução rápida {bid}."}, 404)


@router.get("/executar/{bid}/cartoes", response_class=HTMLResponse)
def executar_cartoes(request: Request, bid: int):
    ctx = _cartoes_ctx(request, bid)
    if ctx is None:
        return _sem_lote(request, bid)
    return _render(request, "_executar_cartoes.html", ctx)


@router.get("/executar/{bid}", response_class=HTMLResponse)
def executar_lote(request: Request, bid: int):
    ctx = _cartoes_ctx(request, bid)
    if ctx is None:
        return _sem_lote(request, bid)
    bug_nome, modulo = "", ""
    try:       # nome do projeto (e módulo) para o título
        job = jobs_queue.batch_jobs(request.app.state.home, bid)[0]
        bug_nome = job["bug_key"].split("/")[1]
        modulo = job["extra"].get("module", "")
    except (IndexError, KeyError):
        pass
    return _render(request, "executar_resultado.html", {"page": "executar", "projeto_nome": bug_nome,
                                                        "modulo": modulo, **ctx})
