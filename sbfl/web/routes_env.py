"""Tela Ambiente: ferramentas detectadas, downloads e pastas manuais."""
from __future__ import annotations

import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import planilha, sincronia
from ..run_jaguar2 import prefetch_jaguar2
from ..proc import NO_WINDOW
from ..toolchain.detect import clear_cache, find_jdks, find_mavens, java_info, maven_info, wsl_status
from ..toolchain.download import DownloadError, fetch, plan_jaguar1, plan_jdk, plan_maven
from ..toolchain.profiles import PROFILES, MissingTool, _pick_maven, resolve
from .routes_cobertura import _ha

router = APIRouter()

JDK_OPTIONS = (8, 11, 17)
MAVEN_OPTIONS = ("3.6.3", "3.9.9")
# perfil legado da cobertura (JDK 7 + Maven 3.0.5): só sob demanda, nunca como padrão de um perfil
JDK_LEGADO = (7,)
MAVEN_LEGADO = ("3.0.5",)
WORKERS_MAX = 12
ESPACO_MAX_GB = 500
TENTATIVAS_MAX = 8
KIND_LABEL = {"jaguar2": "Jaguar 2", "jaguar1": "Jaguar 1", "maven": "Maven puro"}
# versão de Maven oferecida no botão quando falta um Maven para o perfil
MAVEN_DOWNLOAD = {"maven": "3.6.3", "jaguar1": "3.6.3", "jaguar2": "3.9.9"}


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


_GIT_CACHE: dict[str, tuple[float, str]] = {}    # exe -> (validade, texto)
_GIT_TTL = 60.0


def _git() -> str | None:
    exe = shutil.which("git")
    if not exe:
        return None
    hit = _GIT_CACHE.get(exe)
    if hit and time.monotonic() < hit[0]:
        return hit[1]
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, timeout=20, text=True,
                             stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        txt = out.stdout.strip() or exe
    except (OSError, subprocess.SubprocessError):
        return exe      # falha não fica guardada
    _GIT_CACHE[exe] = (time.monotonic() + _GIT_TTL, txt)
    return txt


def clear_git_cache() -> None:
    _GIT_CACHE.clear()


def _botao(tipo: str, v: str = "") -> dict:
    if tipo == "jdk":
        label = f"Baixar JDK {v}"
    elif tipo == "maven":
        label = f"Baixar Maven {v}"
    else:
        label = "Baixar Jaguar 1 (2 jars)"
    url = f"/ambiente/plano?tipo={tipo}" + (f"&v={v}" if v else "")
    return {"label": label, "url": url}


def _faltas(home, settings, name: str) -> list[dict]:
    """O que falta ao perfil, cada item com os botões de download do que resolve."""
    spec = PROFILES[name]
    jdks = find_jdks(home, settings)
    faltas: list[dict] = []
    if spec["jdk"] is not None:
        if not any(j.major == spec["jdk"] for j in jdks):
            faltas.append({"texto": f"JDK {spec['jdk']}", "botoes": [_botao("jdk", str(spec["jdk"]))]})
    elif not jdks:
        faltas.append({"texto": "JDK", "botoes": [_botao("jdk", str(v)) for v in JDK_OPTIONS]})
    try:
        _pick_maven(spec, find_mavens(home, settings), None)
    except MissingTool as e:
        faltas.append({"texto": f"Maven {e.wanted}", "botoes": [_botao("maven", MAVEN_DOWNLOAD[name])]})
    if name == "jaguar1" and not (jaguar1_lib(home, settings) / "jacocoagent.jar").is_file():
        faltas.append({"texto": "pasta do Jaguar 1 (jacocoagent.jar)", "botoes": [_botao("jaguar1")]})
    return faltas


