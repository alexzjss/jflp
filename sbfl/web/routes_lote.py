"""Vista do lote: tabela por bug (Maven, Jaguar 1, Jaguar 2, gabarito), detalhe do bug, resumos do JIRA, CSV e
cancelamento. O fragmento `_lote_vista.html` é usado por `/rodar?lote=N` e pela página `/lote/{bid}`."""
from __future__ import annotations

import hashlib
from html import escape
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import bugview
from .. import queue as jobs_queue

router = APIRouter()

POLL_SECONDS = 2


def _render(request: Request, name: str, ctx: dict, status: int = 200):
    return request.app.state.templates.TemplateResponse(request, name, ctx, status_code=status)


def _sem_lote(request: Request, bid: int):
    return _render(request, "mensagem.html", {"page": "resultados", "titulo": "Lote não encontrado",
                                              "texto": f"Não existe o lote {bid}."}, 404)


def uid(chave: str) -> str:
    """Identificador curto e seguro para `id` HTML de um bug (a chave tem `/` e outros caracteres)."""
    return hashlib.blake2s(chave.encode("utf-8"), digest_size=4).hexdigest()


def vista_ctx(request: Request, bid: int) -> dict | None:
    """Contexto de `_lote_vista.html`; None se o lote não existe."""
    home = request.app.state.home
    lote = jobs_queue.get_batch(home, bid)
    if lote is None:
        return None
    v = bugview.lote_view(home, bid)
    for b in v["bugs"]:
        b["uid"] = uid(b["key"])
    ver = {c: bugview.veredito(c) for c in bugview.VEREDITOS}
    return {"v": v, "bid": bid, "lote": lote, "ver": ver, "poll": POLL_SECONDS,
            "com_catalogo": any(b["catalogo"] for b in v["bugs"])}


@router.get("/lote/{bid}", response_class=HTMLResponse)
def lote_pagina(request: Request, bid: int):
    ctx = vista_ctx(request, bid)
    if ctx is None:
        return _sem_lote(request, bid)
    return _render(request, "lote_pagina.html", {"page": "resultados", **ctx})


@router.get("/lote/{bid}/tabela", response_class=HTMLResponse)
def lote_tabela(request: Request, bid: int, sig: str = ""):
    """A vista inteira; com `sig` igual ao atual responde 204 (nada a trocar: as linhas abertas ficam como estão)."""
    home = request.app.state.home
    if jobs_queue.get_batch(home, bid) is None:
        return _sem_lote(request, bid)
    if sig and sig == bugview.lote_sig(home, bid):
        return Response(status_code=204)
    return _render(request, "_lote_vista.html", vista_ctx(request, bid))


@router.get("/lote/{bid}/resumos", response_class=HTMLResponse)
def lote_resumos(request: Request, bid: int):
    """Resumo do JIRA de cada bug (lê o git; fora da vista que se atualiza sozinha): trocas fora de banda nos
    campos `res-<uid>`."""
    if jobs_queue.get_batch(request.app.state.home, bid) is None:
        return HTMLResponse("", status_code=404)
    res = bugview.lote_resumos(request.app.state.home, bid)
    return HTMLResponse("".join(f'<span id="res-{uid(k)}" hx-swap-oob="innerHTML">{escape(r["resumo"])}</span>'
                                for k, r in res.items()))


def _codigo_rel(d: dict) -> str | None:
    """Execução usada nos links "ver no código": um Jaguar (mostra as linhas suspeitas), senão o Maven."""
    for b in list(d["jaguar"].values()) + [d["maven"]]:
        if b and b.get("rel"):
            return b["rel"]
    return None


@router.get("/lote/{bid}/bug", response_class=HTMLResponse)
def lote_bug(request: Request, bid: int, key: str = ""):
    """Detalhe de um bug do lote (carregado quando a linha é aberta)."""
    home = request.app.state.home
    if jobs_queue.get_batch(home, bid) is None:
        return _sem_lote(request, bid)
    d = bugview.bug_detail(home, bid, key)
    if d is None:
        return HTMLResponse('<p class="aviso erro">Esse bug não faz parte do lote.</p>', status_code=404)
    return _render(request, "_lote_detalhe.html", {"d": d, "bid": bid, "codigo_rel": _codigo_rel(d),
                                                   "ver": {c: bugview.veredito(c) for c in bugview.VEREDITOS}})


@router.get("/lote/{bid}/resultado.csv")
def lote_resultado_csv(request: Request, bid: int):
    home = request.app.state.home
    if jobs_queue.get_batch(home, bid) is None:
        return _sem_lote(request, bid)
    return Response(bugview.lote_csv(home, bid), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="lote-{bid}.csv"'})


@router.post("/lote/{bid}/parar")
def lote_parar(request: Request, bid: int):
    """Cancela o lote: os pendentes saem da fila e o job em andamento é interrompido (a execução é descartada)."""
    home = request.app.state.home
    if jobs_queue.get_batch(home, bid) is None:
        return _sem_lote(request, bid)
    worker = request.app.state.worker
    worker.cancel(bid)
    for j in jobs_queue.batch_jobs(home, bid):
        if j["state"] == "running":
            worker.cancel_job(j["id"])
    if request.headers.get("hx-request"):
        return _render(request, "_lote_vista.html", vista_ctx(request, bid))
    return RedirectResponse(f"/lote/{quote(str(bid))}", status_code=303)
