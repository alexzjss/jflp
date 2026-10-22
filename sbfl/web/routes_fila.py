"""Fila global: ver o que roda e o que espera, cancelar o job em andamento, remover jobs e excluir lotes."""
from __future__ import annotations

import time

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from .. import queue as jobs_queue
from .routes_bugs import KIND_LABEL
from .routes_runs import _msg, _render

router = APIRouter()


def _voltar(request: Request, destino: str):
    """Depois de uma ação: o htmx recarrega a página em que estava; um formulário comum vai a `destino`."""
    if request.headers.get("hx-request"):
        return Response(status_code=204, headers={"HX-Refresh": "true"})
    return RedirectResponse(destino, status_code=303)


def _recusa(request: Request, texto: str, status: int = 409):
    """Recusa de uma ação: texto simples para o htmx (a base mostra num alerta), página para o resto."""
    if request.headers.get("hx-request"):
        return PlainTextResponse(texto, status_code=status)
    return _msg(request, "fila", "Não foi possível", texto, status)


def _tempo(seconds: float) -> str:
    s = int(max(seconds, 0))
    h, resto = divmod(s, 3600)
    m, s = divmod(resto, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min {s:02d} s"


def _fila_ctx(request: Request) -> dict:
    st = request.app.state
    cur = st.worker.current()
    cur = cur if isinstance(cur, dict) else {}
    nomes = {b["id"]: b["name"] for b in jobs_queue.list_batches(st.home, limit=1000)}
    running = []
    for j in jobs_queue.list_jobs(st.home, state="running", limit=10):
        started = cur.get("started") if cur.get("job_id") == j["id"] else None
        running.append({**j, "tempo": _tempo(time.time() - started) if started else None,
                        "batch_name": nomes.get(j["batch_id"], "")})
    return {"running": running, "pending": jobs_queue.pending_jobs(st.home), "kind_label": KIND_LABEL}


@router.get("/fila", response_class=HTMLResponse)
def fila(request: Request):
    return _render(request, "fila.html", {"page": "fila", **_fila_ctx(request)})


@router.get("/fila/estado", response_class=HTMLResponse)
def fila_estado(request: Request):
    return _render(request, "_fila_estado.html", _fila_ctx(request))


@router.post("/fila/limpar")
def fila_limpar(request: Request):
    request.app.state.worker.cancel_pending()
    return _voltar(request, "/fila")


@router.post("/fila/cancelar-tudo")
def fila_cancelar_tudo(request: Request):
    request.app.state.worker.cancel_everything()
    return _voltar(request, "/fila")


def _destino(proximo: str) -> str:
    return proximo if proximo.startswith("/") and not proximo.startswith("//") else "/fila"


@router.post("/job/{job_id}/cancelar")
def job_cancelar(request: Request, job_id: int, proximo: str = Form("/fila")):
    """Cancela um job: pendente sai da fila; em andamento tem o Maven morto e a execução descartada."""
    if jobs_queue.get_job(request.app.state.home, job_id) is None:
        return _msg(request, "fila", "Job não encontrado", f"Não existe o job {job_id}.", 404)
    request.app.state.worker.cancel_job(job_id)
    return _voltar(request, _destino(proximo))


@router.post("/job/{job_id}/remover")
def job_remover(request: Request, job_id: int, proximo: str = Form("/fila")):
    """Tira da fila um job que ainda não começou."""
    if jobs_queue.get_job(request.app.state.home, job_id) is None:
        return _msg(request, "fila", "Job não encontrado", f"Não existe o job {job_id}.", 404)
    if not request.app.state.worker.remove_pending(job_id):
        return _recusa(request, "O job já começou ou terminou: use Cancelar.")
    return _voltar(request, _destino(proximo))


@router.post("/lote/{bid}/excluir")
def lote_excluir(request: Request, bid: int):
    """Apaga um lote encerrado do histórico (as execuções gravadas ficam)."""
    try:
        jobs_queue.delete_batch(request.app.state.home, bid)
    except ValueError as e:
        if "não encontrado" in str(e):
            return _msg(request, "lotes", "Lote não encontrado", f"Não existe o lote {bid}.", 404)
        return _recusa(request, str(e))
    return _voltar(request, "/lotes")
