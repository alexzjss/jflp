"""Acompanhar um job: tela ao vivo (log e progresso por SSE), resumo ao fim e a rota do stream."""
from __future__ import annotations

import asyncio
import json
import queue as _q
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from .. import status as S
from .. import store
from .. import queue as jobs_queue
from .comum import KIND_LABEL, _msg, _render

router = APIRouter()

PENDING_POLL = 2.0     # job na fila: aviso a cada 2 s
START_POLL = 0.5       # job running no banco, mas o worker ainda não o tem


def _job(home, job_id: int) -> dict | None:
    return jobs_queue.get_job(home, job_id)


@router.get("/job/{job_id}/ao-vivo", response_class=HTMLResponse)
def ao_vivo(request: Request, job_id: int):
    job = _job(request.app.state.home, job_id)
    if job is None:
        return _msg(request, "rodar", "Job não encontrado", f"Não existe o job {job_id}.", 404)
    bug_nome = job["bug_key"].split("/", 1)[-1]
    return _render(request, "execucao_viva.html", {"page": "rodar", "job": job, "bug_nome": bug_nome,
                                                   "kind_nome": KIND_LABEL.get(job["kind"], job["kind"])})


@router.get("/job/{job_id}/resumo", response_class=HTMLResponse)
def resumo(request: Request, job_id: int):
    """Resumo de um job encerrado (status, testes, tempo e atalhos), carregado pela tela ao vivo no fim."""
    home = request.app.state.home
    job = _job(home, job_id)
    if job is None:
        return HTMLResponse("", status_code=404)
    run, rel = None, job.get("run_rel")
    if job["state"] == "done" and rel:
        try:
            run = store.load_run(home, rel)
        except (OSError, ValueError):
            run = None
    ctx = {"job": job, "run": run, "rel": rel, "ok": None, "tests": None, "kind_nome": KIND_LABEL.get(job["kind"])}
    if run:
        ctx["ok"] = S.is_success(run.get("status") or "")
        ctx["tom"] = S.tom(run.get("status") or "")
        ctx["rotulo"] = S.label(run.get("status") or "")
        ctx["tests"] = run.get("tests")
    return _render(request, "_execucao_resumo.html", ctx)


def _sse(event: str | None, data: str) -> str:
    head = f"event: {event}\n" if event else ""
    lines = data.splitlines() or [""]    # \r e \r\n também quebram linha: nada injeta campos SSE
    return head + "".join(f"data: {ln}\n" for ln in lines) + "\n"


ESTADO_EVERY = 1.0     # segundos entre eventos `estado` enquanto o job roda


def _estimativa(home, job: dict) -> dict | None:
    """Testes e segundos da última execução do mesmo bug e tipo (para as barras de progresso)."""
    try:
        b, p, i = job["bug_key"].split("/", 2)
        for r in store.list_runs(home, b, p, i, job["kind"]):
            if r.get("n_tests"):
                return {"testes": r["n_tests"], "segundos": r.get("seconds")}
    except Exception:
        pass
    return None


def _estado_sse(worker, job_id: int, estimativa) -> str:
    """Evento `estado` com o progresso do job (vazio se o worker não o tem)."""
    prog = worker.progress_of(job_id)
    if not isinstance(prog, dict):
        return ": sem progresso\n\n"      # comentário SSE: o navegador ignora
    return _sse("estado", json.dumps({**prog, "estimativa": estimativa}, ensure_ascii=False))


@router.get("/job/{job_id}/stream")
async def stream(request: Request, job_id: int):
    st = request.app.state
    worker, home = st.worker, st.home

    async def gen():
        sub = None
        try:
            while True:
                job = await asyncio.to_thread(_job, home, job_id)
                if job is None:
                    yield _sse("fim", "")
                    return
                if job["state"] in ("done", "cancelled"):
                    yield _sse("fim", job.get("run_rel") or "")
                    return
                if job["state"] == "pending":
                    yield _sse("espera", "na fila")
                    delay = PENDING_POLL
                else:
                    cur = worker.current()
                    if cur and cur.get("job_id") == job_id:
                        break
                    delay = START_POLL     # running no banco, worker ainda não o reivindicou
                await asyncio.sleep(delay)
                if await request.is_disconnected():
                    return
            sub = worker.subscribe()       # assina antes de ler o buffer: nenhuma linha se perde
            est = await asyncio.to_thread(_estimativa, home, job)
            cur = worker.current()
            if cur and cur.get("job_id") == job_id:
                for line in cur.get("log", []):
                    yield _sse(None, line)
            last = time.monotonic()
            yield _estado_sse(worker, job_id, est)
            while True:
                try:
                    line = await asyncio.to_thread(sub.get, True, 1.0)
                except _q.Empty:
                    j = await asyncio.to_thread(_job, home, job_id)
                    if j is None or j["state"] != "running":   # assinante descartado: sem sentinela
                        break
                    if await request.is_disconnected():
                        return
                    last = time.monotonic()
                    yield _estado_sse(worker, job_id, est)
                    continue
                if line is None:
                    break
                yield _sse(None, line)
                if time.monotonic() - last >= ESTADO_EVERY:
                    last = time.monotonic()
                    yield _estado_sse(worker, job_id, est)
            job = await asyncio.to_thread(_job, home, job_id) or job
            yield _estado_sse(worker, job_id, est)
            yield _sse("fim", job.get("run_rel") or "")
        finally:
            if sub is not None:
                worker.unsubscribe(sub)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})
