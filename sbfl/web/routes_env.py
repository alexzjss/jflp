"""Tela Ambiente: ferramentas detectadas, downloads e pastas manuais."""
from __future__ import annotations

import shutil
import subprocess
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ..run_jaguar2 import prefetch_jaguar2
from ..toolchain.detect import find_jdks, find_mavens, java_info, maven_info, wsl_status
from ..toolchain.download import DownloadError, fetch, plan_jaguar1, plan_jdk, plan_maven
from ..toolchain.profiles import PROFILES, MissingTool, resolve

router = APIRouter()

JDK_OPTIONS = (8, 11, 17)
MAVEN_OPTIONS = ("3.6.3", "3.9.9")
KIND_LABEL = {"jaguar2": "Jaguar 2", "jaguar1": "Jaguar 1", "maven": "Maven puro"}


def _vstr(v) -> str:
    return ".".join(str(n) for n in v)


def _mb(n) -> str:
    return f"{n / 1048576:.1f} MB" if n else "desconhecido"


def _render(request: Request, name: str, ctx: dict, status: int = 200):
    return request.app.state.templates.TemplateResponse(request, name, ctx, status_code=status)


def jaguar1_lib(home, settings) -> Path:
    return Path(settings.get("jaguar1", "lib") or (home.tools / "jaguar1" / "lib"))


def _profiles(home, settings) -> list[dict]:
    rows = []
    for name in PROFILES:
        try:
            te = resolve(name, home, settings)
            text = (f"JDK {te.jdk.major} ({te.jdk.home}), "
                    f"Maven {_vstr(te.maven.version)} ({te.maven.home})")
            rows.append({"name": KIND_LABEL.get(name, name), "ok": True, "text": text})
        except MissingTool as e:
            rows.append({"name": KIND_LABEL.get(name, name), "ok": False, "text": e.message})
    return rows


def _git() -> str | None:
    exe = shutil.which("git")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, timeout=20, text=True)
        return out.stdout.strip() or exe
    except (OSError, subprocess.SubprocessError):
        return exe


def _env_context(request: Request, msg: str | None = None, msg_ok: bool = True) -> dict:
    st = request.app.state
    home, settings = st.home, st.settings
    lib = jaguar1_lib(home, settings)
    return {
        "page": "ambiente", "msg": msg, "msg_ok": msg_ok,
        "jdks": find_jdks(home, settings),
        "mavens": [(m, _vstr(m.version)) for m in find_mavens(home, settings)],
        "git": _git(),
        "wsl": wsl_status(settings),
        "jaguar1_lib": lib, "jaguar1_ok": (lib / "jacocoagent.jar").is_file(),
        "profiles": _profiles(home, settings),
        "jdk_options": JDK_OPTIONS, "maven_options": MAVEN_OPTIONS,
    }


@router.get("/ambiente", response_class=HTMLResponse)
def ambiente(request: Request):
    return _render(request, "ambiente.html", _env_context(request))


def _plans(home, tipo: str, v: str):
    if tipo == "jdk":
        if v not in {str(x) for x in JDK_OPTIONS}:
            raise DownloadError(f"Versão de JDK não oferecida: {v}")
        return [plan_jdk(home, int(v))]
    if tipo == "maven":
        if v not in MAVEN_OPTIONS:
            raise DownloadError(f"Versão de Maven não oferecida: {v}")
        return [plan_maven(home, v)]
    if tipo == "jaguar1":
        return plan_jaguar1(home)
    raise DownloadError(f"Tipo desconhecido: {tipo}")


@router.get("/ambiente/plano", response_class=HTMLResponse)
def plano(request: Request, tipo: str, v: str = ""):
    ctx = {"error": None, "plans": [], "tipo": tipo, "v": v}
    try:
        plans = _plans(request.app.state.home, tipo, v)
    except DownloadError as e:
        ctx["error"] = str(e)
        return _render(request, "_plano.html", ctx)
    ctx["plans"] = [{"name": d.name, "url": d.url, "size": _mb(d.size),
                     "checksum": f"{d.algo}: {d.checksum}" if d.checksum else "não publicado",
                     "dest": d.dest} for d in plans]
    return _render(request, "_plano.html", ctx)


_LOCK = threading.Lock()


def _new_task(request: Request, label: str, key: str) -> tuple[str, dict, bool]:
    """Cria a tarefa, ou devolve a que já roda para a mesma `key` (False = reaproveitada)."""
    tasks = request.app.state.tasks
    with _LOCK:
        for tid, t in tasks.items():
            if t.get("key") == key and not t["done"]:
                return tid, t, False
        tid = uuid.uuid4().hex[:12]
        task = {"label": label, "key": key, "done": False, "error": None, "bytes": 0,
                "total": None, "dest": None}
        tasks[tid] = task
        return tid, task, True


