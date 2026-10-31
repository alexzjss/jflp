"""Tela Cobertura (painel): o lote único Maven + Jaguar 1 + Jaguar 2 sobre todos os bugs do Bugs.jar."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import cobertura as cb
from .. import queue as jq
from .comum import _render

router = APIRouter()

CORES = (("ok", "Concluído"), ("parcial", "Parcial"), ("falhou", "Falhou"), ("nao_executavel", "Não executável"),
         ("rodando", "Rodando"), ("pendente", "Pendente"))
ROTULO_KIND = {"maven": "Maven", "jaguar1": "Jaguar 1", "jaguar2": "Jaguar 2"}
CONCLUIDAS = ("ok", "parcial", "falhou", "nao_executavel")


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


def _selo(request: Request, bid: int, correntes: list[dict]) -> tuple[str, str]:
    worker = request.app.state.worker
    if not worker.status().get("alive"):
        return "Parado", "parado"
    lote = jq.get_batch(request.app.state.home, bid) or {}
    if lote.get("state") == "paused":
        return ("Pausando…", "pausando") if correntes else ("Pausado", "pausado")
    return "Rodando", "rodando"


def _planilha(request: Request) -> dict | None:
    env = getattr(request.app.state, "enviador", None)
    if env is None:
        return None
    s = env.status()
    if not s.get("configurada"):
        return {"tipo": "nao_configurada"}
    if s.get("erro"):
        return {"tipo": "erro", "pendentes": s.get("pendentes", 0), "erro": s["erro"], "ha": _ha(s.get("ultimo_ok"))}
    return {"tipo": "ok", "ha": _ha(s.get("ultimo_ok")), "linhas": s.get("pendentes", 0)}


def painel_ctx(request: Request, bid: int) -> dict:
    st = request.app.state
    home = st.home
    r = cb.resumo(home, bid)
    lista, rows = cb._montar(home, bid, detalhe=True)
    por_chave = {b["key"]: b for b in lista}
    correntes = st.worker.currents()
    agora = datetime.now().timestamp()
    rodando = []
    for c in correntes:
        b = por_chave.get(c["bug"])
        etapas = (b or {}).get("etapas", {})
        chips = [{"nome": ROTULO_KIND[k], "estado": ("rodando" if k == c["kind"] else
                  "feito" if etapas.get(k) in CONCLUIDAS else "espera")} for k in cb.KINDS]
        rodando.append({"bug": c["bug"].split("/", 2)[-1], "projeto": (b or {}).get("projeto", ""),
                        "tempo": _duracao(agora - c.get("started", agora)), "chips": chips})
    ultimos = []
    for b in lista:
        if b["situacao"] not in CONCLUIDAS:
            continue
        fins = [f for f in (cb._fim(j, rows) for j in b["jobs"].values()) if f]
        ultimos.append((max(fins) if fins else datetime.min, b))
    ultimos.sort(key=lambda x: x[0], reverse=True)
    ultimos = [{"bug": b["bug"], "projeto": b["projeto"], "situacao": b["situacao"],
                "quando": _ha(f.isoformat()) if f != datetime.min else "—"} for f, b in ultimos[:5]]
    total = r["total"]
    pct = int(r["concluidos"] * 100 / total) if total else 0
    selo, selo_cls = _selo(request, bid, correntes)
    lote = jq.get_batch(home, bid) or {}
    ritmo = restante = None
    if r["ritmo_h"]:
        ritmo = f"{r['ritmo_h']:.1f}".replace(".", ",")
        if r["restante_s"] is not None:
            restante = f"{r['restante_s'] / 3600:.1f}".replace(".", ",")
    n_workers = st.worker.status().get("workers") or len(correntes) or 1
    return {"r": r, "pct": pct, "selo": selo, "selo_cls": selo_cls, "pausado": lote.get("state") == "paused",
            "rodando": rodando, "ultimos": ultimos, "cores": CORES,
            "n_falhados": r["por_situacao"]["falhou"] + r["por_situacao"]["parcial"],
            "ritmo": ritmo, "restante": restante, "n_workers": n_workers, "planilha": _planilha(request)}


@router.get("/cobertura", response_class=HTMLResponse)
def cobertura_pagina(request: Request):
    st = request.app.state
    bid = cb.batch_id(st.home)
    if bid is None:
        return _render(request, "cobertura.html", {"page": "cobertura", "bid": None, "erro": "",
                       "pasta": st.settings.get("bugsjar", "root", "") or ""})
    return _render(request, "cobertura.html", {"page": "cobertura", "bid": bid, "erro": "",
                   **painel_ctx(request, bid)})


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
    return RedirectResponse("/cobertura", status_code=303)


def _voltar() -> RedirectResponse:
    return RedirectResponse("/cobertura", status_code=303)


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
        cb.refazer(st.home, bid)
    return _voltar()


@router.post("/cobertura/sincronizar")
def cobertura_sincronizar(request: Request):
    st = request.app.state
    pasta = st.settings.get("bugsjar", "root", "")
    if pasta and Path(pasta).is_dir():
        cb.sincronizar(st.home, st.settings, Path(pasta))
    return _voltar()
