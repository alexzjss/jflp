"""Telas de bugs: seleção, página do bug, revisão do POM do Jaguar 2 e log ao vivo (SSE)."""
from __future__ import annotations

import asyncio
import json
import queue as _q
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from .. import queue as jobs_queue
from .. import recipes, store
from ..adapters.base import Bug
from ..errors import StageError
from ..pomgen import FIXES, generate
from ..prepare import bug_from_key, list_bugs, local_bug, prepare

router = APIRouter()

BENCHMARKS = (("bugsjar", "Bugs.jar"), ("d4j", "Defects4J"))
KINDS = (("maven", "Maven"), ("jaguar2", "Jaguar 2"), ("jaguar1", "Jaguar 1"))
KIND_LABEL = dict(KINDS)


def _render(request: Request, name: str, ctx: dict, status: int = 200):
    return request.app.state.templates.TemplateResponse(request, name, ctx, status_code=status)


def _list_ctx(request: Request, **kw) -> dict:
    ctx = {"page": "bugs", "benchmarks": BENCHMARKS, "bench": "", "proj": "", "projects": [],
           "rows": None, "error": None, "pendentes": False, "kinds": KINDS,
           "locais": list(request.app.state.local_bugs.values())}
    ctx.update(kw)
    return ctx


def _not_found(request: Request, msg: str):
    return _render(request, "bugs.html", _list_ctx(request, error=msg), 404)


def _projects(settings, bench: str) -> list[str]:
    if bench == "d4j":
        return list(settings.get("defects4j", "projects") or [])
    return sorted({b.project for b in list_bugs(settings, "bugsjar")})


def _all_jobs(home) -> list[dict]:
    return jobs_queue.list_jobs(home, limit=1000000)


def _find_bug(request: Request, key: str) -> Bug | None:
    st = request.app.state
    registry: dict = st.local_bugs
    if key in registry:
        return registry[key]
    if key.startswith("local/"):   # reconstrói de um job guardado (após reiniciar o app)
        for job in _all_jobs(st.home):
            if job["bug_key"] == key:
                p = key.split("/", 2)
                bug = Bug(p[0], p[1], p[2], json.loads(job["bug_extra"] or "{}"))
                registry[key] = bug
                return bug
        return None
    try:
        return bug_from_key(st.settings, key)
    except Exception:       # chave inválida ou benchmark mal configurado
        return None


def _latest_by_kind(home, bug: Bug) -> dict:
    out: dict = {}
    for r in store.list_runs(home, bug.benchmark, bug.project, bug.bug_id):   # mais recentes primeiro
        out.setdefault(r["kind"], r)
    return out


# ------------------------------------------------------------------- lista

@router.get("/bugs", response_class=HTMLResponse)
def bugs(request: Request, bench: str = "", proj: str = "", pendentes: int = 0):
    st = request.app.state
    ctx = _list_ctx(request, bench=bench, proj=proj, pendentes=bool(pendentes))
    if bench in dict(BENCHMARKS):
        try:
            ctx["projects"] = _projects(st.settings, bench)
            if not ctx["projects"]:
                ctx["error"] = ("Nenhum projeto encontrado: configure o benchmark "
                                "(pasta do Bugs.jar ou projetos do Defects4J).")
            elif proj:
                rows = []
                for b in list_bugs(st.settings, bench, proj):
                    last = _latest_by_kind(st.home, b)
                    if pendentes and any(r["status"] == "ok" for r in last.values()):
                        continue
                    rows.append({"bug": b, "last": last})
                ctx["rows"] = rows
        except StageError as e:
            ctx["error"] = e.detail or str(e)
        except Exception as e:
            ctx["error"] = f"Não foi possível listar os bugs: {e}"
    elif bench:
        ctx["error"] = f"Benchmark desconhecido: {bench}"
    return _render(request, "bugs.html", ctx)


@router.post("/bugs/local")
def bugs_local(request: Request, spec: str = Form(...)):
    try:
        bug = local_bug(spec.strip().strip('"'))
    except ValueError as e:
        return _render(request, "bugs.html", _list_ctx(request, error=str(e)), 400)
    request.app.state.local_bugs[bug.key] = bug
    return RedirectResponse(f"/bug/{bug.key}", status_code=303)


# --------------------------------------------------------------------- bug

