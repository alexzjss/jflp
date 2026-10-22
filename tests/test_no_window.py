"""Todo processo-filho de console precisa de creationflags (o app roda sem console, via pythonw)."""
from __future__ import annotations

import ast
from pathlib import Path

SBFL = Path(__file__).resolve().parent.parent / "sbfl"
CALLS = {"run", "Popen", "check_output", "check_call", "call"}

# Chamadas que de propósito não levam NO_WINDOW (arquivo -> motivo).
PERMITIDOS = {
    "desktop.py:open_app_window": "abre a janela do navegador (GUI)",
    "web/routes_runs.py": "abrir pasta no explorador (GUI)",
    "power.py": "caffeinate/systemd-inhibit: só macOS/Linux",
}


def _chamadas(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))

    def visita(node, func):
        for ch in ast.iter_child_nodes(node):
            nome = ch.name if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)) else func
            if isinstance(ch, ast.Call) and isinstance(ch.func, ast.Attribute) and ch.func.attr in CALLS \
                    and isinstance(ch.func.value, ast.Name) and ch.func.value.id == "subprocess":
                yield ch, func
            yield from visita(ch, nome)

    yield from visita(tree, None)


def test_toda_chamada_de_subprocess_tem_creationflags():
    faltando = []
    for path in sorted(SBFL.rglob("*.py")):
        rel = path.relative_to(SBFL).as_posix()
        for call, func in _chamadas(path):
            if rel in PERMITIDOS or f"{rel}:{func}" in PERMITIDOS:
                continue
            if "creationflags" not in {k.arg for k in call.keywords}:
                faltando.append(f"{rel}:{call.lineno}")
    assert not faltando, f"subprocess sem creationflags=NO_WINDOW: {faltando}"
