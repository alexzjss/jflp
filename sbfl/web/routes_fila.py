"""Fila global: ver o que roda e o que espera, cancelar o job em andamento, remover jobs e excluir lotes."""
from __future__ import annotations

import hashlib
import time

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from .. import queue as jobs_queue
from ..progress import linha
from .routes_bugs import KIND_LABEL
from .routes_runs import _batch_ctx, _msg, _render

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


def assinatura(*partes) -> str:
    """Resumo curto de "o que está na tela": muda só quando a estrutura (jobs e estados) muda."""
    return hashlib.blake2s("|".join(str(x) for x in partes).encode(), digest_size=4).hexdigest()


def _sem_mudanca(sig: str, atual: str):
    """204 (o htmx não troca nada) quando a tela do cliente ainda é a atual: os botões ficam onde estão."""
    return Response(status_code=204) if sig and sig == atual else None


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
    pending = jobs_queue.pending_jobs(st.home)
    sig = assinatura("r", *[j["id"] for j in running], "p", *[(j["id"], j.get("batch_state")) for j in pending])
    return {"running": running, "pending": pending, "kind_label": KIND_LABEL, "sig": sig}


@router.get("/fila", response_class=HTMLResponse)
def fila(request: Request):
    return _render(request, "fila.html", {"page": "fila", **_fila_ctx(request)})


@router.get("/fila/estado", response_class=HTMLResponse)
def fila_estado(request: Request, sig: str = ""):
    """A fila inteira; com `sig` igual ao atual responde 204 (nada a trocar, os botões não são recriados)."""
    ctx = _fila_ctx(request)
    return _sem_mudanca(sig, ctx["sig"]) or _render(request, "_fila_estado.html", ctx)


@router.get("/job/{job_id}/mini", response_class=HTMLResponse)
def job_mini(request: Request, job_id: int):
    """Só o miolo do andamento de um job (texto e barra fina): é o que se atualiza a cada segundo."""
    job = jobs_queue.get_job(request.app.state.home, job_id)
    if job is None:
        return HTMLResponse("", status_code=404)
    texto, ativo = "", False
    if job["state"] == "pending":
        texto, ativo = "Na fila", True
    elif job["state"] == "running":
        prog = request.app.state.worker.progress_of(job_id)
        texto, ativo = linha(prog if isinstance(prog, dict) else None), True
    elif job["state"] == "done":
        texto = "Concluído"
    elif job["state"] == "cancelled":
        texto = "Cancelado"
    return _render(request, "_mini_progresso.html", {"texto": texto, "ativo": ativo})


@router.get("/lote/{bid}/painel", response_class=HTMLResponse)
def lote_painel(request: Request, bid: int, sig: str = ""):
    """Painel do lote que só se refaz quando algum job muda de estado."""
    ctx = _batch_ctx(request, bid)
    if ctx is None:
        return _msg(request, "lotes", "Lote não encontrado", f"Não existe o lote {bid}.", 404)
    b = ctx["b"]
    ctx["sig"] = assinatura(b["state"], *[(j["id"], j["state"], j["run_rel"]) for j in b["jobs"]])
    return _sem_mudanca(sig, ctx["sig"]) or _render(request, "_lote_painel.html", ctx)


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
