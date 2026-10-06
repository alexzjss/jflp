"""`sbfl selftest`: roda o pipeline inteiro (Maven + Jaguar reais) num mini projeto com um defeito
conhecido (sbfl/selftest_data). Rode antes de qualquer campanha."""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path

from .adapters.base import Bug
from .adapters.gitpair import GitPairAdapter
from .config import Config
from .pipeline import process

DATA = Path(__file__).parent / "selftest_data"
CALC = "src/main/java/demo/Calc.java"

HINTS = {
    "build_failed": "o Maven falhou: confira `mvn -v`, a internet (o 1º uso baixa plugins) e [java] java_home",
    "jaguar_crash": "a JVM do Jaguar morreu: confira jaguar_lib, o Java (prefira 8) e o log em results/",
    "jaguar_timeout": "estourou o timeout: aumente [jaguar] timeout_s ou veja se o processo travou",
    "no_failing_tests": "nenhum teste falhou: o runner pode não estar executando o JUnit 4 do mini projeto",
    "fault_not_covered": "o Jaguar rodou, mas a linha do defeito não aparece no relatório",
    "no_tests": "nenhuma classe de teste encontrada em target/test-classes",
    "git_failed": "problema com o git; rode `git --version`",
}


class _Adapter(GitPairAdapter):
    name, section = "selftest", "manifest"

    def list_bugs(self, project=None, bug=None):
        return []


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


def run(cfg) -> int:
    base = cfg.work / "_selftest"
    shutil.rmtree(base, ignore_errors=True)
    buggy, fixed = build_repo(base / "repo")
    data = copy.deepcopy(cfg.data)
    data["paths"].update(workdir=str(base / "w"), results=str(base / "results"))
    data["jaguar"]["test_scope"] = "all"
    cfg2 = Config(data, cfg.base)
    bug = Bug("selftest", "demo", "demo-1", {"repo": str(base / "repo"), "buggy": buggy, "fixed": fixed})
    ln = bug_line()
    print(f"selftest: defeito esperado em demo.Calc:{ln}")
    meta = process(cfg2, _Adapter(cfg2), bug, force=True)
    status = meta.get("status")
    d = cfg2.results / bug.benchmark / bug.project / bug.bug_id
    ok = False
    if status == "ok":
        rows = json.loads((d / "metrics.json").read_text(encoding="utf-8"))
        och = next(r for r in rows if r["heuristic"] == "ochiai")
        if och["rank_worst"] == 1:
            ok = True
            print("[OK] linha defeituosa em 1º lugar (Ochiai, pior caso nos empates)")
        else:
            print(f"[XX] linha defeituosa em {och['rank_worst']}º (pior caso); esperado 1º. "
                  "Confira o ground truth e o espectro com `inspect`.")
    else:
        print(f"[XX] status: {status}")
        if meta.get("detail"):
            print("     " + str(meta["detail"])[-600:].replace("\n", "\n     "))
        print("     dica: " + HINTS.get(status, f"veja o log em {d}"))
    print(f"     tempo: {meta.get('total_seconds')}s | java: {meta.get('java')}")
    print("SELFTEST OK — o ambiente está pronto para uma campanha." if ok else
          f"SELFTEST FALHOU (status {status}). Resultados em {cfg2.results}")
    return 0 if ok else 1
