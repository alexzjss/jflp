"""Ground truth (linhas defeituosas) a partir do diff buggy -> corrigido.

Convenção: o lado ESQUERDO do diff é a versão com defeito.
 - hunk que remove/altera linhas  -> essas linhas (kind="removed")
 - hunk de pura adição (ex.: um `if`/loop novo, como no MNG-5742) -> não há linha
   defeituosa de verdade; usamos as linhas vizinhas à inserção (kind="anchor").
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
SRC_RE = re.compile(r"(?:^|/)(?:src/main/java|src/java|src/main|java|src)/(.+)\.java$")
TEST_PATH_RE = re.compile(r"(^|/)(test|tests)/")


@dataclass(frozen=True)
class FaultLine:
    cls: str
    line: int
    kind: str  # removed | anchor

    def to_dict(self):
        return asdict(self)


def parse_left_hunks(diff_text: str, path_fn) -> dict:
    """{path: [(start, count), ...]} do lado esquerdo. path_fn(path)->path|None filtra arquivos."""
    out, cur, in_header = {}, None, False
    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            in_header, cur = True, None
            continue
        if in_header:
            if line.startswith("--- "):
                p = line[4:].strip()
                if p == "/dev/null":
                    cur = None
                else:
                    cur = path_fn(p[2:] if p.startswith("a/") else p)
                continue
            if line.startswith("+++ "):
                continue
        m = HUNK.match(line)
        if m:
            in_header = False
            if cur:
                start = int(m.group(1))
                count = 1 if m.group(2) is None else int(m.group(2))
                out.setdefault(cur, []).append((start, count))
    return out


def fqcn_from_path(rel: str) -> str | None:
    m = SRC_RE.search(rel.replace("\\", "/"))
    return m.group(1).replace("/", ".") if m else None


def faults_from_hunks(hunks: dict, to_cls) -> list[FaultLine]:
    res = []
    for path, hs in hunks.items():
        cls = to_cls(path)
        if not cls:
            continue
        for start, count in hs:
            if count > 0:
                res += [FaultLine(cls, ln, "removed") for ln in range(start, start + count)]
            else:  # pura adição: a inserção ocorre depois da linha `start`
                res += [FaultLine(cls, ln, "anchor") for ln in (start, start + 1) if ln > 0]
    return sorted(set(res), key=lambda f: (f.cls, f.line, f.kind))


def select_lines(faults: list[FaultLine]) -> list[FaultLine]:
    removed = [f for f in faults if f.kind == "removed"]
    return removed or faults
