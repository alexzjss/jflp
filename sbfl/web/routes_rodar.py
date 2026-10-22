"""Tela Rodar (página inicial): escolher a pasta e rodar bugs em três passos, tudo na mesma página (htmx).

1. Pasta  2. Projetos e bugs (ou módulo)  3. Ferramentas e opções  ->  um lote, cuja vista (`_lote_vista.html`)
aparece na própria página; a URL vira `/rodar?lote=N` (recarregar restaura)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from html import escape
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import queue as jobs_queue
from .. import status as S
from .. import store
from ..adapters.base import Bug
from ..prepare import (bugsjar_label, bugsjar_repo_bugs, bugsjar_summary, find_bugsjar_repo, find_poms,
                       is_bugsjar_collection, is_bugsjar_repo, local_bug)
from .comum import (KIND_LABEL, KINDS, _pick_folder, _ramo, _tk_available, add_recente, is_bugsjar_repo_ou_ramo,
                    kinds_validos, load_recentes)
from .routes_lote import vista_ctx

router = APIRouter()

PADRAO = ("maven", "jaguar2")      # Jaguar 1 fica desmarcado
SEP = "|"                          # separa repositório e ramo no valor de cada bug marcado (inválido em caminhos do Windows)


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
    """{ramo: {"label", "tom", "st"}} do resultado mais recente de cada bug do repositório (uma consulta só).
    `st` é o grupo do filtro da tela: `ok` ou `falha` (quem nunca rodou não aparece)."""
    out: dict[str, dict] = {}
    for r in store.list_runs(home, "bugsjar", repo.name):       # mais recentes primeiro
        if r["bug"] not in out:
            tom = S.tom(r["status"])
            out[r["bug"]] = {"label": S.label(r["status"]), "tom": tom, "st": "ok" if tom == "ok" else "falha"}
    return out


def _ctx_bugs(request: Request, repo: Path, pi: int) -> dict:
    ult = _ultimo_status(request.app.state.home, repo)
    atual = _ramo(repo)
    bugs = [{"branch": b.bug_id, "id": bugsjar_label(b.bug_id), "ult": ult.get(b.bug_id),
             "st": (ult.get(b.bug_id) or {}).get("st", "nunca"), "marcado": b.bug_id == atual}
            for b in bugsjar_repo_bugs(repo)]
    return {"repo": str(repo), "pi": pi, "bugs": bugs}


def _ctx_form(request: Request, projetos: list[dict], **kw) -> dict:
    return {"modo": "bugsjar", "projetos": projetos, "kinds": KINDS, "sel": PADRAO, **kw}


def _fluxo(request: Request, pasta: str) -> tuple[str, dict]:
    """Detecta o que há na pasta: devolve (template do fluxo, contexto)."""
    p = Path(_limpa(pasta)).expanduser()
    base: dict = {"pasta": _limpa(pasta), "erro": None}
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
        projetos = [{"nome": x.name, "caminho": str(x), "pi": i, "bugs": None}
                    for i, x in enumerate(_projetos(p))]
        return "_rodar_form.html", {**base, **_ctx_form(request, projetos), "salvo": mudou, "colecao": True}
    repo = find_bugsjar_repo(p) or (p if _repo_valido(str(p)) else None)
    if repo is not None:
        proj = {"nome": repo.name, "caminho": str(repo), "pi": 0, **_ctx_bugs(request, repo, 0)}
        return "_rodar_form.html", {**base, **_ctx_form(request, [proj]), "colecao": False}
    mods = list(find_poms(p))
    if (p / "pom.xml").is_file() or mods:
        return "_rodar_form.html", {**base, "modo": "maven", "nome": p.name, "modulos": mods,
                                    "raiz_pom": (p / "pom.xml").is_file(), "kinds": KINDS, "sel": PADRAO,
                                    "projetos": [], "colecao": False}
    return "_rodar_erro.html", {**base, "erro": "Não encontrei um repositório do Bugs.jar nem um projeto Maven "
                                "(pom.xml) nessa pasta."}


@router.get("/rodar", response_class=HTMLResponse)
def rodar_pagina(request: Request, pasta: str = "", lote: int = 0):
    st = request.app.state
    pasta = _limpa(pasta) or str(st.settings.get("bugsjar", "root") or "")
    ctx = {"page": "rodar", "pasta": pasta, "tk": _tk_available(), "recentes": _recentes(request), "erro": None,
           "lote": 0}
    status = 200
    if lote:
        vista = vista_ctx(request, lote)
        if vista is None:
            ctx["erro"], status = f"Não existe o lote {lote}.", 404
        else:
            ctx.update(vista, lote=lote, resumo=_resumo(request, lote))
    return _render(request, "rodar.html", ctx, status)


def _resumo(request: Request, bid: int) -> dict:
    """Linha do formulário recolhido: "12 bugs em 3 projetos · Maven, Jaguar 2"."""
    jobs = jobs_queue.batch_jobs(request.app.state.home, bid)
    bugs = list(dict.fromkeys(j["bug_key"] for j in jobs))
    kinds = [KIND_LABEL[k] for k, _ in KINDS if any(j["kind"] == k for j in jobs)]
    projetos = list(dict.fromkeys(k.split("/")[1] for k in bugs if k.count("/") >= 2))
    if bugs and all(k.startswith("local/") for k in bugs):
        quantos = f"Projeto Maven {projetos[0]}" if projetos else "1 projeto Maven"
    else:
        quantos = f"{len(bugs)} bug{'s' if len(bugs) != 1 else ''}"
        if len(projetos) > 1:
            quantos += f" em {len(projetos)} projetos"
    return {"quantos": quantos, "ferramentas": ", ".join(kinds), "bid": bid}


@router.get("/rodar/pasta", response_class=HTMLResponse)
def rodar_pasta(request: Request, projeto: str = ""):
    """Passos 2 e 3 da pasta informada, mais o passo 1 recolhido numa linha."""
    tpl, ctx = _fluxo(request, projeto)
    return _render(request, "_rodar_fluxo.html", {**ctx, "fluxo_tpl": tpl, "ok": tpl != "_rodar_erro.html",
                                                  "recentes": _recentes(request), "tk": _tk_available()})


@router.get("/rodar/trocar", response_class=HTMLResponse)
def rodar_trocar(request: Request, projeto: str = ""):
    """Reabre o passo 1 e limpa o resto."""
    return _render(request, "_rodar_trocar.html", {"pasta": _limpa(projeto), "tk": _tk_available(),
                                                   "recentes": _recentes(request)})


@router.get("/rodar/bugs", response_class=HTMLResponse)
def rodar_bugs(request: Request, repo: str = "", pi: int = 0):
    """Lista de bugs de um projeto (carregada quando a seção é aberta)."""
    r = _repo_valido(repo)
    if r is None:
        return _render(request, "_rodar_erro.html", {"erro": "Projeto do Bugs.jar não encontrado."}, 404)
    return _render(request, "_rodar_bugs.html", _ctx_bugs(request, r, pi))


def _resumo_de(st, repo: Path, bug: Bug) -> str:
    """Resumo do JIRA do bug, com cache por (repositório, ramo); lê do ramo que o bug usa (`origin/...` se for o caso)."""
    key = (str(repo), bug.bug_id)
    if key not in st.resumos:
        st.resumos[key] = bugsjar_summary(repo, bug.extra.get("buggy") or bug.bug_id)
    return st.resumos[key]


@router.get("/rodar/resumos", response_class=HTMLResponse)
def rodar_resumos(request: Request, repo: str = "", pi: int = 0):
    """Resumo do JIRA de cada bug do projeto (lê o git, por isso à parte): devolve só trocas fora de banda
    (`hx-swap-oob`) nos campos `rs-<pi>-<n>` da lista."""
    r = _repo_valido(repo)
    if r is None:
        return HTMLResponse("")
    st = request.app.state
    with ThreadPoolExecutor(max_workers=6) as pool:
        textos = list(pool.map(lambda b: _resumo_de(st, r, b), bugsjar_repo_bugs(r)))
    return HTMLResponse("".join(f'<span id="rs-{pi}-{i}" hx-swap-oob="innerHTML">{escape(t)}</span>'
                                for i, t in enumerate(textos)))


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
    """Seletor de pastas nativo; devolve o campo já preenchido."""
    if not _tk_available():
        return HTMLResponse('<span class="erro">O seletor de pastas não está disponível neste computador.</span>',
                            status_code=404)
    escolhida = _pick_folder() or projeto
    return HTMLResponse(f'<input type="text" id="projeto" name="projeto" value="{escape(escolhida, quote=True)}" '
                        'autocomplete="off" placeholder="C:\\caminho\\da\\pasta" required>')


def _erro(request: Request, pasta: str, erro: str):
    """Recusa do formulário: um trecho no lugar certo para o htmx; a página inteira para um envio comum."""
    if request.headers.get("hx-request"):
        return HTMLResponse(f'<p class="aviso erro" role="alert">{escape(erro)}</p>', status_code=400,
                            headers={"HX-Retarget": "#rd-erro", "HX-Reswap": "innerHTML"})
    ctx = {"page": "rodar", "pasta": pasta, "tk": _tk_available(), "recentes": _recentes(request), "erro": erro,
           "lote": 0}
    return _render(request, "rodar.html", ctx, 400)


def _lote_criado(request: Request, bid: int):
    """htmx: a vista do lote no lugar certo e a URL `/rodar?lote=N`; envio comum: redireciona para ela."""
    destino = f"/rodar?lote={bid}"
    if not request.headers.get("hx-request"):
        return RedirectResponse(destino, status_code=303)
    ctx = {**vista_ctx(request, bid), "resumo": _resumo(request, bid)}
    resp = _render(request, "_rodar_resultado.html", ctx)
    resp.headers["HX-Push-Url"] = destino
    return resp


def _bugs_marcados(sel: list[str], repo: str) -> tuple[list[Bug], str | None]:
    """Bugs dos valores `repositório|ramo` (ou só `ramo`, com `repo`); (bugs, erro)."""
    por_repo: dict[str, list[str]] = {}
    for v in dict.fromkeys(sel):
        r, sep, ramo = v.rpartition(SEP)
        por_repo.setdefault(r if sep else repo, []).append(ramo)
    escolhidos: list[Bug] = []
    for caminho, ramos in por_repo.items():
        r = _repo_valido(caminho)
        if r is None:
            return [], "Esse projeto não é um repositório do Bugs.jar."
        todos = {b.bug_id: b for b in bugsjar_repo_bugs(r)}
        escolhidos += [todos[x] for x in ramos if x in todos]
    return escolhidos, None


@router.post("/rodar")
def rodar_post(request: Request, modo: str = Form("bugsjar"), pasta: str = Form(""), repo: str = Form(""),
               sel: list[str] = Form([]), kinds: list[str] = Form([]), auto_search: str = Form(""),
               escopo: str = Form("relevant"), module: str = Form("")):
    ks = [k for k, _ in KINDS if k in kinds]
    if not ks:
        return _erro(request, pasta, "Escolha pelo menos uma ferramenta.")
    auto = bool(auto_search)
    st = request.app.state
    if modo == "maven":
        try:
            bug = local_bug(_limpa(pasta), _limpa(module))
        except ValueError as e:
            return _erro(request, pasta, str(e))
        st.local_bugs[bug.key] = bug
        bid = jobs_queue.create_batch(st.home, f"executar {bug.project}", [bug], ks,
                                      {"test_scope": "all", "auto_search": auto})
        add_recente(st.home, _limpa(pasta), bug.project, ks, [], auto, bug.extra.get("module", ""))
        return _lote_criado(request, bid)
    escolhidos, err = _bugs_marcados(sel, repo)
    if err:
        return _erro(request, pasta, err)
    if not escolhidos:
        return _erro(request, pasta, "Marque pelo menos um bug.")
    for b in escolhidos:         # a tela de cada execução abre mesmo sem bugsjar.root
        st.local_bugs.setdefault(b.key, b)
    opts = {"test_scope": "all" if escopo == "all" else "relevant", "auto_search": auto, "archive_checkout": False}
    ks = kinds_validos(ks)
    projetos = list(dict.fromkeys(b.project for b in escolhidos))
    nome = (f"bugs.jar {projetos[0]}: {len(escolhidos)} bug(s)" if len(projetos) == 1
            else f"bugs.jar {len(projetos)} projetos: {len(escolhidos)} bug(s)")
    bid = jobs_queue.create_batch(st.home, nome, escolhidos, ks, opts)
    return _lote_criado(request, bid)
