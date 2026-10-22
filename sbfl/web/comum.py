"""Helpers compartilhados pelas telas da interface (Rodar, Resultados, lote, execução, ambiente)."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

from fastapi import Request

from .. import queue as jobs_queue
from ..adapters.base import Bug
from ..prepare import bug_from_key, bugsjar_repo_bugs, is_bugsjar_repo

MAX_RECENTES = 10

KINDS = (("maven", "Maven"), ("jaguar2", "Jaguar 2"), ("jaguar1", "Jaguar 1"))
KIND_LABEL = dict(KINDS)
BUGSJAR_KINDS = ("jaguar1", "maven")   # padrão ao rodar defeitos do Bugs.jar: o que a pesquisa mais usa


def _render(request: Request, name: str, ctx: dict, status: int = 200):
    return request.app.state.templates.TemplateResponse(request, name, ctx, status_code=status)


def _msg(request: Request, page: str, title: str, text: str, status: int):
    return _render(request, "mensagem.html", {"page": page, "titulo": title, "texto": text}, status)


def kinds_validos(kinds: list[str], padrao: tuple[str, ...] = BUGSJAR_KINDS) -> list[str]:
    """Tipos reconhecidos na ordem de KINDS; sem nenhum válido, o padrão (Jaguar 1 e Maven)."""
    ks = [k for k, _ in KINDS if k in kinds]
    return ks or list(padrao)


# ----------------------------------------------------------------- recentes

def _recentes_path(home) -> Path:
    return Path(home.root) / "recentes.json"


def load_recentes(home) -> list[dict]:
    """Projetos usados por último (mais recente primeiro); arquivo ausente ou inválido = lista vazia."""
    try:
        data = json.loads(_recentes_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for r in data:
        if isinstance(r, dict) and isinstance(r.get("spec"), str) and r["spec"].strip():
            out.append({"spec": r["spec"], "nome": str(r.get("nome") or r["spec"]),
                        "data": str(r.get("data") or ""),
                        "kinds": [k for k in (r.get("kinds") or []) if k in KIND_LABEL]
                        if isinstance(r.get("kinds"), list) else [],
                        "tests": [str(t) for t in r["tests"]] if isinstance(r.get("tests"), list) else [],
                        "auto_search": bool(r.get("auto_search", True)),
                        "module": r["module"].strip() if isinstance(r.get("module"), str) else ""})
    return out[:MAX_RECENTES]


def add_recente(home, spec: str, nome: str, kinds: list[str], tests: list[str], auto_search: bool,
                module: str = "") -> None:
    """Põe o projeto no topo dos recentes (sem duplicar o mesmo spec+módulo) e mantém no máximo 10."""
    entry = {"spec": spec, "nome": nome, "data": datetime.now().isoformat(timespec="seconds"),
             "kinds": list(kinds), "tests": list(tests), "auto_search": bool(auto_search), "module": module}
    lst = [entry] + [r for r in load_recentes(home) if (r["spec"], r["module"]) != (spec, module)]
    path = _recentes_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(lst[:MAX_RECENTES], ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- formulário

def _tk_available() -> bool:
    try:
        import tkinter  # noqa: F401
    except ImportError:
        return False
    return True


def _pick_folder() -> str:
    """Seletor de pastas nativo, numa thread própria e com a janela no topo. "" se cancelar ou falhar."""
    out = [""]

    def work() -> None:
        try:
            import tkinter
            from tkinter import filedialog
            root = tkinter.Tk()
        except Exception:        # sem tkinter ou sem tela
            return
        try:
            root.withdraw()
            root.attributes("-topmost", True)
            out[0] = filedialog.askdirectory(parent=root, title="Escolha a pasta do projeto") or ""
        except Exception:
            out[0] = ""
        finally:
            root.destroy()

    t = threading.Thread(target=work, name="jflp-folder-picker")
    t.start()
    t.join()
    return out[0]


def _ramo(repo: Path) -> str:
    """Ramo atual do repositório (lê `.git/HEAD`); "" se não der para saber."""
    try:
        head = (repo / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return head[len("ref: refs/heads/"):] if head.startswith("ref: refs/heads/") else head[:8]


def is_bugsjar_repo_ou_ramo(r: Path) -> bool:
    """Repositório git com ramos do Bugs.jar (o ramo atual pode nem ter `.bugs-dot-jar/`)."""
    return (r / ".git").exists() and (is_bugsjar_repo(r) or bool(bugsjar_repo_bugs(r)))


# --------------------------------------------------------------------- bugs

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
