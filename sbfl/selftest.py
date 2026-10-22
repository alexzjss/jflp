"""Mini projeto com um defeito conhecido (sbfl/selftest_data), usado por `jflp selftest` (cli.py)
e pelos testes para validar o ambiente; aqui ficam só os dados e o repositório git de exemplo."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


DATA = Path(__file__).parent / "selftest_data"
CALC = "src/main/java/demo/Calc.java"

def bug_line() -> int:
    """Número da (primeira) linha que difere entre a versão com defeito e a corrigida."""
    a = (DATA / "buggy" / CALC).read_text(encoding="utf-8").splitlines()
    b = (DATA / "fixed" / CALC).read_text(encoding="utf-8").splitlines()
    return next(i for i, (x, y) in enumerate(zip(a, b), 1) if x != y)


def build_repo(repo: Path) -> tuple[str, str]:
    """Repositório git local com o commit corrigido e, depois, o commit com defeito. -> (buggy, fixed)."""
    shutil.copytree(DATA / "fixed", repo)
    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True,
                                  text=True).stdout.strip()
    g("init", "-q"); g("config", "user.email", "selftest@local"); g("config", "user.name", "selftest")
    g("add", "."); g("commit", "-qm", "fixed")
    fixed = g("rev-parse", "HEAD")
    shutil.copy(DATA / "buggy" / CALC, repo / CALC)
    g("commit", "-qam", "buggy")
    return g("rev-parse", "HEAD"), fixed
