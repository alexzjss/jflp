"""Resultados (lista de lotes e contagem de vereditos), mini-andamento dos jobs, cancelar e excluir lotes."""
from __future__ import annotations

import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from .. import bugview
from .. import queue as jobs_queue
from ..progress import linha
from .comum import KIND_LABEL, _msg, _render

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
    return _msg(request, "resultados", "Não foi possível", texto, status)


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


def _destino(proximo: str) -> str:
    return proximo if proximo.startswith("/") and not proximo.startswith("//") else "/resultados"


@router.post("/job/{job_id}/cancelar")
def job_cancelar(request: Request, job_id: int, proximo: str = Form("/resultados")):
    """Cancela um job: pendente sai da fila; em andamento tem o Maven morto e a execução descartada."""
    if jobs_queue.get_job(request.app.state.home, job_id) is None:
        return _msg(request, "resultados", "Job não encontrado", f"Não existe o job {job_id}.", 404)
    request.app.state.worker.cancel_job(job_id)
    return _voltar(request, _destino(proximo))


@router.post("/lote/{bid}/excluir")
def lote_excluir(request: Request, bid: int):
    """Apaga um lote encerrado do histórico (as execuções gravadas ficam)."""
    try:
        jobs_queue.delete_batch(request.app.state.home, bid)
    except ValueError as e:
        if "não encontrado" in str(e):
            return _msg(request, "resultados", "Lote não encontrado", f"Não existe o lote {bid}.", 404)
        return _recusa(request, str(e))
    return _voltar(request, "/resultados")


# ------------------------------------------------------------ resultados

@router.get("/fila/contagem", response_class=HTMLResponse)
def fila_contagem(request: Request):
    """Selo da barra lateral: jobs em andamento + esperando (vazio quando não há nenhum)."""
    home = request.app.state.home
    n = len(jobs_queue.list_jobs(home, state="running", limit=1000)) + len(jobs_queue.pending_jobs(home))
    return HTMLResponse(f'<span class="selo" title="{n} na fila">{n}</span>' if n else "")


LOTES_COM_CONTAGEM = 20      # só os lotes mais recentes têm a contagem de vereditos (ler as execuções custa)
ESTADO_LOTE = {"active": "rodando", "paused": "pausado", "cancelled": "cancelado", "done": "terminado"}


def _contagem(request: Request, bid: int) -> dict | None:
    """Vereditos do lote; lotes encerrados não mudam, então o resultado fica guardado (por assinatura dos jobs)."""
    st = request.app.state
    home = st.home
    sig = bugview.lote_sig(home, bid)
    if (bid, sig) in st.contagens:
        return st.contagens[(bid, sig)]
    v = bugview.lote_view(home, bid)
    if not v["pendente"]:
        st.contagens[(bid, sig)] = v["contagem"]
    return v["contagem"]


def _lotes(request: Request) -> list[dict]:
    """Lotes do mais recente ao mais antigo: nome, data, ferramentas, nº de bugs, vereditos e estado."""
    home = request.app.state.home
    out = []
    for i, b in enumerate(jobs_queue.list_batches(home, limit=200)):
        try:
            kinds = [KIND_LABEL.get(k, k) for k in json.loads(b["kinds"] or "[]")]
        except ValueError:
            kinds = []
        out.append({**b, "data": (b["created"] or "")[:16].replace("T", " "), "ferramentas": ", ".join(kinds),
                    "bugs": b["total"] // len(kinds) if kinds else b["total"],
                    "estado": ESTADO_LOTE.get(b["state"], b["state"]),
                    "andamento": f"{b['done']}/{b['total']}" if b["state"] in ("active", "paused") else "",
                    "encerrado": b["state"] in ("done", "cancelled"),
                    "contagem": _contagem(request, b["id"]) if i < LOTES_COM_CONTAGEM else None})
    return out


@router.get("/resultados", response_class=HTMLResponse)
def resultados(request: Request):
    """Lotes (mais recentes primeiro) com a contagem de vereditos; abrir leva à vista do lote."""
    ver = {c: bugview.veredito(c) for c in bugview.VEREDITOS}
    return _render(request, "resultados.html", {"page": "resultados", "lotes": _lotes(request), "ver": ver})