def _cartoes(home, settings) -> list[dict]:
    """Um cartão por perfil: pronto (com resumo) ou o que falta e os botões."""
    cartoes = []
    for name in ("maven", "jaguar1", "jaguar2"):
        faltas = _faltas(home, settings, name)
        resumo = ""
        if not faltas:
            try:
                te = resolve(name, home, settings)
                resumo = f"JDK {te.jdk.major} · Maven {_vstr(te.maven.version)}"
            except MissingTool as e:
                faltas.append({"texto": e.message, "botoes": []})
        cartoes.append({"nome": KIND_LABEL[name], "ok": not faltas, "resumo": resumo,
                        "faltas": faltas, "offline": name == "jaguar2"})
    return cartoes


def _estado_planilha(s: dict | None) -> tuple[str, str]:
    """(texto, classe) da linha de situação do envio à planilha."""
    if s is None:
        return "O envio automático não está rodando nesta sessão.", "vazio"
    if not s.get("configurada"):
        return "Sem URL configurada: nada é enviado para a planilha.", "vazio"
    pend = s.get("pendentes", 0)
    if s.get("erro"):
        return f"Falha no envio: {s['erro']} · {pend} pendentes (tenta de novo sozinho).", "erro"
    ultimo = f"há {_ha(s['ultimo_ok'])}" if s.get("ultimo_ok") else "nenhum ainda"
    texto = f"Último envio: {ultimo} · {pend} pendentes"
    if s.get("falhas_linha"):
        texto += f" · {s['falhas_linha']} linhas não puderam ser montadas ({s.get('erro_linha') or 'ver o log'})"
        return texto, "erro"
    return texto, "vazio"


def _estado_sincronia(s: dict | None, ativa: bool) -> dict:
    """Linhas do bloco "Sincronização entre computadores" (sem o sincronizador, só diz que não está rodando)."""
    if s is None:
        return {"rodando": False, "texto": "A sincronização não está rodando nesta sessão.", "classe": "vazio"}
    if not ativa:
        return {"rodando": True, "texto": "Sincronização desligada (ou sem URL da planilha).", "classe": "vazio"}
    ultimo = f"há {_ha(s['ultimo_puxar'])}" if s.get("ultimo_puxar") else "nenhum ainda"
    texto = (f"Último puxar: {ultimo} · {s.get('aplicadas', 0)} execuções recebidas · "
             f"{s.get('pacotes_pendentes', 0)} pacotes a enviar · "
             f"{s.get('reservas_remotas', 0)} bugs reservados por outros computadores")
    if s.get("hub_antigo"):
        return {"rodando": True, "texto": texto, "classe": "erro", "hub_antigo": True,
                "aviso": sincronia.AVISO_ANTIGO}
    if s.get("erro"):
        return {"rodando": True, "texto": texto + f" · erro: {s['erro']}", "classe": "erro"}
    return {"rodando": True, "texto": texto, "classe": "vazio"}


def _planilha_ctx(request: Request) -> dict:
    st = request.app.state
    env = getattr(st, "enviador", None)
    s = env.status() if env is not None else None
    texto, classe = _estado_planilha(s)
    sinc = getattr(st, "sincronizador", None)
    return {"url": st.settings.get("planilha", "url", "") or "",
            "token": planilha.garantir_token(st.settings),
            "linhas": planilha.linhas_total(st.home), "estado": texto, "classe": classe,
            "maquina_nome": st.settings.get("planilha", "maquina_nome", "") or "",
            "maquina_padrao": sincronia.maquina(st.settings)[1],
            "sincronizar": bool(st.settings.get("planilha", "sincronizar", True)),
            "sinc": _estado_sincronia(sinc.status() if sinc is not None else None, sincronia.ativa(st.settings))}


def _cobertura_ctx(request: Request) -> dict:
    st = request.app.state
    cob = st.settings.section("cobertura")
    return {"workers": cob.get("workers", 4), "work_dir": cob.get("work_dir", "") or "",
            "legado": bool(cob.get("legado", True)), "legado_offline": bool(cob.get("legado_offline", True)),
            "min_livre_gb": cob.get("min_livre_gb", 5),
            "max_tentativas_jaguar": cob.get("max_tentativas_jaguar", 4),
            "paralelo_j1": bool(st.settings.get("jaguar1", "port_aware", False)),
            "em_uso": str(st.home.work),          # a pasta que a execução usa agora (já com o padrão aplicado)
            "aviso": getattr(st, "aviso_work_dir", None)}


