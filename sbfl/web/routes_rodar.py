"""Tela Rodar (página inicial): escolher a pasta e rodar bugs em quatro passos, tudo na mesma página (htmx).

1. Pasta  2. Projeto  3. Bugs (ou módulo)  4. Ferramentas e opções  ->  um lote e a tela de progresso."""
from __future__ import annotations

from html import escape
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import queue as jobs_queue
from .. import status as S
from .. import store
from ..adapters.base import Bug
from ..prepare import (bugsjar_label, bugsjar_repo_bugs, find_bugsjar_repo, find_poms, is_bugsjar_collection,
                       is_bugsjar_repo, local_bug)
from .routes_bugs import KINDS, kinds_validos
from .routes_quick import (_pick_folder, _ramo, _tk_available, add_recente, is_bugsjar_repo_ou_ramo, load_recentes)

router = APIRouter()

PADRAO = ("jaguar1", "maven")      # o que a pesquisa mais usa


def _render(request: Request, name: str, ctx: dict, status: int = 200):
    return request.app.state.templates.TemplateResponse(request, name, ctx, status_code=status)


def _limpa(texto: str) -> str:
    return (texto or "").strip().strip('"')


def _recentes(request: Request) -> list[dict]:
    """Pastas recentes que ainda existem (URL@commit fica de fora: esta tela só trabalha com pastas)."""
    vistos: list[str] = []
    for r in load_recentes(request.app.state.home):
        if "@" not in r["spec"] and Path(r["spec"]).expanduser().is_dir() and r["spec"] not in vistos:
            vistos.append(r["spec"])
    return [{"spec": s, "nome": Path(s).name or s} for s in vistos[:6]]


def _repo_valido(repo: str) -> Path | None:
    r = Path(_limpa(repo)).expanduser()
    return r if r.is_dir() and (r / ".git").exists() and is_bugsjar_repo_ou_ramo(r) else None


def _projetos(raiz: Path) -> list[Path]:
    try:
        subs = sorted(x for x in raiz.iterdir() if x.is_dir() and (x / ".git").exists())
    except OSError:
        return []
    return [x for x in subs if is_bugsjar_repo(x) or is_bugsjar_repo_ou_ramo(x)]


def _ultimo_status(home, repo: Path) -> dict[str, dict]:
    """{ramo: {"label", "tom"}} do resultado mais recente de cada bug do repositório (uma consulta só)."""
    out: dict[str, dict] = {}
    for r in store.list_runs(home, "bugsjar", repo.name):       # mais recentes primeiro
        if r["bug"] not in out:
            out[r["bug"]] = {"label": S.label(r["status"]), "tom": S.tom(r["status"])}
    return out


def _ctx_form(request: Request, repo: Path) -> dict:
    ult = _ultimo_status(request.app.state.home, repo)
    atual = _ramo(repo)
    bugs = [{"branch": b.bug_id, "id": bugsjar_label(b.bug_id), "ult": ult.get(b.bug_id),
             "marcado": b.bug_id == atual} for b in bugsjar_repo_bugs(repo)]
    return {"modo": "bugsjar", "repo": str(repo), "nome": repo.name, "bugs": bugs, "kinds": KINDS, "sel": PADRAO}


def _fluxo(request: Request, pasta: str) -> tuple[str, dict]:
    """Detecta o que há na pasta: devolve (template do fluxo, contexto)."""
    p = Path(_limpa(pasta)).expanduser()
    base: dict = {"pasta": _limpa(pasta), "erro": None, "colecao": ""}
    if not _limpa(pasta) or not p.is_dir():
        return "_rodar_erro.html", {**base, "erro": "Essa pasta não existe neste computador. "
                                    "Escolha a pasta do repositório do Bugs.jar ou de um projeto Maven."}
    p = p.resolve()
    base["pasta"] = str(p)
    st = request.app.state
    if is_bugsjar_collection(p):
        mudou = str(st.settings.get("bugsjar", "root") or "") != str(p)
        if mudou:
            st.settings.set("bugsjar", "root", str(p))
            st.settings.save()
        projetos = [{"nome": x.name, "caminho": str(x)} for x in _projetos(p)]
        return "_rodar_projetos.html", {**base, "projetos": projetos, "salvo": mudou}
    repo = find_bugsjar_repo(p) or (p if _repo_valido(str(p)) else None)
    if repo is not None:
        return "_rodar_projeto.html", {**base, **_ctx_form(request, repo)}
    mods = list(find_poms(p))
    if (p / "pom.xml").is_file() or mods:
        return "_rodar_projeto.html", {**base, "modo": "maven", "nome": p.name, "modulos": mods,
                                       "raiz_pom": (p / "pom.xml").is_file(), "kinds": KINDS, "sel": PADRAO}
    return "_rodar_erro.html", {**base, "erro": "Não encontrei um repositório do Bugs.jar nem um projeto Maven "
                                "(pom.xml) nessa pasta."}


@router.get("/rodar", response_class=HTMLResponse)
def rodar_pagina(request: Request, pasta: str = ""):
    st = request.app.state
    pasta = _limpa(pasta) or str(st.settings.get("bugsjar", "root") or "")
    ctx = {"page": "rodar", "pasta": pasta, "tk": _tk_available(), "recentes": _recentes(request), "erro": None}
    return _render(request, "rodar.html", ctx)