def _progress_page(request: Request, tid: str):
    return _render(request, "_progresso.html", {"tid": tid, "t": request.app.state.tasks[tid]})


@router.post("/ambiente/baixar", response_class=HTMLResponse)
def baixar(request: Request, tipo: str = Form(...), v: str = Form("")):
    home = request.app.state.home
    tid, task, new = _new_task(request, f"Baixando {tipo} {v}".strip(), f"baixar:{tipo}:{v}")
    if not new:
        return _progress_page(request, tid)

    def work():
        base = 0
        try:
            plans = _plans(home, tipo, v)
            for d in plans:
                def progress(done, total, base=base):
                    task["bytes"] = base + done
                    task["total"] = total if len(plans) == 1 else None
                fetch(d, progress=progress)
                base = task["bytes"]
                task["dest"] = str(d.dest)
        except DownloadError as e:
            task["error"] = str(e)
        except Exception as e:   # a thread não pode morrer calada
            task["error"] = f"Erro inesperado: {e}"
        finally:
            task["done"] = True

    threading.Thread(target=work, name=f"jflp-dl-{tid}", daemon=True).start()
    return _progress_page(request, tid)


@router.post("/ambiente/prefetch", response_class=HTMLResponse)
def prefetch(request: Request):
    st = request.app.state
    tid, task, new = _new_task(request, "Preparando o Jaguar 2 para uso offline", "prefetch")
    if not new:
        return _progress_page(request, tid)

    def work():
        try:
            if prefetch_jaguar2(st.home, st.settings):
                task["dest"] = str(st.home.m2)
            else:
                task["error"] = "Algumas dependências não foram baixadas; veja work/prefetch_jaguar2.log."
        except MissingTool as e:
            task["error"] = e.message
        except Exception as e:
            task["error"] = f"Erro inesperado: {e}"
        finally:
            task["done"] = True

    threading.Thread(target=work, name=f"jflp-prefetch-{tid}", daemon=True).start()
    return _progress_page(request, tid)


@router.get("/ambiente/progresso/{tid}", response_class=HTMLResponse)
def progresso(request: Request, tid: str):
    if tid not in request.app.state.tasks:
        return HTMLResponse("<p class='erro'>Tarefa não encontrada.</p>", status_code=404)
    return _progress_page(request, tid)


def _looks_like_jdk(p: Path) -> bool:
    return (p / "release").is_file() or (p / "lib" / "rt.jar").is_file() or (p / "lib" / "modules").is_file()


def _looks_like_maven(p: Path) -> bool:
    return (p / "boot").is_dir() and (p / "conf" / "settings.xml").is_file()


@router.post("/ambiente/apontar", response_class=HTMLResponse)
def apontar(request: Request, kind: str = Form(...), path: str = Form(...)):
    settings = request.app.state.settings
    folder = Path(path.strip().strip('"'))
    msg, ok = None, False
    if not folder.is_absolute():
        msg = "Informe o caminho absoluto da pasta."
    elif not folder.is_dir():
        msg = f"A pasta não existe: {folder}"
    elif kind == "jdk":
        info = java_info(folder) if _looks_like_jdk(folder) else None
        if info is None:
            msg = "Não é um JDK válido: faltam release, lib/rt.jar ou lib/modules, ou o java não executa."
        else:
            jdks = dict(settings.get("tools", "jdks", {}) or {})
            jdks[str(info[0])] = str(folder)
            settings.set("tools", "jdks", jdks)
            msg, ok = f"JDK {info[0]} ({info[1]}) registrado.", True
    elif kind == "maven":
        ver = maven_info(folder) if _looks_like_maven(folder) else None
        if ver is None:
            msg = "Não é um Maven válido: faltam boot/ e conf/settings.xml, ou o mvn não executa."
        else:
            mavens = dict(settings.get("tools", "mavens", {}) or {})
            mavens[_vstr(ver)] = str(folder)
            settings.set("tools", "mavens", mavens)
            msg, ok = f"Maven {_vstr(ver)} registrado.", True
    elif kind == "jaguar1":
        if not (folder / "jacocoagent.jar").is_file():
            msg = "Não é uma pasta lib do Jaguar 1: falta o jacocoagent.jar."
        else:
            settings.set("jaguar1", "lib", str(folder))
            msg, ok = "Pasta do Jaguar 1 registrada.", True
    else:
        msg = f"Tipo desconhecido: {kind}"
    if ok:
        settings.save()
    return _render(request, "ambiente.html", _env_context(request, msg, ok), 200 if ok else 400)