def _env_context(request: Request, msg: str | None = None, msg_ok: bool = True) -> dict:
    st = request.app.state
    home, settings = st.home, st.settings
    lib = jaguar1_lib(home, settings)
    return {
        "page": "ambiente", "msg": msg, "msg_ok": msg_ok, "msgs": {}, "erros": {},
        "jdks": find_jdks(home, settings),
        "mavens": [(m, _vstr(m.version)) for m in find_mavens(home, settings)],
        "git": _git(),
        "wsl": wsl_status(settings),
        "jaguar1_lib": lib, "jaguar1_ok": (lib / "jacocoagent.jar").is_file(),
        "cartoes": _cartoes(home, settings),
        "profiles": _profiles(home, settings),
        "jdk_options": JDK_OPTIONS, "maven_options": MAVEN_OPTIONS,
        "jdk_legado": JDK_LEGADO, "maven_legado": MAVEN_LEGADO,
        "planilha": _planilha_ctx(request), "cobertura": _cobertura_ctx(request),
        "workers_max": WORKERS_MAX, "tentativas_max": TENTATIVAS_MAX, "espaco_max_gb": ESPACO_MAX_GB,
    }


SECOES = ("planilha", "cobertura")


@router.get("/ambiente", response_class=HTMLResponse)
def ambiente(request: Request, msg: str = "", secao: str = ""):
    ctx = _env_context(request)
    if msg and secao in SECOES:
        ctx["msgs"] = {secao: msg}
    return _render(request, "ambiente.html", ctx)


def _plans(home, tipo: str, v: str):
    if tipo == "jdk":
        if v not in {str(x) for x in JDK_OPTIONS + JDK_LEGADO}:
            raise DownloadError(f"Versão de JDK não oferecida: {v}")
        return [plan_jdk(home, int(v))]
    if tipo == "maven":
        if v not in MAVEN_OPTIONS + MAVEN_LEGADO:
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
            clear_cache()
            clear_git_cache()
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
            clear_cache()
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


def _volta(secao: str, msg: str) -> RedirectResponse:
    """PRG: depois de salvar, volta à seção com a mensagem na própria seção."""
    return RedirectResponse("/ambiente?" + urlencode({"secao": secao, "msg": msg}) + f"#{secao}", status_code=303)


def _erro_ambiente(request: Request, secao: str, erro: str, valores: dict) -> HTMLResponse:
    """Re-renderiza a seção com o erro e os valores digitados (não se perde o que foi escrito)."""
    ctx = _env_context(request)
    ctx["erros"] = {secao: erro}
    ctx[secao] = {**ctx[secao], **valores}
    return _render(request, "ambiente.html", ctx, 400)


@router.post("/ambiente/planilha", response_class=HTMLResponse)
def salvar_planilha(request: Request, url: str = Form("")):
    st = request.app.state
    url = url.strip()
    if url and not url.lower().startswith("https://"):
        return _erro_ambiente(request, "planilha", "A URL do app da web precisa começar com https://.", {"url": url})
    planilha.salvar_url(st.settings, url)
    env = getattr(st, "enviador", None)
    if env is not None:
        env.acordar()
    return _volta("planilha", "URL da planilha salva." if url else "URL da planilha removida.")


@router.post("/ambiente/planilha/maquina", response_class=HTMLResponse)
def salvar_maquina(request: Request, maquina_nome: str = Form(""), sincronizar: str = Form("")):
    """Nome deste computador e liga/desliga da sincronização com os outros."""
    st = request.app.state
    st.settings.set("planilha", "maquina_nome", " ".join(maquina_nome.split())[:60])
    st.settings.set("planilha", "sincronizar", bool(sincronizar))
    st.settings.save()
    sinc = getattr(st, "sincronizador", None)
    if sinc is not None:
        sinc.acordar(forcar=True)
    return _volta("planilha", "Configuração de sincronização salva.")