@router.get("/bug/{key:path}/jaguar2", response_class=HTMLResponse)
def jaguar2_get(request: Request, key: str):
    bug = _find_bug(request, key)
    if bug is None:
        return _not_found(request, f"Bug não encontrado: {key}")
    st = request.app.state
    try:
        prep = prepare(st.home, st.settings, bug, st.home.work / "web_prepare.log")
    except StageError as e:
        return _render(request, "revisao_pom.html", {
            "page": "bugs", "bug": bug, "plan": None, "error": e.detail or str(e)})
    mod = prep.pristine if prep.module == "." else prep.pristine / prep.module
    recipe = recipes.find(st.home, bug, "jaguar2")
    plan = generate(Path(mod), st.settings, 11,
                    set(recipe.fixes) if recipe and recipe.fixes else None, set(),
                    dict(recipe.params) if recipe else {})
    diff_lines = []
    for ln in plan.diff.splitlines():
        cls = ("mais" if ln.startswith("+") and not ln.startswith("+++")
               else "menos" if ln.startswith("-") and not ln.startswith("---") else "")
        diff_lines.append((cls, ln))
    return _render(request, "revisao_pom.html", {
        "page": "bugs", "bug": bug, "plan": plan, "error": None, "recipe": recipe,
        "fixes": list(FIXES.values()), "checked": set(plan.applied) | set(plan.detected),
        "diff_lines": diff_lines})


def _enqueue(request: Request, bug: Bug, kind: str, scope: str, auto: bool, extra: dict | None = None):
    st = request.app.state
    opts = {"test_scope": scope if scope in ("relevant", "all") else "relevant", "auto_search": auto}
    opts.update(extra or {})
    bid = jobs_queue.create_batch(st.home, f"{kind} {bug.key}", [bug], [kind], opts)
    job = jobs_queue.batch_jobs(st.home, bid)[0]
    return RedirectResponse(f"/job/{job['id']}/ao-vivo", status_code=303)


@router.post("/bug/{key:path}/jaguar2")
def jaguar2_post(request: Request, key: str, fix: list[str] = Form([]), detected: list[str] = Form([]),
                 scope: str = Form("relevant"), auto_search: str = Form("")):
    bug = _find_bug(request, key)
    if bug is None:
        return _not_found(request, f"Bug não encontrado: {key}")
    on = [f for f in fix if f in FIXES]
    off = [f for f in detected if f in FIXES and f not in on]
    return _enqueue(request, bug, "jaguar2", scope, bool(auto_search),
                    {"fixes_on": on, "fixes_off": off, "params": {}})


@router.post("/bug/{key:path}/rodar")
def rodar(request: Request, key: str, kind: str = Form(...), scope: str = Form("relevant"),
          auto_search: str = Form("")):
    bug = _find_bug(request, key)
    if bug is None:
        return _not_found(request, f"Bug não encontrado: {key}")
    if kind not in KIND_LABEL:
        return _not_found(request, f"Tipo de execução desconhecido: {kind}")
    return _enqueue(request, bug, kind, scope, bool(auto_search))


@router.get("/bug/{key:path}", response_class=HTMLResponse)
def bug_page(request: Request, key: str):
    bug = _find_bug(request, key)
    if bug is None:
        return _not_found(request, f"Bug não encontrado: {key}")
    runs = store.list_runs(request.app.state.home, bug.benchmark, bug.project, bug.bug_id)
    return _render(request, "bug.html", {"page": "bugs", "bug": bug, "runs": runs,
                                         "kind_label": KIND_LABEL})


# ------------------------------------------------------------------ ao vivo

def _job(home, job_id: int) -> dict | None:
    return next((j for j in _all_jobs(home) if j["id"] == job_id), None)


@router.get("/job/{job_id}/ao-vivo", response_class=HTMLResponse)
def ao_vivo(request: Request, job_id: int):
    job = _job(request.app.state.home, job_id)
    if job is None:
        return _not_found(request, f"Job não encontrado: {job_id}")
    return _render(request, "execucao_viva.html", {"page": "bugs", "job": job})


def _sse(event: str | None, data: str) -> str:
    head = f"event: {event}\n" if event else ""
    return head + "".join(f"data: {ln}\n" for ln in (data.split("\n") or [""])) + "\n"


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
                if job["state"] != "pending":
                    break
                yield _sse("espera", "na fila")
                await asyncio.sleep(2)
                if await request.is_disconnected():
                    return
            if job["state"] == "running":
                sub = worker.subscribe()           # assina antes de ler o buffer: nenhuma linha se perde
                cur = worker.current()
                if cur and cur.get("job_id") == job_id:
                    for line in cur.get("log", []):
                        yield _sse(None, line)
                    while True:
                        try:
                            line = await asyncio.to_thread(sub.get, True, 1.0)
                        except _q.Empty:
                            if await request.is_disconnected():
                                return
                            continue
                        if line is None:
                            break
                        yield _sse(None, line)
                    job = await asyncio.to_thread(_job, home, job_id) or job
            yield _sse("fim", job.get("run_rel") or "")
        finally:
            if sub is not None:
                worker.unsubscribe(sub)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})
