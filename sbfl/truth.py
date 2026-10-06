"""Ground truth (linhas defeituosas) de qualquer fonte: dois commits, um patch, ou duas árvores."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .diffgt import TEST_PATH_RE, faults_from_hunks, fqcn_from_path, parse_left_hunks
from .errors import StageError
from .gitutil import git

SKIP_DIRS = {".git", "target", "build", ".gradle", "out", "node_modules"}


def module_of(path: str) -> str:
    m = re.match(r"(.*?)/?src/", path)
    return (m.group(1) if m else "") or "."


def _path_fn(p):
    return None if TEST_PATH_RE.search(p) or not p.endswith(".java") else p


def truth_from_diff(diff: str):
    """diff cujo lado ESQUERDO é a versão com defeito (ex.: `git diff buggy fixed`
    ou um patch que, aplicado ao buggy, produz o corrigido)."""
    hunks = parse_left_hunks(diff, _path_fn)
    faults = faults_from_hunks(hunks, fqcn_from_path)
    if not faults:
        raise StageError("no_truth", "diff buggy->fixed sem linhas em código de produção")
    return faults, module_of(sorted(hunks)[0])


def git_truth(repo, buggy_ref: str, fixed_ref: str):
    return truth_from_diff(git(repo, "diff", "-U0", "--no-color", "--no-renames",
                               buggy_ref, fixed_ref, "--", "*.java"))


def patch_truth(patch_file):
    return truth_from_diff(Path(patch_file).expanduser().read_text(encoding="utf-8", errors="replace"))


def tree_truth(buggy: Path, fixed: Path):
    """Compara duas árvores de diretórios (ex.: dois `checkout` de um CLI de benchmark)."""
    chunks = []
    for p in sorted(buggy.rglob("*.java")):
        parts = p.relative_to(buggy).parts
        rel = "/".join(parts)
        if SKIP_DIRS & set(parts) or TEST_PATH_RE.search(rel):
            continue
        q = fixed / rel
        if not q.exists() or p.read_bytes() == q.read_bytes():
            continue
        d = subprocess.run(["git", "diff", "--no-index", "-U0", "--no-color", "--no-renames", "--",
                            str(p), str(q)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace").stdout
        # reescreve o cabeçalho para o caminho relativo
        d = re.sub(r"^--- .*$", f"--- a/{rel}", d, count=1, flags=re.M)
        chunks.append(d)
    return truth_from_diff("\n".join(chunks))


def crosscheck_classes(dev_patch_text: str | None, faults) -> str:
    """Compara as CLASSES do ground truth (via git) com as de um patch do próprio benchmark.
    Independe da direção do patch. absent | unparsable | agree | partial | disagree."""
    if not dev_patch_text:
        return "absent"
    try:
        other, _ = truth_from_diff(dev_patch_text)
    except StageError:
        return "unparsable"
    a, b = {f.cls for f in faults}, {f.cls for f in other}
    return "agree" if a == b else ("partial" if a & b else "disagree")
