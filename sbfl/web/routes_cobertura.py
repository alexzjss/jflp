"""Tela Cobertura (painel): o lote único Maven + Jaguar 1 + Jaguar 2 sobre todos os bugs do Bugs.jar."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import cobertura as cb
from .. import planilha
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
    if s.get("erro"):
        return {"tipo": "erro", "pendentes": s.get("pendentes", 0), "erro": s["erro"], "ha": _ha(s.get("ultimo_ok"))}
    return {"tipo": "ok", "ha": _ha(s.get("ultimo_ok")), "linhas": planilha.linhas_total(request.app.state.home)}


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
    return {"r": r, "pct": pct, "selo": selo, "selo_cls": selo_cls, "pausado": lote_estado == "paused",
            "concluida": lote_estado == "done", "outra_instancia": outra,
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
