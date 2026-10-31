"""Tela Cobertura (painel): o lote único Maven + Jaguar 1 + Jaguar 2 sobre todos os bugs do Bugs.jar."""
from __future__ import annotations

import contextlib
import json
import math
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import bugview as bv
from .. import cobertura as cb
from .. import planilha
from .. import queue as jq
from ..importar import ORIGEM
from .comum import _render
from .routes_lote import _codigo_rel

router = APIRouter()

CORES = (("ok", "Concluído"), ("parcial", "Parcial"), ("falhou", "Falhou"), ("nao_executavel", "Não executável"),
         ("rodando", "Rodando"), ("pendente", "Pendente"))
ROTULO_KIND = {"maven": "Maven", "jaguar1": "Jaguar 1", "jaguar2": "Jaguar 2"}
CONCLUIDAS = ("ok", "parcial", "falhou", "nao_executavel")


def _ram_gb() -> float | None:
    """RAM física total em GB (Windows via GlobalMemoryStatusEx); None se não der para medir."""
    try:
        import ctypes

        class _Mem(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = _Mem()
        m.dwLength = ctypes.sizeof(_Mem)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):    # type: ignore[attr-defined]
            return None
        return m.ullTotalPhys / 1024 ** 3
    except (AttributeError, OSError, ImportError):
        return None


