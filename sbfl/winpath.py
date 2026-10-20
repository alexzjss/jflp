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


def lp(p) -> Path:
    """Como `long_path`, mas devolvendo `Path` (para rglob/iterdir em árvores profundas)."""
    return Path(long_path(p))
