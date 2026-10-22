"""Caminhos longos no Windows: forma estendida `\\\\?\\` para as operações de arquivo do Python."""
from __future__ import annotations

import os
from pathlib import Path


def long_path(p) -> str:
    """Caminho absoluto no formato `\\\\?\\` (Windows); inalterado nos outros sistemas."""
    s = str(p)
    if os.name != "nt" or s.startswith("\\\\?\\"):
        return s
    s = os.path.abspath(s)
    if s.startswith("\\\\"):  # \\servidor\share -> \\?\UNC\servidor\share
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s


def inside(child, base) -> bool:
    """`child` é `base` ou fica dentro dela? Compara caminhos normalizados, sem `resolve()`: no Windows,
    um processo aberto dentro de um app empacotado (MSIX) vê o AppData redirecionado e `resolve()` devolve
    o caminho virtual de arquivos novos, o que faria um arquivo legítimo parecer fora da pasta."""
    c = os.path.normcase(os.path.abspath(str(child)))
    b = os.path.normcase(os.path.abspath(str(base)))
    return c == b or c.startswith(b.rstrip(os.sep) + os.sep)


def lp(p) -> Path:
    """Como `long_path`, mas devolvendo `Path` (para rglob/iterdir em árvores profundas)."""
    return Path(long_path(p))