@router.post("/ambiente/planilha/sincronizar", response_class=HTMLResponse)
def sincronizar_agora(request: Request):
    """Puxa e envia agora, sem esperar o intervalo."""
    sinc = getattr(request.app.state, "sincronizador", None)
    if sinc is None:
        return _volta("planilha", "A sincronização não está rodando nesta sessão.")
    sinc.acordar(forcar=True)
    return _volta("planilha", "Sincronizando com os outros computadores…")


@router.post("/ambiente/planilha/reenviar", response_class=HTMLResponse)
def reenviar_planilha(request: Request):
    """Reenvio completo: todas as linhas da cobertura voltam à fila e o envio tenta já, sem esperar a pausa."""
    st = request.app.state
    n = planilha.enfileirar_todos(st.home)
    env = getattr(st, "enviador", None)
    if env is not None:
        env.acordar(forcar=True)
        msg = f"{n} linhas voltaram para a fila de envio."
    else:
        msg = f"{n} linhas marcadas para envio, mas o envio automático não está rodando nesta sessão."
    return _volta("planilha", msg)


@router.post("/ambiente/cobertura", response_class=HTMLResponse)
def salvar_cobertura(request: Request, workers: str = Form("4"), work_dir: str = Form(""),
                     legado: bool = Form(False), legado_offline: bool = Form(False),
                     min_livre_gb: str = Form("5"), jaguar1_paralelo: bool = Form(False),
                     max_tentativas_jaguar: str | None = Form(None)):
    st = request.app.state
    if max_tentativas_jaguar is None:     # formulário sem o campo: mantém o valor atual
        max_tentativas_jaguar = str(st.settings.get("cobertura", "max_tentativas_jaguar", 4))
    valores = {"max_tentativas_jaguar": max_tentativas_jaguar, "workers": workers, "work_dir": work_dir, "legado": legado, "legado_offline": legado_offline,
               "min_livre_gb": min_livre_gb, "paralelo_j1": jaguar1_paralelo}
    try:
        n = int(workers.strip())
    except ValueError:
        n = 0
    if not 1 <= n <= WORKERS_MAX:
        return _erro_ambiente(request, "cobertura", f"Os workers vão de 1 a {WORKERS_MAX}: digite um número inteiro.",
                              valores)
    try:
        gb = int(min_livre_gb.strip())
    except ValueError:
        gb = 0
    if not 1 <= gb <= ESPACO_MAX_GB:
        return _erro_ambiente(request, "cobertura",
                              f"O espaço mínimo vai de 1 a {ESPACO_MAX_GB} GB: digite um número inteiro.", valores)
    try:
        tent = int(max_tentativas_jaguar.strip())
    except ValueError:
        tent = 0
    if not 1 <= tent <= TENTATIVAS_MAX:
        return _erro_ambiente(request, "cobertura", f"As tentativas por bug vão de 1 a {TENTATIVAS_MAX}: "
                                                    "digite um número inteiro.", valores)
    wd = work_dir.strip().strip('"')
    if wd and not Path(wd).is_absolute():
        return _erro_ambiente(request, "cobertura", "A pasta de trabalho precisa de um caminho absoluto.", valores)
    settings = st.settings
    settings.set("cobertura", "workers", n)
    settings.set("cobertura", "work_dir", wd)
    settings.set("cobertura", "legado", bool(legado))
    settings.set("cobertura", "legado_offline", bool(legado_offline))
    settings.set("cobertura", "min_livre_gb", gb)
    settings.set("cobertura", "max_tentativas_jaguar", tent)
    settings.set("jaguar1", "port_aware", bool(jaguar1_paralelo))
    settings.save()
    return _volta("cobertura", "Configuração salva. Workers e pasta de trabalho valem depois de reiniciar "
                               "o JFLP Studio.")


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
        clear_cache()
    return _render(request, "ambiente.html", _env_context(request, msg, ok), 200 if ok else 400)