def dica_maquina() -> str:
    """Texto de orientação sobre quantos workers cabem nesta máquina."""
    nucleos = os.cpu_count() or 2
    ram = _ram_gb()
    rec = max(1, nucleos // 2)
    if ram is not None:
        rec = max(1, min(rec, int(ram) // 3))
    ate = min(rec + 2, nucleos)
    maq = f"Esta máquina: {nucleos} núcleos" + (f", {round(ram)} GB de RAM" if ram is not None else "")
    extra = f"; até {ate} se a RAM livre aguentar" if ate > rec else ""
    return f"{maq}. Cada worker usa ~1–2 GB (Maven/JVM). Recomendado: {rec}{extra}."


def tem_cobertura(request: Request) -> bool:
    """Há lote de cobertura ou uma pasta do Bugs.jar configurada (a tela inicial passa a ser a Cobertura)."""
    st = request.app.state
    return cb.batch_id(st.home) is not None or bool(st.settings.get("bugsjar", "root", ""))


def _duracao(segundos: float) -> str:
    s = int(max(segundos, 0))
    if s >= 3600:
        return f"{s // 3600} h {s % 3600 // 60:02d} min"
    return f"{s // 60} min {s % 60:02d} s" if s >= 60 else f"{s} s"


def _ha(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return _duracao((datetime.now() - datetime.fromisoformat(iso)).total_seconds())
    except ValueError:
        return "—"


def _selo(request: Request, bid: int, correntes: list[dict], outra: bool) -> tuple[str, str, str]:
    """(texto, classe, estado do lote)."""
    st = request.app.state.worker.status()
    lote = (jq.get_batch(request.app.state.home, bid) or {}).get("state", "")
    if outra or not st.get("alive") or lote == "cancelled":
        return "Parado", "parado", lote
    if lote == "done":
        return "Concluída", "concluida", lote
    if lote == "paused":
        return ("Pausando…", "pausando", lote) if correntes else ("Pausado", "pausado", lote)
    return "Rodando", "rodando", lote


def _planilha(request: Request) -> dict | None:
    env = getattr(request.app.state, "enviador", None)
    if env is None:
        return None
    s = env.status()
    if not s.get("configurada"):
        return {"tipo": "nao_configurada"}
    falhas = int(s.get("falhas_linha") or 0)
    ultimo = s.get("ultimo_ok")
    if s.get("erro"):
        return {"tipo": "erro", "pendentes": s.get("pendentes", 0), "erro": s["erro"], "ha": _ha(ultimo), "falhas": falhas}
    # ha None: nenhuma linha foi enviada ainda (o painel diz "ainda não enviada", sem "há —")
    return {"tipo": "ok", "ha": _ha(ultimo) if ultimo else None,
            "linhas": planilha.linhas_total(request.app.state.home), "falhas": falhas}


def painel_ctx(request: Request, bid: int) -> dict:
    st = request.app.state
    home = st.home
    r = cb.painel(home, bid)
    wstatus = st.worker.status()
    correntes = st.worker.currents()
    agora = datetime.now().timestamp()
    rodando = []
    for c in correntes:
        b = r["rodando"].get(c["bug"], {})
        etapas = b.get("etapas", {})
        chips = [{"nome": ROTULO_KIND[k], "estado": "rodando" if k == c["kind"] else etapas.get(k, "pendente")} for k in cb.KINDS]
        rodando.append({"bug": c["bug"].split("/", 2)[-1], "projeto": b.get("projeto") or (c["bug"].split("/")[1:2] or [""])[0],
                        "tempo": _duracao(agora - c.get("started", agora)), "chips": chips})
    ultimos = [{"bug": u["bug"], "projeto": u["projeto"], "situacao": u["situacao"],
                "quando": _ha(u["fim"].isoformat()) if u["fim"] else ""} for u in r["ultimos"]]
    total = r["total"]
    pct = int(r["concluidos"] * 100 / total) if total else 0
    outra = bool(wstatus.get("other_instance"))
    selo, selo_cls, lote_estado = _selo(request, bid, correntes, outra)
    ritmo = restante = None
    if r["ritmo_h"]:
        ritmo = f"{r['ritmo_h']:.1f}".replace(".", ",")
        if r["restante_s"] is not None:
            restante = f"{r['restante_s'] / 3600:.1f}".replace(".", ",")
    n_workers = wstatus.get("workers") or len(correntes) or 1
    ativos = wstatus.get("workers_ativos")
    kinds = st.settings.get("cobertura", "kinds", None)
    kinds = [k for k in cb.KINDS if k in kinds] if isinstance(kinds, list) else []
    return {"etapas": [{"k": k, "nome": ROTULO_KIND[k], "on": k in (kinds or cb.KINDS)} for k in cb.KINDS],
            "workers_max": jq.WORKERS_MAX, "workers_ativos": ativos, "dica_maquina": dica_maquina(),
            "diminuindo": isinstance(ativos, int) and ativos > n_workers,
            "r": r, "pct": pct, "selo": selo, "selo_cls": selo_cls, "pausado": lote_estado == "paused",
            "concluida": lote_estado == "done", "cancelada": lote_estado == "cancelled", "outra_instancia": outra,
            "rodando": rodando, "ultimos": ultimos, "cores": CORES,
            "n_falhados": r["por_situacao"]["falhou"] + r["por_situacao"]["parcial"],
            "ritmo": ritmo, "restante": restante, "n_workers": n_workers, "planilha": _planilha(request),
            "aviso_disco": wstatus.get("aviso_disco")}


@router.get("/cobertura", response_class=HTMLResponse)
def cobertura_pagina(request: Request, aviso: str = ""):
    st = request.app.state
    bid = cb.batch_id(st.home)
    if bid is None:
        return _render(request, "cobertura.html", {"page": "cobertura", "bid": None, "erro": "",
                       "pasta": st.settings.get("bugsjar", "root", "") or ""})
    return _render(request, "cobertura.html", {"page": "cobertura", "bid": bid, "erro": "",
                   **painel_ctx(request, bid),
                   "aviso_etapas": "Deixe ao menos uma etapa marcada." if aviso == "etapas" else ""})


@router.get("/cobertura/painel", response_class=HTMLResponse)
def cobertura_painel(request: Request):
    bid = cb.batch_id(request.app.state.home)
    if bid is None:
        return HTMLResponse("")
    return _render(request, "_cobertura_painel.html", painel_ctx(request, bid))


@router.post("/cobertura/iniciar")
def cobertura_iniciar(request: Request, pasta: str = Form("")):
    st = request.app.state
    pasta = pasta.strip().strip('"')
    if not pasta or not Path(pasta).is_dir():
        return _render(request, "cobertura.html", {"page": "cobertura", "bid": None, "pasta": pasta,
                       "erro": "Informe a pasta com os repositórios do Bugs.jar."}, 400)
    r = cb.sincronizar(st.home, st.settings, Path(pasta))
    if not r["total_bugs"]:
        return _render(request, "cobertura.html", {"page": "cobertura", "bid": None, "pasta": pasta,
                       "erro": "Nenhum bug do Bugs.jar encontrado nessa pasta. " + " ".join(r["avisos"])}, 400)
    if not st.settings.get("bugsjar", "root", ""):
        st.settings.set("bugsjar", "root", pasta)
        st.settings.save()
    return RedirectResponse("/cobertura", status_code=303)


def _voltar() -> RedirectResponse:
    return RedirectResponse("/cobertura", status_code=303)


@router.post("/cobertura/workers")
def cobertura_workers(request: Request, workers: str = Form("")):
    """Muda o número de workers na hora (sem reiniciar) e guarda a escolha."""
    st = request.app.state
    try:
        n = int(workers.strip())
    except ValueError:
        return _voltar()
    n = max(1, min(jq.WORKERS_MAX, n))
    st.worker.set_workers(n)
    st.settings.set("cobertura", "workers", n)
    st.settings.save()
    return _voltar()


@router.post("/cobertura/etapas")
def cobertura_etapas(request: Request, etapas: list[str] = Form([])):
    """Escolhe quais etapas a cobertura roda (vale na próxima reivindicação, sem reiniciar) e guarda."""
    st = request.app.state
    ligadas = [k for k in cb.KINDS if k in etapas]
    if not ligadas:
        return RedirectResponse("/cobertura?aviso=etapas", status_code=303)
    st.settings.set("cobertura", "kinds", ligadas)
    st.settings.save()
    return _voltar()


@router.post("/cobertura/pausar")
def cobertura_pausar(request: Request):
    bid = cb.batch_id(request.app.state.home)
    if bid is not None:
        request.app.state.worker.pause(bid)
    return _voltar()


@router.post("/cobertura/continuar")
def cobertura_continuar(request: Request):
    bid = cb.batch_id(request.app.state.home)
    if bid is not None:
        request.app.state.worker.resume(bid)
    return _voltar()


@router.post("/cobertura/parar")
def cobertura_parar(request: Request):
    request.app.state.worker.interromper()
    return _voltar()


@router.post("/cobertura/refazer-falhados")
def cobertura_refazer(request: Request):
    st = request.app.state
    bid = cb.batch_id(st.home)
    if bid is not None:
        falhados = [b["key"] for b in cb.tabela(st.home, bid) if b["situacao"] in (cb.SIT_FALHOU, cb.SIT_PARCIAL)]
        if cb.refazer(st.home, bid) and falhados:
            planilha.enfileirar(st.home, falhados)
            env = getattr(st, "enviador", None)
            if env is not None:
                env.acordar()
    return _voltar()


def _pasta_do_lote(home, bid: int | None) -> str:
    """Pasta do Bugs.jar gravada no lote de cobertura (meta.root); "" se não houver."""
    if bid is None:
        return ""
    with contextlib.closing(cb._con(home)) as con:
        row = con.execute("SELECT meta FROM batches WHERE id=?", (bid,)).fetchone()
    try:
        return str((json.loads(row["meta"] or "{}") if row else {}).get("root") or "")
    except ValueError:
        return ""


@router.post("/cobertura/sincronizar")
def cobertura_sincronizar(request: Request):
    st = request.app.state
    candidatas = (_pasta_do_lote(st.home, cb.batch_id(st.home)), st.settings.get("bugsjar", "root", "") or "")
    pasta = next((p for p in candidatas if p and Path(p).is_dir()), "")
    if pasta:
        cb.sincronizar(st.home, st.settings, Path(pasta))
    return _voltar()


# ------------------------------------------------------------------ tabela dos bugs

PAGINA = 50
ACOES = ("refazer", "nao_executavel", "priorizar")
FILTROS = (("", "Todos"), (cb.SIT_PENDENTE, "Pendentes"), (cb.SIT_RODANDO, "Rodando"), (cb.SIT_OK, "OK"),
           (cb.SIT_PARCIAL, "Parcial"), (cb.SIT_FALHOU, "Falharam"), (cb.SIT_NAO_EXEC, "Não executáveis"))
SITUACOES = {s for s, _ in FILTROS if s}


def _href(filtros: dict, pagina: int = 1, aviso: str = "") -> str:
    """/cobertura/bugs com os filtros não vazios (e a página, se não for a primeira)."""
    qs = {k: v for k, v in filtros.items() if v}
    if pagina > 1:
        qs["pagina"] = pagina
    if aviso:
        qs["aviso"] = aviso
    return "/cobertura/bugs" + ("?" + urlencode(qs) if qs else "")


def _rotulo(situacao: str) -> str:
    return dict(CORES).get(situacao, situacao)


def _maven(bug: dict, runs: dict) -> str:
    if "maven" not in bug["jobs"]:
        return "—"
    tests = (runs.get("maven") or {}).get("tests") or {}
    if tests.get("total") is not None and bug["etapas"]["maven"] in (cb.SIT_OK, cb.SIT_FALHOU):
        falhas = int(tests.get("failed") or 0) + int(tests.get("error") or 0)
        return f"{tests['total']} · {falhas} falhas"
    return _rotulo(bug["etapas"]["maven"])


def _jaguar(b: bv._Bug, bug: dict, kind: str) -> str:
    if kind not in bug["jobs"]:
        return "—"
    etapa = bug["etapas"][kind]
    if etapa in (cb.SIT_OK, cb.SIT_FALHOU):
        _, pos = b.posicao(kind)
        melhor = (pos or {}).get("melhor")
        if melhor:
            return f"pos. {melhor['expected']:g}"
    if etapa == cb.SIT_RODANDO:
        return "rodando…"
    if etapa == cb.SIT_PENDENTE:
        return "na fila"
    return _rotulo(etapa)


def _linha(home, bug: dict) -> dict:
    """Uma linha da tabela: Maven, Jaguar 1 e Jaguar 2, concordância J1×J2, motivo e perfil."""
    jobs = bug["jobs"]
    primeiro = next(iter(jobs.values()), {})
    extra = json.loads(primeiro.get("bug_extra") or "{}")
    b = bv._Bug(home, {"key": bug["key"], "jobs": jobs, "extra": extra})
    runs = {k: b.run(k) for k in jobs}
    conc = "—"
    if "jaguar1" in jobs and "jaguar2" in jobs:
        c = bv._concordancia(b, bv.TOP_N)
        if c:
            conc = f"{c['n_comuns']}/{c['de']}"
    return {"key": bug["key"], "projeto": bug["projeto"], "bug": bug["bug"], "situacao": bug["situacao"],
            "situacao_rotulo": _rotulo(bug["situacao"]), "maven": _maven(bug, runs),
            "j1": _jaguar(b, bug, "jaguar1"), "j2": _jaguar(b, bug, "jaguar2"), "conc": conc,
            "motivo": planilha._motivo(bug, runs), "perfil": planilha._perfil(runs),
            "importado": bug.get("origem") == ORIGEM}


@router.get("/cobertura/bugs", response_class=HTMLResponse)
def cobertura_bugs(request: Request, situacao: str = "", projeto: str = "", q: str = "", pagina: int = 1,
                   aviso: str = ""):
    st = request.app.state
    bid = cb.batch_id(st.home)
    if bid is None:
        return _voltar()
    base = cb.tabela(st.home, bid, projeto or None)
    contagem = Counter(b["situacao"] for b in base)
    sit = situacao if situacao in SITUACOES else ""
    filtrados = cb.filtrar(base, [sit] if sit else None, q)
    total_pag = max(1, math.ceil(len(filtrados) / PAGINA))
    pagina = min(max(pagina, 1), total_pag)
    trecho = filtrados[(pagina - 1) * PAGINA:pagina * PAGINA]
    filtros = {"situacao": sit, "projeto": projeto, "q": q}
    chips = [{"nome": nome, "n": len(base) if not s else contagem.get(s, 0),
              "href": _href({**filtros, "situacao": s}), "ativo": s == sit} for s, nome in FILTROS]
    return _render(request, "cobertura_bugs.html", {
        "page": "cobertura", "bid": bid, "situacao": sit, "projeto": projeto, "q": q, "pagina": pagina,
        "total": len(filtrados), "total_paginas": total_pag, "chips": chips,
        "aviso": "Nenhum bug marcado." if aviso == "nenhum" else "",
        "linhas": [_linha(st.home, b) for b in trecho],
        "limpar_projeto": _href({**filtros, "projeto": ""}),
        "anterior": _href(filtros, pagina - 1) if pagina > 1 else "",
        "proxima": _href(filtros, pagina + 1) if pagina < total_pag else "",
    })


@router.get("/cobertura/bug", response_class=HTMLResponse)
def cobertura_bug(request: Request, key: str = ""):
    """Detalhe de um bug da tabela (mesmo fragmento do lote)."""
    home = request.app.state.home
    bid = cb.batch_id(home)
    d = bv.bug_detail(home, bid, key) if bid is not None else None
    if d is None:
        return HTMLResponse('<p class="aviso erro">Esse bug não faz parte da cobertura.</p>', status_code=404)
    return _render(request, "_lote_detalhe.html", {"d": d, "bid": bid, "codigo_rel": _codigo_rel(d),
                                                   "ver": {c: bv.veredito(c) for c in bv.VEREDITOS}})


@router.post("/cobertura/bugs/acao")
def cobertura_bugs_acao(request: Request, acao: str = Form(...), keys: list[str] = Form([]),
                        situacao: str = Form(""), projeto: str = Form(""), q: str = Form(""),
                        pagina: int = Form(1)):
    """Ações sobre os bugs marcados; volta para a tabela com os mesmos filtros."""
    if acao not in ACOES:
        return HTMLResponse("Ação desconhecida.", status_code=400)
    filtros = {"situacao": situacao, "projeto": projeto, "q": q}
    if not keys:
        return RedirectResponse(_href(filtros, pagina, aviso="nenhum"), status_code=303)
    st = request.app.state
    bid = cb.batch_id(st.home)
    if bid is not None:
        if acao == "refazer":
            cb.refazer(st.home, bid, keys, so_falhas=False)
        elif acao == "nao_executavel":
            cb.marcar_nao_executavel(st.home, bid, keys)
        else:
            cb.priorizar(st.home, bid, keys)
        if acao in ("refazer", "nao_executavel"):
            planilha.enfileirar(st.home, keys)
            env = getattr(st, "enviador", None)
            if env is not None:
                env.acordar()
    return RedirectResponse(_href(filtros, pagina), status_code=303)