@router.get("/rodar/pasta", response_class=HTMLResponse)
def rodar_pasta(request: Request, projeto: str = ""):
    """Passos 2 a 4 da pasta informada, mais o passo 1 recolhido numa linha."""
    tpl, ctx = _fluxo(request, projeto)
    return _render(request, "_rodar_fluxo.html", {**ctx, "fluxo_tpl": tpl, "ok": tpl != "_rodar_erro.html",
                                                  "recentes": _recentes(request), "tk": _tk_available()})


@router.get("/rodar/trocar", response_class=HTMLResponse)
def rodar_trocar(request: Request, projeto: str = ""):
    """Reabre o passo 1 e limpa o resto."""
    return _render(request, "_rodar_trocar.html", {"pasta": _limpa(projeto), "tk": _tk_available(),
                                                   "recentes": _recentes(request)})


@router.get("/rodar/projeto", response_class=HTMLResponse)
def rodar_projeto(request: Request, repo: str = "", raiz: str = ""):
    """Passos 3 e 4 de um projeto da coleção do Bugs.jar (o passo 2 vira uma linha com "trocar")."""
    r = _repo_valido(repo)
    if r is None:
        return _render(request, "_rodar_erro.html", {"erro": "Projeto do Bugs.jar não encontrado."}, 404)
    return _render(request, "_rodar_selecionado.html", {**_ctx_form(request, r), "pasta": raiz or str(r.parent),
                                                        "colecao": raiz or str(r.parent)})


@router.get("/rodar/contagem", response_class=HTMLResponse)
def rodar_contagem(repo: str = ""):
    """Quantos bugs tem o projeto (lido sob demanda: listar os ramos demora um pouco)."""
    r = _repo_valido(repo)
    if r is None:
        return HTMLResponse("-")
    n = len(bugsjar_repo_bugs(r))
    return HTMLResponse(f"{n} bug{'s' if n != 1 else ''}")


@router.post("/rodar/escolher", response_class=HTMLResponse)
def rodar_escolher(projeto: str = Form("")):
    """Seletor de pastas nativo (o mesmo de Executar); devolve o campo já preenchido."""
    if not _tk_available():
        return HTMLResponse('<span class="erro">O seletor de pastas não está disponível neste computador.</span>',
                            status_code=404)
    escolhida = _pick_folder() or projeto
    return HTMLResponse(f'<input type="text" id="projeto" name="projeto" value="{escape(escolhida, quote=True)}" '
                        'autocomplete="off" placeholder="C:\\caminho\\da\\pasta" required>')


def _erro_pagina(request: Request, pasta: str, erro: str):
    ctx = {"page": "rodar", "pasta": pasta, "tk": _tk_available(), "recentes": _recentes(request), "erro": erro}
    return _render(request, "rodar.html", ctx, 400)


@router.post("/rodar")
def rodar_post(request: Request, modo: str = Form("bugsjar"), pasta: str = Form(""), repo: str = Form(""),
               sel: list[str] = Form([]), kinds: list[str] = Form([]), auto_search: str = Form(""),
               escopo: str = Form("relevant"), module: str = Form("")):
    ks = [k for k, _ in KINDS if k in kinds]
    if not ks:
        return _erro_pagina(request, pasta, "Escolha pelo menos uma ferramenta.")
    auto = bool(auto_search)
    st = request.app.state
    if modo == "maven":
        try:
            bug = local_bug(_limpa(pasta), _limpa(module))
        except ValueError as e:
            return _erro_pagina(request, pasta, str(e))
        st.local_bugs[bug.key] = bug
        bid = jobs_queue.create_batch(st.home, f"executar {bug.project}", [bug], ks,
                                      {"test_scope": "all", "auto_search": auto})
        add_recente(st.home, _limpa(pasta), bug.project, ks, [], auto, bug.extra.get("module", ""))
        return RedirectResponse(f"/executar/{bid}", status_code=303)
    r = _repo_valido(repo)
    if r is None:
        return _erro_pagina(request, pasta, "Esse projeto não é um repositório do Bugs.jar.")
    por_ramo: dict[str, Bug] = {b.bug_id: b for b in bugsjar_repo_bugs(r)}
    escolhidos = [por_ramo[x] for x in dict.fromkeys(sel) if x in por_ramo]
    if not escolhidos:
        return _erro_pagina(request, pasta, "Marque pelo menos um bug.")
    for b in escolhidos:         # a tela de cada bug abre mesmo sem bugsjar.root
        st.local_bugs.setdefault(b.key, b)
    opts = {"test_scope": "all" if escopo == "all" else "relevant", "auto_search": auto, "archive_checkout": False}
    ks = kinds_validos(ks)
    bid = jobs_queue.create_batch(st.home, f"bugs.jar {r.name}: {len(escolhidos)} bug(s)", escolhidos, ks, opts)
    return RedirectResponse(f"/executar/{bid}" if len(escolhidos) == 1 else f"/lote/{bid}", status_code=303)
