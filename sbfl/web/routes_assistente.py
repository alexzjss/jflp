"""Assistente de primeiro uso: checklist, plano de downloads e preparação em segundo plano."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..pomgen.fixes import JAGUAR_ARTIFACTS, JAGUAR_GROUP
from ..run_jaguar2 import prefetch_jaguar2
from ..toolchain.detect import find_jdks, find_mavens
from ..toolchain.download import DownloadError, fetch
from ..toolchain.profiles import MissingTool, resolve
from .routes_env import KIND_LABEL, _git, _mb, _new_task, _plans, _render, jaguar1_lib

router = APIRouter()

GIT_URL = "https://git-scm.com/downloads"
ESCOPOS = ("essencial", "tudo")

# Passos na ordem de execução: o pre-fetch vem por último porque precisa do perfil jaguar2.
STEPS = (
    {"id": "jdk11", "label": "JDK 11", "plan": ("jdk", "11"), "essencial": True},
    {"id": "maven399", "label": "Maven 3.9.9", "plan": ("maven", "3.9.9"), "essencial": True},
    {"id": "jdk8", "label": "JDK 8", "plan": ("jdk", "8"), "essencial": False},
    {"id": "maven363", "label": "Maven 3.6.3", "plan": ("maven", "3.6.3"), "essencial": False},
    {"id": "jaguar1", "label": "Biblioteca do Jaguar 1 (2 jars)", "plan": ("jaguar1", ""), "essencial": False},
    {"id": "prefetch", "label": "Dependências do Jaguar 2 (uso offline)", "plan": None, "essencial": True},
)


# ---- estado da interface (<home>/estado-ui.json) -------------------------------------------

def _estado_path(home) -> Path:
    return Path(home.root) / "estado-ui.json"


def ler_estado_ui(home) -> dict:
    """Lê o estado; arquivo ausente, ilegível ou que não seja um objeto JSON vale como padrão."""
    try:
        data = json.loads(_estado_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def gravar_estado_ui(home, **valores) -> None:
    data = ler_estado_ui(home)
    data.update(valores)
    path = _estado_path(home)
    tmp = path.with_name(path.name + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def assistente_dispensado(home) -> bool:
    return ler_estado_ui(home).get("assistente_dispensado") is True


def deve_abrir_assistente(home, settings) -> bool:
    """O Painel manda para o assistente se o perfil do Jaguar 2 não resolve e ele não foi dispensado."""
    if assistente_dispensado(home):
        return False
    try:
        resolve("jaguar2", home, settings)
    except MissingTool:
        return True
    return False


# ---- presença de cada passo ------------------------------------------------------------------

def _prefetch_presente(home, settings) -> bool:
    """Sem marcador próprio: considera feito se o jaguar2-core da versão configurada está no m2."""
    versao = settings.get("jaguar2", "version", "0.0.2")
    pasta = Path(home.m2).joinpath(*JAGUAR_GROUP.split("."), JAGUAR_ARTIFACTS[2], str(versao))
    return pasta.is_dir() and any(pasta.glob("*.jar"))


def passo_presente(step_id: str, home, settings) -> bool:
    if step_id == "jdk11":
        return any(j.major == 11 for j in find_jdks(home, settings))
    if step_id == "jdk8":
        return any(j.major == 8 for j in find_jdks(home, settings))
    if step_id == "maven399":
        return any(m.version >= (3, 9, 9) for m in find_mavens(home, settings))
    if step_id == "maven363":
        return any(m.version == (3, 6, 3) for m in find_mavens(home, settings))
    if step_id == "jaguar1":
        return (jaguar1_lib(home, settings) / "jacocoagent.jar").is_file()
    if step_id == "prefetch":
        return _prefetch_presente(home, settings)
    raise KeyError(step_id)


def _steps_do_escopo(escopo: str) -> list[dict]:
    return [s for s in STEPS if escopo == "tudo" or s["essencial"]]


def _escopo(valor: str) -> str:
    return valor if valor in ESCOPOS else "tudo"


def _checklist(home, settings) -> list[dict]:
    rows = []
    git = _git()
    rows.append({"label": "Git", "ok": bool(git), "text": git or "não encontrado",
                 "git_link": not git})

    def perfil(nome):
        try:
            te = resolve(nome, home, settings)
            v = ".".join(str(n) for n in te.maven.version)
            return True, f"JDK {te.jdk.major}, Maven {v}"
        except MissingTool as e:
            return False, e.message

    ok, texto = perfil("jaguar2")
    rows.append({"label": "Perfil Jaguar 2 (JDK 11 + Maven 3.9.9 ou mais novo)", "ok": ok, "text": texto})
    pre = _prefetch_presente(home, settings)
    rows.append({"label": "Dependências do Jaguar 2 na pasta Maven própria", "ok": pre,
                 "text": "baixadas" if pre else "ainda não baixadas"})
    ok, texto = perfil("maven")
    rows.append({"label": f"Perfil {KIND_LABEL['maven']}", "ok": ok, "text": texto})
    ok, texto = perfil("jaguar1")
    lib = (jaguar1_lib(home, settings) / "jacocoagent.jar").is_file()
    if ok and not lib:
        ok, texto = False, "falta a biblioteca (lib) do Jaguar 1"
    rows.append({"label": "Perfil Jaguar 1 (JDK 8 + Maven 3.6.3 + lib)", "ok": ok, "text": texto})
    return rows


# ---- telas -------------------------------------------------------------------------------------

def _task_em_andamento(request: Request) -> str | None:
    for tid, t in request.app.state.tasks.items():
        if t.get("key") == "bem-vindo" and not t["done"]:
            return tid
    return None


@router.get("/bem-vindo", response_class=HTMLResponse)
def bem_vindo(request: Request):
    st = request.app.state
    ctx = {"page": "bem-vindo", "linhas": _checklist(st.home, st.settings), "git_url": GIT_URL,
           "tid": _task_em_andamento(request)}
    return _render(request, "bem_vindo.html", ctx)


@router.get("/bem-vindo/plano", response_class=HTMLResponse)
def bem_vindo_plano(request: Request, escopo: str = "tudo"):
    st = request.app.state
    escopo = _escopo(escopo)
    itens, falta = [], False
    for s in _steps_do_escopo(escopo):
        item = {"label": s["label"], "presente": False, "error": None, "plans": [], "m2": None}
        if passo_presente(s["id"], st.home, st.settings):
            item["presente"] = True
        else:
            falta = True
            if s["plan"] is None:
                item["m2"] = str(st.home.m2)
            else:
                try:
                    item["plans"] = [{"name": d.name, "url": d.url, "size": _mb(d.size), "dest": d.dest}
                                     for d in _plans(st.home, *s["plan"])]
                except DownloadError as e:
                    item["error"] = str(e)
        itens.append(item)
    return _render(request, "_bem_vindo_plano.html", {"linhas": itens, "escopo": escopo, "falta": falta})


def _set_step(task: dict, step_id: str, state: str, msg: str = "") -> None:
    for s in task["steps"]:
        if s["id"] == step_id:
            s["state"], s["msg"] = state, msg


def _run_step(step: dict, home, settings, task: dict) -> tuple[str, str]:
    """Executa um passo; devolve (estado, mensagem)."""
    if passo_presente(step["id"], home, settings):
        return "skipped", "já instalado"
    if step["plan"] is None:
        try:
            if prefetch_jaguar2(home, settings):
                return "ok", f"dependências em {home.m2}"
            return "failed", "Algumas dependências não foram baixadas; veja work/prefetch_jaguar2.log."
        except MissingTool as e:
            return "failed", f"Precisa do perfil Jaguar 2 pronto antes. {e.message}"
    plans = _plans(home, *step["plan"])
    base = 0
    for d in plans:
        def progress(done, total, base=base):
            task["bytes"] = base + done
            task["total"] = total if len(plans) == 1 else None
        fetch(d, progress=progress)
        base = task["bytes"]
    return "ok", str(plans[0].dest)


def _work(task: dict, steps: list[dict], home, settings) -> None:
    try:
        for step in steps:
            task["current"] = step["label"]
            task["bytes"], task["total"] = 0, None
            _set_step(task, step["id"], "running")
            try:
                state, msg = _run_step(step, home, settings, task)
            except DownloadError as e:
                state, msg = "failed", str(e)
            except Exception as e:        # um passo não derruba os demais
                state, msg = "failed", f"Erro inesperado: {e}"
            _set_step(task, step["id"], state, msg)
        task["current"] = None
        task["failed"] = sum(1 for s in task["steps"] if s["state"] == "failed")
        if not task["failed"]:
            gravar_estado_ui(home, assistente_dispensado=True)
    except Exception as e:                # a thread não pode morrer calada
        task["error"] = f"Erro inesperado: {e}"
    finally:
        task["done"] = True


def _progress_page(request: Request, tid: str):
    return _render(request, "_bem_vindo_progresso.html", {"tid": tid, "t": request.app.state.tasks[tid]})


@router.post("/bem-vindo/preparar", response_class=HTMLResponse)
def bem_vindo_preparar(request: Request, escopo: str = Form("tudo")):
    st = request.app.state
    escopo = _escopo(escopo)
    tid, task, new = _new_task(request, "Preparando o ambiente", "bem-vindo")
    if not new:
        return _progress_page(request, tid)
    steps = _steps_do_escopo(escopo)
    task.update(current=None, failed=0,
                steps=[{"id": s["id"], "label": s["label"], "state": "pending", "msg": ""} for s in steps])
    threading.Thread(target=_work, args=(task, steps, st.home, st.settings),
                     name=f"jflp-bv-{tid}", daemon=True).start()
    return _progress_page(request, tid)


@router.get("/bem-vindo/progresso/{tid}", response_class=HTMLResponse)
def bem_vindo_progresso(request: Request, tid: str):
    if tid not in request.app.state.tasks or "steps" not in request.app.state.tasks[tid]:
        return HTMLResponse("<p class='erro'>Tarefa não encontrada.</p>", status_code=404)
    return _progress_page(request, tid)


@router.post("/bem-vindo/dispensar")
def bem_vindo_dispensar(request: Request):
    gravar_estado_ui(request.app.state.home, assistente_dispensado=True)
    return RedirectResponse("/", status_code=303)
